# app/services/campaign_email_service.py
"""
Campaign email service: prospect enrollment, stub generation,
placeholder substitution, and pre-scheduling of email messages.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import Optional
from datetime import datetime, timedelta
import uuid

from app.core.config import settings

from app.models.campaign import Campaign, CampaignProspect, CampaignStateEvent
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.email_sequence import EmailSequence
from app.models.email_message import EmailMessage
from app.models.email_template import EmailTemplate
from app.schemas.campaign_schema import CampaignStatus, CampaignEnrollmentRequest
from app.services.audit_service import AuditService
from app.utils.email_utils import (
    normalize_unsubscribe_footer,
    strip_cta_content_no_link,
    has_effective_cta_link,
    normalize_cta_link,
    finalize_email_body,
    build_signature_block,
)


class CampaignEmailService:
    """
    Handles all email-related campaign operations:
    - Prospect enrollment
    - Stub content generation
    - Campaign launch (auto-approve + pre-schedule)
    - Placeholder substitution
    """

    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    # =============================
    # PLACEHOLDER SUBSTITUTION
    # =============================

    def substitute_placeholders(
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

        # The sending tenant's own company name (never another tenant's)
        from app.services.sender_identity import get_sender_identity
        our_company = get_sender_identity(getattr(prospect, "tenant_id", None)).company_name

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
            "{{signature_block}}": build_signature_block(sender_name, sender_title, our_company),
            "{{our_company}}": our_company or "",
            "{{calendar_link}}": normalized_cta_link,
            "{{cta_link}}": normalized_cta_link,
        }

        result = template
        for placeholder, value in substitutions.items():
            result = result.replace(placeholder, str(value))
            result = result.replace(placeholder.lower(), str(value))
            result = result.replace(placeholder.upper(), str(value))

        return result

    # =============================
    # ENROLLMENT
    # =============================

    def generate_stub_content(self, campaign_id: str, user_id: str) -> int:
        """
        Stage 4 Stub: Generate placeholder templates for all steps.
        Groups by 'designation' of enrolled prospects to create persona-based templates.
        """
        sequences = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).all()
        if not sequences:
            raise ValueError("No sequence steps defined")

        designations = [None]  # Default generic

        enrolled_designations = self.db.query(Prospect.designation).join(
            CampaignProspect, Prospect.prospect_id == CampaignProspect.prospect_id
        ).filter(
            CampaignProspect.campaign_id == campaign_id
        ).distinct().all()

        if enrolled_designations:
            fetched = [d[0] for d in enrolled_designations]
            if fetched:
                designations = fetched

        count = 0
        for designation in designations:
            for seq in sequences:
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
                    persona_label = designation if designation else "General"
                    subject = f"[{persona_label}] Subject for Step {seq.step_number}"
                    body = f"Hi {{{{first_name}}}},\n\nThis is a placeholder {persona_label} email for step {seq.step_number}."

                    tmpl = EmailTemplate(
                        template_id=str(uuid.uuid4()),
                        campaign_id=campaign_id,
                        sequence_id=seq.sequence_id,
                        designation=designation,
                        subject=subject,
                        body=body,
                        is_ai_generated=False,
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
        return self.enroll_prospects_with_report(campaign_id, user_id, request)[0]

    def enroll_prospects_with_report(
        self,
        campaign_id: str,
        user_id: str,
        request: CampaignEnrollmentRequest
    ):
        """
        Enroll prospects into campaign (Stage 3 Gate). Returns (enrolled_count, rejections),
        with a reason for every contact not enrolled (app/services/enrollment_rules.py):
        global unsubscribe, opted out, invalid email, already enrolled, or an email sent
        in the last 24h (cool-off).
        """
        from app.services import enrollment_rules

        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()
        if not campaign:
            raise ValueError("Campaign not found")

        if campaign.status not in [CampaignStatus.DRAFT.value, CampaignStatus.PAUSED.value, CampaignStatus.ACTIVE.value]:
            raise ValueError("Cannot enroll prospects in current campaign status")

        # 1. Gather candidates, scoped to the campaign's workspace
        candidate_ids = list(request.prospect_ids or [])
        if request.list_ids:
            candidate_ids.extend(enrollment_rules.list_member_ids(self.db, campaign.tenant_id, request.list_ids))

        if not candidate_ids:
            return 0, []

        prospects, rejections = enrollment_rules.load_tenant_prospects(self.db, campaign.tenant_id, candidate_ids)
        cooloff_ids = enrollment_rules.sent_within(self.db, [p.prospect_id for p in prospects], hours=24)
        eligible, screened_out = enrollment_rules.screen(
            self.db, campaign.tenant_id, campaign_id, prospects, recently_contacted=cooloff_ids)
        rejections.extend(screened_out)

        # Calculate Schedule Start
        step1 = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == 1
        ).first()
        start_delay_days = step1.wait_days if step1 else 0
        scheduled_time = datetime.utcnow() + timedelta(days=start_delay_days)

        new_enrollments = []
        enrolled_count = 0

        for prospect in eligible:
            new_enrollments.append(CampaignProspect(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                prospect_id=prospect.prospect_id,
                current_step=1,
                status="ACTIVE",
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

            # Campaign already launched: _preschedule_all_emails() normally only
            # runs once, at launch(). Without this, prospects enrolled after
            # launch get a CampaignProspect row but no EmailMessage rows are
            # ever created for them, so the scheduler (which only ever reads
            # pre-existing EmailMessage rows) silently never emails them.
            # The idempotency guard inside _preschedule_all_emails skips
            # prospects/steps that already have a message, so this only backfills
            # the newly enrolled prospects.
            if campaign.status in (CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value):
                self._preschedule_all_emails(campaign_id)

        return enrolled_count, rejections

    def remove_prospect(self, campaign_id: str, prospect_id: str, user_id: str) -> bool:
        """
        Remove a prospect from a campaign (post-launch prospect-list editing).

        Cancels any not-yet-sent EmailMessage rows for this prospect in this
        campaign so no further emails go out, and marks the enrollment as
        PAUSED (via the status-priority ladder, so a more important status
        like BOUNCED/REPLIED/UNSUBSCRIBED already recorded is never downgraded).
        The CampaignProspect row is kept (not deleted) to preserve history.
        """
        from app.utils.campaign_prospect_status import set_prospect_status

        campaign = self.db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
        if not campaign:
            return False

        campaign_prospect = self.db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.prospect_id == prospect_id,
        ).first()
        if not campaign_prospect:
            return False

        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]),
        ).update(
            {"status": "CANCELLED", "failure_reason": "Removed from campaign by user"},
            synchronize_session=False,
        )

        set_prospect_status(campaign_prospect, "PAUSED", stopped_reason="Removed from campaign by user")

        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=f"{AuditService.ACTION_UPDATE}_REMOVED_PROSPECT",
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )

        self.db.flush()
        return True

    def propagate_template_edit(self, template_id: str) -> int:
        """
        Re-render subject/body for not-yet-sent EmailMessage snapshots tied to
        this template.

        EmailMessage.subject/body_text are substituted once, per-prospect, at
        launch time (_preschedule_all_emails) and the scheduler always trusts
        that snapshot over the live template (see email_sender_service.py).
        Without this, editing a template after launch has no visible effect
        on emails that haven't gone out yet.
        """
        template = self.db.query(EmailTemplate).filter(
            EmailTemplate.template_id == template_id
        ).first()
        if not template or not template.campaign_id:
            return 0

        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == template.campaign_id
        ).first()
        if not campaign:
            return 0

        pending_messages = self.db.query(EmailMessage).filter(
            EmailMessage.template_id == template_id,
            EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]),
        ).all()
        if not pending_messages:
            return 0

        prospect_ids = {m.prospect_id for m in pending_messages}
        prospects = self.db.query(Prospect).filter(Prospect.prospect_id.in_(prospect_ids)).all()
        prospect_map = {p.prospect_id: p for p in prospects}

        updated = 0
        for msg in pending_messages:
            prospect = prospect_map.get(msg.prospect_id)
            if not prospect:
                continue

            final_subject = self.substitute_placeholders(
                template.subject or "", prospect, campaign.sender_name, campaign.cta_link, campaign.sender_title
            )
            final_body = self.substitute_placeholders(
                template.body or "", prospect, campaign.sender_name, campaign.cta_link, campaign.sender_title
            )
            if not has_effective_cta_link(campaign.cta_link):
                final_body = strip_cta_content_no_link(final_body)
            final_body = finalize_email_body(final_body)

            msg.subject = final_subject
            msg.body_text = final_body
            updated += 1

        self.db.flush()
        return updated

    # =============================
    # LAUNCH / PRE-SCHEDULING
    # =============================

    def launch(self, campaign_id: str, user_id: str) -> Optional[Campaign]:
        """
        Launch a draft campaign (set to ACTIVE).
        Auto-approves all templates then pre-schedules all emails.
        """
        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()
        if not campaign:
            return None

        if campaign.status != CampaignStatus.DRAFT.value:
            raise ValueError(f"Cannot launch campaign with status: {campaign.status}")

        # CAN-SPAM: every email must carry the sender's own postal address, so a
        # tenant without one cannot start new sending (400 with the reason)
        from app.services.sender_identity import require_postal_address
        require_postal_address(self.db, campaign.tenant_id)

        # Validation Gates
        step_count = self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar()
        if step_count == 0:
            raise ValueError("Cannot launch campaign without sequence steps")

        tmpl_count = self.db.query(func.count(EmailTemplate.template_id)).filter(
            EmailTemplate.campaign_id == campaign_id
        ).scalar()
        if tmpl_count < step_count:
            raise ValueError(f"Missing email templates. Defined: {step_count}, Found: {tmpl_count}")

        # Auto-approve all templates
        unapproved_templates = self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.approved_by == None
        ).all()

        for t in unapproved_templates:
            t.approved_by = user_id
            t.approved_at = datetime.utcnow()

        self.db.flush()

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

        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()

        if not campaign:
            logger.error(f"[PRESCHEDULE] Campaign {campaign_id} not found")
            return 0

        prospects = self.db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status == "ACTIVE"
        ).all()

        print(f"[PRESCHEDULE] Found {len(prospects)} active prospects")

        if not prospects:
            return 0

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
            EmailTemplate.campaign_id == campaign_id
        ).all()

        print(f"[PRESCHEDULE] Found {len(templates)} templates")

        # Build template lookup: (sequence_id, designation) -> template
        template_map = {}
        for t in templates:
            key = (t.sequence_id, t.designation)
            template_map[key] = t
            if t.designation is None or t.designation == "":
                template_map[(t.sequence_id, None)] = t

        base_time = datetime.utcnow()
        if campaign.start_date:
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(campaign.campaign_timezone or "UTC")
                start_dt = datetime.combine(campaign.start_date, datetime.min.time(), tzinfo=tz)
                start_utc = start_dt.astimezone(zoneinfo.ZoneInfo("UTC")).replace(tzinfo=None)
                if start_utc > base_time:
                    base_time = start_utc
            except Exception as e:
                logger.error(f"Error parsing campaign start date: {e}")

        messages_created = 0
        # Keep ordering stable so spread/batch scheduling is deterministic.
        prospects = sorted(prospects, key=lambda cp: (cp.prospect_id or ""))
        total_prospects = len(prospects)
        active_inboxes = [inbox for inbox in (campaign.inboxes or []) if (inbox.status or "").upper() == "ACTIVE"]

        # Batch-load everything the per-prospect/per-step loop below needs, so
        # launching a campaign with hundreds of prospects doesn't issue
        # thousands of individual round trips (prospect lookup + idempotency
        # check + persona lookup, each previously queried one row at a time
        # inside the loop). Over the network to a remote DB that was slow
        # enough to blow past read/write timeouts and surface as
        # "Database unavailable" on launch.
        prospect_ids = [cp.prospect_id for cp in prospects]

        prospect_by_id = {
            p.prospect_id: p
            for p in self.db.query(Prospect).filter(Prospect.prospect_id.in_(prospect_ids)).all()
        }

        existing_message_keys = {
            (pid, sid) for pid, sid in self.db.query(
                EmailMessage.prospect_id, EmailMessage.sequence_id
            ).filter(EmailMessage.campaign_id == campaign_id).all()
        }

        from app.models.prospect_persona import ProspectPersona
        persona_by_prospect_id = {
            pp.prospect_id: pp.persona_type
            for pp in self.db.query(ProspectPersona).filter(
                ProspectPersona.prospect_id.in_(prospect_ids)
            ).all()
        }

        for prospect_index, cp in enumerate(prospects):
            prospect = prospect_by_id.get(cp.prospect_id)

            if not prospect:
                continue

            cumulative_wait_days = 0
            assigned_inbox = active_inboxes[prospect_index % len(active_inboxes)] if active_inboxes else None

            # Daily batch throttle: shift prospect's base_time by N business days
            # so the first batch goes day 1, second batch goes day 2, etc.
            if campaign.daily_batch_size and campaign.daily_batch_size > 0:
                from app.utils.business_calendar import add_business_days
                day_offset = prospect_index // campaign.daily_batch_size
                prospect_base_time = add_business_days(base_time, day_offset) if day_offset > 0 else base_time
            else:
                prospect_base_time = base_time

            for seq in sequences:
                cumulative_wait_days += seq.wait_days

                # Idempotency guard
                if (cp.prospect_id, seq.sequence_id) in existing_message_keys:
                    continue

                prospect_timezone = None
                if campaign.respect_timezone and prospect.poc_state:
                    from app.utils.business_calendar import get_timezone_for_state
                    prospect_timezone = get_timezone_for_state(prospect.poc_state, campaign.campaign_timezone)

                from app.utils.business_calendar import schedule_email_in_window

                # Per-step base time (after cumulative waits). The window planner then
                # places each prospect inside the configured send window/mode.
                step_base_time = prospect_base_time + timedelta(days=cumulative_wait_days)
                scheduled_at = schedule_email_in_window(
                    base_utc=step_base_time,
                    timezone_str=prospect_timezone or campaign.campaign_timezone,
                    send_window_start=campaign.send_window_start,
                    send_window_end=campaign.send_window_end,
                    sending_mode=(campaign.sending_mode or "spread"),
                    prospect_index=prospect_index,
                    total_prospects=total_prospects,
                    min_gap_minutes=max(int(campaign.min_gap_minutes or 0), 0),
                    batch_size=campaign.batch_size,
                    batch_gap_minutes=max(int(campaign.batch_gap_minutes or 30), 1),
                )

                from app.services.ai_email_service import classify_prospect

                existing_persona_type = persona_by_prospect_id.get(prospect.prospect_id)
                if existing_persona_type:
                    persona_type = existing_persona_type
                else:
                    persona_type, _ = classify_prospect(prospect.designation or "", prospect.company_name)

                template = template_map.get((seq.sequence_id, persona_type))
                if not template:
                    template = template_map.get((seq.sequence_id, None))

                if not template:
                    for key, t in template_map.items():
                        if key[0] == seq.sequence_id:
                            template = t
                            break

                if not template:
                    continue

                final_subject = self.substitute_placeholders(
                    template.subject or f"[Step {seq.step_number}] Email",
                    prospect,
                    campaign.sender_name,
                    campaign.cta_link,
                    campaign.sender_title
                )
                final_body = self.substitute_placeholders(
                    template.body or "",
                    prospect,
                    campaign.sender_name,
                    campaign.cta_link,
                    campaign.sender_title
                )
                if not has_effective_cta_link(campaign.cta_link):
                    final_body = strip_cta_content_no_link(final_body)
                # final_body = normalize_unsubscribe_footer(final_body)
                final_body = finalize_email_body(final_body)

                from app.models.sending_inbox import SendingInbox
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

        self.db.flush()
        print(f"[PRESCHEDULE] Created {messages_created} messages")
        return messages_created
