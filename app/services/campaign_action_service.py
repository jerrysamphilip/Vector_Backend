# Kartik has changed this: Split God object into focused service
# app/services/campaign_service.py
"""
Campaign service for CRUD operations and state management.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional, Tuple, Set
from datetime import datetime, timedelta
import uuid

from app.core.config import settings

from app.models.campaign import Campaign, CampaignStateEvent, CampaignProspect
from app.models.user import User
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectListMember
from app.models.metrics import CampaignMetricsRealtime
from app.models.email_sequence import EmailSequence
from app.models.email_message import EmailMessage, EmailEvent
from app.models.email_template import EmailTemplate
from app.models.sending_inbox import SendingInbox
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignUpdate,
    CampaignFilter,
    CampaignStatus,
    CampaignResponse,
    CampaignListItem,
    CampaignListResponse,
    CampaignMetrics,
    CampaignEnrollmentRequest,
)
from app.schemas.sequence_schema import SequenceStepCreate, SequenceStepResponse
from app.services.audit_service import AuditService
from app.services.metrics_service import MetricsService


class CampaignActionService:
    """Service for campaign management."""
    
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)
        self.metrics = MetricsService(db)
    
    # =============================
    # CRUD OPERATIONS
    # =============================
    
    
    
    
    
    
    # =============================
    # CONFIGURATION
    # =============================
    
    def add_sequence_step(
        self,
        campaign_id: str,
        data: SequenceStepCreate
    ) -> EmailSequence:
        """Add a step to the campaign sequence."""
        campaign = self.get(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")
        
        # Check step count limit (max 7)
        count = self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar()
        
        if count >= 7:
            raise ValueError("Maximum 7 steps allowed per campaign")
            
        # Check duplicate step number
        existing = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == data.step_number
        ).first()
        
        if existing:
            raise ValueError(f"Step {data.step_number} already exists")
            
        step = EmailSequence(
            sequence_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            step_number=data.step_number,
            wait_days=data.wait_days,
            stop_on_reply=data.stop_on_reply,
            stop_on_bounce=data.stop_on_bounce,
            send_start_hour=data.send_start_hour,
            send_end_hour=data.send_end_hour,
        )
        self.db.add(step)
        self.db.flush()
        return step

    def generate_stub_content(self, campaign_id: str, user_id: str) -> int:
        """
        Stage 4 Stub: Generate placeholder templates for all steps.
        Groups by 'designation' of enrolled prospects to create persona-based templates.
        """
        sequences = self.db.query(EmailSequence).filter(EmailSequence.campaign_id == campaign_id).all()
        if not sequences:
            raise ValueError("No sequence steps defined")

        # 1. Identify Personas (Designations)
        # Verify if we have enrolled prospects. If not, default to generic.
        designations = [None] # Default generic
        
        # Get distinct designations from currently enrolled (if any)
        enrolled_designations = self.db.query(Prospect.designation).join(
            CampaignProspect, Prospect.prospect_id == CampaignProspect.prospect_id
        ).filter(
            CampaignProspect.campaign_id == campaign_id
        ).distinct().all()
        
        if enrolled_designations:
            # Flatten result [(None,), ('CEO',)] -> [None, 'CEO']
            fetched = [d[0] for d in enrolled_designations]
            if fetched:
                designations = fetched

        count = 0
        for designation in designations:
            for seq in sequences:
                # Check if template exists for this sequence + designation
                query = self.db.query(EmailTemplate).filter(
                    EmailTemplate.campaign_id == campaign_id,
                    EmailTemplate.sequence_id == seq.sequence_id
                )
                
                if designation:
                    query = query.filter(EmailTemplate.designation == designation)
                else:
                    query = query.filter(or_(EmailTemplate.designation == None, EmailTemplate.designation == ""))

                exists = query.first()
                
                if not exists:
                    # Create Stub
                    persona_label = designation if designation else "General"
                    subject = f"[{persona_label}] Subject for Step {seq.step_number}"
                    body = f"Hi {{first_name}},\n\nThis is a placeholder {persona_label} email for step {seq.step_number}."
                    
                    tmpl = EmailTemplate(
                        template_id=str(uuid.uuid4()),
                        campaign_id=campaign_id,
                        sequence_id=seq.sequence_id,
                        designation=designation,
                        subject=subject,
                        body=body,
                        is_ai_generated=False,
                        # approved_by=user_id, # Removed to enforce Manual Approval (Stage 5)
                        # Setting approved_by=None to force Stage 5 Manual Approval
                        # approved_by=None, 
                        # approved_at=None
                        # Actually, keeping it pre-approved for 'Stub' to speed up testing per "stub" name, 
                        # BUT strict plan says Stage 5 is Approval.
                        # Let's leave it unapproved to enforce Stage 5.
                        approved_by=None,
                        approved_at=None
                    )
                    self.db.add(tmpl)
                    count += 1
                
        self.db.flush()
        return count

    def enroll_prospects(
        self,
        campaign_id: str,
        user_id: str,
        request: CampaignEnrollmentRequest
    ) -> int:
        """
        Enroll prospects into campaign (Stage 3 Gate).
        
        Strict Rules:
        1. Not Global Unsubscribed
        2. Consent is OPT_IN
        3. Valid Email
        4. Not already in this campaign
        5. Cool-off: No email sent in last 24h
        """
        campaign = self.get(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")
            
        if campaign.status not in [CampaignStatus.DRAFT.value, CampaignStatus.PAUSED.value, CampaignStatus.ACTIVE.value]:
            raise ValueError("Cannot enroll prospects in current campaign status")

        # 1. Gather Candidate IDs
        candidate_ids = set()
        if request.prospect_ids:
            candidate_ids.update(request.prospect_ids)
            
        if request.list_ids:
            members = self.db.query(ProspectListMember.prospect_id).filter(
                ProspectListMember.list_id.in_(request.list_ids)
            ).all()
            candidate_ids.update([m[0] for m in members])
            
        if not candidate_ids:
            return 0

        # 2. Fetch Data for Rules (Set-based filtering)
        # Fetch Prospect Objects
        prospects = self.db.query(Prospect).filter(Prospect.prospect_id.in_(candidate_ids)).all()
        p_map = {p.prospect_id: p for p in prospects}
        candidate_emails = {p.email for p in prospects}
        
        # Rule 1: Active Global Unsubscribe (suppression not yet expired)
        # A prospect is blocked if they have a global_unsubscribe record AND:
        #   - suppression_expires_at IS NULL (legacy permanent block), OR
        #   - suppression_expires_at > NOW (suppression still active)
        # Prospects whose suppression period has expired ARE allowed through.
        now = datetime.utcnow()
        global_unsubs_emails = {r[0] for r in self.db.query(GlobalUnsubscribe.email).filter(
            GlobalUnsubscribe.tenant_id == campaign.tenant_id,
            GlobalUnsubscribe.email.in_(candidate_emails),
            or_(
                GlobalUnsubscribe.suppression_expires_at.is_(None),  # NULL = permanent
                GlobalUnsubscribe.suppression_expires_at > now        # suppression active
            )
        ).all()}
        
        # Rule 4: Already enrolled in this campaign
        existing_enrolled_ids = {r[0] for r in self.db.query(CampaignProspect.prospect_id).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.prospect_id.in_(candidate_ids)
        ).all()}
        
        # Rule 5: Cool-off (Check EmailMessage sent in last 24h)
        cutoff_time = datetime.utcnow() - timedelta(hours=24)
        cooloff_ids = {r[0] for r in self.db.query(EmailMessage.prospect_id).filter(
            EmailMessage.prospect_id.in_(candidate_ids),
            EmailMessage.sent_at > cutoff_time
        ).all()}
        
        # Calculate Schedule Start (based on Step 1 wait_days)
        step1 = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == 1
        ).first()
        start_delay_days = step1.wait_days if step1 else 0
        scheduled_time = datetime.utcnow() + timedelta(days=start_delay_days)

        # 3. Filter and Build Enrollments
        new_enrollments = []
        enrolled_count = 0
        
        for pid in candidate_ids:
            prospect = p_map.get(pid)
            if not prospect:
                continue
                
            # strict gates
            if prospect.email in global_unsubs_emails:
                continue
            if prospect.consent_status != "OPT_IN":
                continue
            if not prospect.is_valid_email:
                continue
            if pid in existing_enrolled_ids:
                continue
            if pid in cooloff_ids:
                continue
            
            # Pass all gates -> Enroll
            new_enrollments.append(CampaignProspect(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                prospect_id=pid,
                current_step=1,
                status="ACTIVE",  # Use ACTIVE as initial status per plan (or QUEUED?) Plan says "ACTIVE" in INSERT intent.
                enrolled_at=datetime.utcnow(),
                next_scheduled_at=scheduled_time
            ))
            enrolled_count += 1
            
        if new_enrollments:
            self.db.bulk_save_objects(new_enrollments)
            self.db.flush()
        
        if enrolled_count > 0:
             self.audit.log_action(
                tenant_id=campaign.tenant_id,
                user_id=user_id,
                action=f"{AuditService.ACTION_UPDATE}_ENROLLED_{enrolled_count}",
                entity_type=AuditService.ENTITY_CAMPAIGN,
                entity_id=campaign_id,
            )
            
        return enrolled_count
    
    # =============================
    # STATE MANAGEMENT
    # =============================
    
    def pause(self, campaign_id: str, user_id: str, reason: Optional[str] = None) -> Optional[Campaign]:
        """
        Pause an active campaign.
        
        Args:
            campaign_id: Campaign ID
            user_id: User pausing the campaign
            reason: Optional reason for pausing
        
        Returns:
            Updated Campaign or None if not found
        """
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        
        if campaign.status != CampaignStatus.ACTIVE.value:
            raise ValueError(f"Cannot pause campaign with status: {campaign.status}")
        
        old_status = campaign.status
        campaign.status = CampaignStatus.PAUSED.value
        campaign.paused_reason = reason
        campaign.paused_at = datetime.utcnow()
        campaign.auto_paused = False
        
        # ── CORE FIX: Freeze all queued emails for this campaign.
        # Without this, QUEUED EmailMessage rows keep their scheduled_at and
        # the scheduler picks them up and sends them even though the campaign
        # is PAUSED. Changing status to 'PAUSED_BY_CAMPAIGN' removes them
        # from the scheduler's QUEUED/SCHEDULED filter entirely.
        from app.models.email_message import EmailMessage
        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status.in_(["QUEUED", "SCHEDULED"])
        ).update(
            {"status": "PAUSED_BY_CAMPAIGN"},
            synchronize_session=False
        )
        
        # Record state change
        self._record_state_change(campaign_id, old_status, campaign.status, reason)
        
        # Log action
        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_PAUSE,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )
        
        self.db.flush()
        return campaign
    
    def resume(self, campaign_id: str, user_id: str) -> Optional[Campaign]:
        """
        Resume a paused campaign.
        
        Args:
            campaign_id: Campaign ID
            user_id: User resuming the campaign
        
        Returns:
            Updated Campaign or None if not found
        """
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        
        if campaign.status != CampaignStatus.PAUSED.value:
            raise ValueError(f"Cannot resume campaign with status: {campaign.status}")
        
        old_status = campaign.status
        campaign.status = CampaignStatus.ACTIVE.value
        if campaign.auto_paused:
            # The user has reviewed the auto-pause: judge health on sends from now on (BR-DF-07)
            campaign.health_baseline_at = datetime.utcnow()
        campaign.auto_paused = False
        campaign.paused_reason = None
        campaign.paused_at = None
        
        # ── CORE FIX: Restore all emails that were frozen by pause().
        # Set them back to QUEUED so the scheduler picks them up again.
        from app.models.email_message import EmailMessage
        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status == "PAUSED_BY_CAMPAIGN"
        ).update(
            {"status": "QUEUED"},
            synchronize_session=False
        )
        
        # Record state change
        self._record_state_change(campaign_id, old_status, campaign.status)
        
        # Log action
        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_RESUME,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )
        
        self.db.flush()
        return campaign
    
    def launch(self, campaign_id: str, user_id: str) -> Optional[Campaign]:
        """
        Launch a draft campaign (set to ACTIVE).
        
        Args:
            campaign_id: Campaign ID
            user_id: User launching the campaign
        
        Returns:
            Updated Campaign or None if not found
        """
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        
        if campaign.status != CampaignStatus.DRAFT.value:
            raise ValueError(f"Cannot launch campaign with status: {campaign.status}")
        
        # Validation Gates (Stage 6)
        # 1. Must have sequence steps
        step_count = self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar()
        if step_count == 0:
            raise ValueError("Cannot launch campaign without sequence steps")
            
        # 2. Must have templates for steps
        tmpl_count = self.db.query(func.count(EmailTemplate.template_id)).filter(
            EmailTemplate.campaign_id == campaign_id
        ).scalar()
        if tmpl_count < step_count:
             raise ValueError(f"Missing email templates. Defined: {step_count}, Found: {tmpl_count}")
        
        # Auto-approve all templates (User Request)
        unapproved_templates = self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.approved_by == None
        ).all()
        
        for t in unapproved_templates:
            t.approved_by = user_id
            t.approved_at = datetime.utcnow()
        
        # Pre-schedule all sequence emails for enrolled prospects
        self._preschedule_all_emails(campaign_id)
        
        old_status = campaign.status
        campaign.status = CampaignStatus.ACTIVE.value
        
        # Record state change
        self._record_state_change(campaign_id, old_status, campaign.status)
        
        # Log action
        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_LAUNCH,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )
        
        self.db.flush()
        return campaign
    
    def complete(self, campaign_id: str) -> Optional[Campaign]:
        """Mark a campaign as completed."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        
        old_status = campaign.status
        campaign.status = CampaignStatus.COMPLETED.value
        
        self._record_state_change(campaign_id, old_status, campaign.status)
        self.db.flush()
        
        return campaign
    
    # =============================
    # FILTERING & SEARCH
    # =============================
    
    
    # =============================
    # HELPER METHODS
    # =============================
    
    def _record_state_change(
        self,
        campaign_id: str,
        from_state: str,
        to_state: str,
        reason: Optional[str] = None
    ):
        """Record a campaign state transition."""
        event = CampaignStateEvent(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
        self.db.add(event)
    
    def _get_creator_name(self, user_id: str) -> Optional[str]:
        """Get creator's full name."""
        user = self.db.query(User).filter(User.user_id == user_id).first()
        return user.full_name if user else None
    
    def _get_sequence_count(self, campaign_id: str) -> int:
        """Get number of sequence steps."""
        return self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar() or 0
    
    def _get_prospect_count(self, campaign_id: str) -> int:
        """Get number of enrolled prospects."""
        return self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id
        ).scalar() or 0
    
    
    



    def _preschedule_all_emails(self, campaign_id: str) -> int:
        """
        Pre-generate EmailMessage records for all sequence steps for all enrolled prospects.
        Called during campaign activation to show full schedule upfront.
        
        Returns:
            Number of messages created
        """
        import logging
        logger = logging.getLogger(__name__)
        
        print(f"[PRESCHEDULE] Starting for campaign {campaign_id}")
        
        # Fetch the campaign to get timezone and send window settings
        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()
        
        if not campaign:
            logger.error(f"[PRESCHEDULE] Campaign {campaign_id} not found")
            return 0
        
        # Get all enrolled (ACTIVE) prospects for this campaign
        prospects = self.db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status == "ACTIVE"
        ).all()
        
        print(f"[PRESCHEDULE] Found {len(prospects)} active prospects")
        
        if not prospects:
            return 0
        
        # Get all sequences ordered by step_number
        sequences = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).order_by(EmailSequence.step_number).all()
        
        print(f"[PRESCHEDULE] Found {len(sequences)} sequences")
        
        if not sequences:
            return 0
        
        # Flush to ensure auto-approved templates are visible
        self.db.flush()
        
        # Get all templates for this campaign (for matching)
        templates = self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.approved_by != None  # Only approved templates
        ).all()
        
        print(f"[PRESCHEDULE] Found {len(templates)} approved templates")
        
        # Build template lookup: (sequence_id, designation) -> template
        template_map = {}
        for t in templates:
            key = (t.sequence_id, t.designation)
            template_map[key] = t
            # Also add fallback with None designation
            if t.designation is None or t.designation == "":
                template_map[(t.sequence_id, None)] = t
        
        base_time = datetime.utcnow()
        if campaign.start_date:
            try:
                import zoneinfo
                # The frontend start_date is just YYYY-MM-DD. 
                # We need to consider the start of the day in that timezone.
                tz = zoneinfo.ZoneInfo(campaign.campaign_timezone or "UTC")
                start_dt = datetime.combine(campaign.start_date, datetime.min.time(), tzinfo=tz)
                start_utc = start_dt.astimezone(zoneinfo.ZoneInfo("UTC")).replace(tzinfo=None)
                # If start_utc is in the past, fall back to now to avoid immediate blast.
                if start_utc > base_time:
                    base_time = start_utc
            except Exception as e:
                logger.error(f"Error parsing campaign start date: {e}")
                
        messages_created = 0
        active_inboxes = [inbox for inbox in (campaign.inboxes or []) if (inbox.status or "").upper() == "ACTIVE"]

        for prospect_index, cp in enumerate(prospects):
            # Get prospect details for personalization
            prospect = self.db.query(Prospect).filter(
                Prospect.prospect_id == cp.prospect_id
            ).first()
            
            if not prospect:
                continue
            
            # Uses the campaign's specific timezone and send windows
            from app.utils.business_calendar import calculate_send_time_with_window
            
            cumulative_wait_days = 0
            assigned_inbox = active_inboxes[prospect_index % len(active_inboxes)] if active_inboxes else None
            
            for seq in sequences:
                # Accumulate wait days for all steps
                cumulative_wait_days += seq.wait_days
                
                # Determine timezone - respect prospect state if configured
                prospect_timezone = None
                if campaign.respect_timezone and prospect.poc_state:
                    from app.utils.business_calendar import get_timezone_for_state
                    prospect_timezone = get_timezone_for_state(prospect.poc_state, campaign.campaign_timezone)
                
                scheduled_at = calculate_send_time_with_window(
                    base_time=base_time,
                    wait_days=cumulative_wait_days,
                    state=None,  # We resolve timezone manually below
                    timezone_str=prospect_timezone or campaign.campaign_timezone,
                    send_window_start=campaign.send_window_start,
                    send_window_end=campaign.send_window_end,
                    add_jitter=True
                )
                
                # Find matching template
                template = template_map.get((seq.sequence_id, prospect.designation))
                if not template:
                    template = template_map.get((seq.sequence_id, None))
                
                # Fallback: use any template for this sequence
                if not template:
                    for key, t in template_map.items():
                        if key[0] == seq.sequence_id:
                            template = t
                            break
                
                if not template:
                    # Skip if no template found for this sequence
                    continue
                
                # Create personalized message
                final_subject = template.subject or f"[Step {seq.step_number}] Email"
                final_body = template.body or ""
                if prospect.first_name:
                    final_body = final_body.replace("{first_name}", prospect.first_name)
                    final_body = final_body.replace("{{first_name}}", prospect.first_name)
                
                email_msg = EmailMessage(
                    message_id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    prospect_id=cp.prospect_id,
                    sequence_id=seq.sequence_id,
                    inbox_id=assigned_inbox.inbox_id if assigned_inbox else None,
                    from_email=assigned_inbox.email_address if assigned_inbox else settings.SENDER_EMAIL,
                    to_email=prospect.email,
                    subject=final_subject,
                    body_text=final_body,
                    status="SCHEDULED",
                    scheduled_at=scheduled_at,
                    sent_at=None,
                    template_id=template.template_id
                )
                self.db.add(email_msg)
                messages_created += 1
        
        print(f"[PRESCHEDULE] Created {messages_created} email messages")
        return messages_created