# app/services/execution_service.py
"""
Campaign Execution Engine.
Handles the recurring loop of picking eligible prospects and generating email messages.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_
from typing import List, Optional, Tuple, Set
from datetime import datetime, timedelta
import uuid

from app.models.campaign import Campaign, CampaignProspect
from app.schemas.campaign_schema import CampaignStatus
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.email_message import EmailMessage
from app.services.audit_service import AuditService
from app.core.config import settings
from app.utils.email_utils import normalize_cta_link, has_effective_cta_link, strip_cta_content_no_link, normalize_unsubscribe_footer, normalize_paragraph_spacing, finalize_email_body, build_signature_block
from app.utils.business_calendar import schedule_email_in_window, get_timezone_for_state
from app.utils.campaign_prospect_status import set_prospect_status

class CampaignExecutionService:
    """
    Background worker service for campaign execution.
    Follows strict 5-step loop:
    1. Pick eligible prospects
    2. Pre-send hard checks
    3. Create email message
    4. Personalize (Stubbed here as creation)
    5. Update state
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    def _substitute_placeholders(
        self,
        template: str,
        prospect: Prospect,
        sender_name: Optional[str] = None,
        cta_link: Optional[str] = None,
        sender_title: Optional[str] = None
    ) -> str:
        """Replace placeholders in template with prospect data."""
        if not template:
            return template

        normalized_cta_link = normalize_cta_link(cta_link)

        substitutions = {
            "{{first_name}}": prospect.first_name or "there",
            "{{last_name}}": prospect.last_name or "",
            "{{full_name}}": f"{prospect.first_name or ''} {prospect.last_name or ''}".strip() or "there",
            "{{company_name}}": prospect.company_name or "your company",
            "{{company}}": prospect.company_name or "your company",
            "{{designation}}": prospect.designation or "Professional",
            "{{title}}": prospect.designation or "Professional",
            "{{email}}": prospect.email or "",
            "{{industry}}": prospect.industry or "",
            "{{linkedin_url}}": prospect.linkedin_url or "",
            "{{city}}": prospect.poc_city or "",
            "{{state}}": prospect.poc_state or "",
            "{{your_name}}": sender_name or settings.SENDER_NAME,
            "{{signature_block}}": build_signature_block(sender_name, sender_title),
            "{{our_company}}": "Neutrino Tech Systems",
            "{{calendar_link}}": normalized_cta_link,
            "{{cta_link}}": normalized_cta_link,
        }

        result = template
        for placeholder, value in substitutions.items():
            result = result.replace(placeholder, str(value))
            result = result.replace(placeholder.lower(), str(value))
            result = result.replace(placeholder.upper(), str(value))

        return result

    def execute_cycle(self, limit: int = 50) -> int:
        """
        Run one execution cycle.
        Returns number of messages created.
        """
        # Step 1: Pick eligible prospects
        # MUST be:
        # - Campaign is ACTIVE
        # - Prospect status in Campaign is ACTIVE
        # - Scheduled time <= NOW
        
        now = datetime.utcnow()
        
        eligible_candidates = self.db.query(CampaignProspect).join(
            Campaign, CampaignProspect.campaign_id == Campaign.campaign_id
        ).filter(
            Campaign.status == CampaignStatus.ACTIVE.value,
            CampaignProspect.status == "ACTIVE",
            CampaignProspect.next_scheduled_at <= now
        ).limit(limit).all()
        
        if not eligible_candidates:
            return 0
            
        processed_count = 0
        
        for cp in eligible_candidates:
            try:
                # Step 2: Pre-send Hard Checks
                if not self._check_gates(cp):
                    continue
                    
                # Get Sequence Step
                step = self.db.query(EmailSequence).filter(
                    EmailSequence.campaign_id == cp.campaign_id,
                    EmailSequence.step_number == cp.current_step
                ).first()
                
                if not step:
                    # Logic error or end of sequence?
                    # If step > max steps, maybe mark completed?
                    # For now skip
                    continue
                    
                # Get Template (using Stage 4 Persona Logic)
                prospect = self.db.query(Prospect).filter(Prospect.prospect_id == cp.prospect_id).first()
                from app.models.prospect_persona import ProspectPersona
                from app.services.ai_email_service import classify_prospect
                
                existing_persona = self.db.query(ProspectPersona).filter(
                    ProspectPersona.prospect_id == prospect.prospect_id
                ).first()
                if existing_persona:
                    persona_type = existing_persona.persona_type
                else:
                    persona_type, _ = classify_prospect(prospect.designation or "", prospect.company_name)
                
                template = self._get_template(cp.campaign_id, step.sequence_id, persona_type)
                
                if not template:
                    # Log error - missing template?
                    continue

                if not template.approved_by:
                    # Stage 5 Gate: Template MUST be approved
                    # If not approved, skip this send.
                    continue

                # Step 3: Create Email Message
                msg_id = str(uuid.uuid4())

                # Full placeholder substitution
                cta_link = normalize_cta_link(cp.campaign.cta_link)
                sender_name = cp.campaign.sender_name if hasattr(cp.campaign, 'sender_name') else None
                sender_title = getattr(cp.campaign, 'sender_title', None)
                final_subject = self._substitute_placeholders(
                    template.subject or "", prospect, sender_name, cta_link, sender_title
                )
                final_body = self._substitute_placeholders(
                    template.body or "", prospect, sender_name, cta_link, sender_title
                )
                if not has_effective_cta_link(cta_link):
                    final_body = strip_cta_content_no_link(final_body)
                # final_body = normalize_unsubscribe_footer(final_body)
                # final_body = normalize_paragraph_spacing(final_body)
                final_body = finalize_email_body(final_body)
                
                # Determine prospect timezone
                prospect_tz = None
                if cp.campaign.respect_timezone and prospect.poc_state:
                    prospect_tz = get_timezone_for_state(prospect.poc_state, cp.campaign.campaign_timezone)

                effective_tz = prospect_tz or cp.campaign.campaign_timezone or "UTC"


                # For spread/batch modes we need the prospect's rank and total count
                # at the current step so emails are distributed evenly across the window.
                sending_mode = getattr(cp.campaign, 'sending_mode', 'spread') or 'spread'
                prospect_index = 0
                total_prospects = 1
                if sending_mode in ('spread', 'batch'):
                    total_prospects = self.db.query(func.count(CampaignProspect.id)).filter(
                        CampaignProspect.campaign_id == cp.campaign_id,
                        CampaignProspect.current_step == cp.current_step,
                    ).scalar() or 1
                    # rank = how many enrolled before this one (stable ordering by PK)
                    prospect_index = self.db.query(func.count(CampaignProspect.id)).filter(
                        CampaignProspect.campaign_id == cp.campaign_id,
                        CampaignProspect.current_step == cp.current_step,
                        CampaignProspect.id < cp.id,
                    ).scalar() or 0

                scheduled_at = schedule_email_in_window(
                    base_utc=datetime.utcnow(),
                    timezone_str=effective_tz,
                    send_window_start=cp.campaign.send_window_start,
                    send_window_end=cp.campaign.send_window_end,
                    sending_mode=sending_mode,
                    prospect_index=prospect_index,
                    total_prospects=total_prospects,
                    min_gap_minutes=getattr(cp.campaign, 'min_gap_minutes', 2) or 2,
                    batch_size=getattr(cp.campaign, 'batch_size', None),
                    batch_gap_minutes=getattr(cp.campaign, 'batch_gap_minutes', 30) or 30,
                )
                
                email_msg = EmailMessage(
                    message_id=msg_id,
                    campaign_id=cp.campaign_id,
                    prospect_id=cp.prospect_id,
                    sequence_id=step.sequence_id,
                    template_id=template.template_id,
                    from_email=None,  # Allow scheduler to assign based on inbox rotation
                    to_email=prospect.email,
                    subject=final_subject,
                    body_text=final_body,
                    status="QUEUED",
                    sent_at=None, # Will be set by actual sender
                    scheduled_at=scheduled_at
                )
                self.db.add(email_msg)
                
                # Step 5: Update Execution State
                # Calculate next step time
                next_step_number = cp.current_step + 1
                next_step = self.db.query(EmailSequence).filter(
                    EmailSequence.campaign_id == cp.campaign_id,
                    EmailSequence.step_number == next_step_number
                ).first()
                
                if next_step:
                    cp.current_step = next_step_number
                    # Calculate delay
                    cp.next_scheduled_at = datetime.utcnow() + timedelta(days=next_step.wait_days)
                else:
                    # End of sequence
                    set_prospect_status(cp, "COMPLETED")
                    cp.next_scheduled_at = None
                    
                processed_count += 1
                
            except Exception as e:
                print(f"Error processing prospect {cp.prospect_id}: {e}")
                continue
        
        self.db.commit()
        return processed_count

    def _check_gates(self, cp: CampaignProspect) -> bool:
        """Run hard checks before sending."""
        prospect = self.db.query(Prospect).filter(Prospect.prospect_id == cp.prospect_id).first()
        if not prospect:
            return False
            
        # 1. Global Unsubscribe
        is_unsub = self.db.query(GlobalUnsubscribe).filter(
            GlobalUnsubscribe.email == prospect.email
        ).first()
        if is_unsub:
            set_prospect_status(cp, "UNSUBSCRIBED", stopped_reason="Global Unsubscribe")
            return False
            
        # 2. Cool-off Window (24h)
        cutoff = datetime.utcnow() - timedelta(hours=24)
        last_sent = self.db.query(EmailMessage).filter(
            EmailMessage.prospect_id == cp.prospect_id,
            EmailMessage.sent_at > cutoff
        ).first()
        if last_sent:
            # Delay execution, don't fail, just skip for now
            return False
            
        return True

    def _get_template(self, campaign_id: str, sequence_id: str, designation: Optional[str]) -> Optional[EmailTemplate]:
        """Find best matching template."""
        query = self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.sequence_id == sequence_id
        )
        
        if designation:
            # Try specific match
            specific = query.filter(EmailTemplate.designation == designation).first()
            if specific:
                return specific
        
        # Fallback to general
        return query.filter(or_(EmailTemplate.designation == None, EmailTemplate.designation == "")).first()
