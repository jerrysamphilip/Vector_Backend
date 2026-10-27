# app/services/campaign_email_service.py
"""
Campaign email service: prospect enrollment, stub generation,
placeholder substitution, and pre-scheduling of email messages.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from typing import List, Optional, Tuple
from datetime import datetime, timedelta
import uuid
import zoneinfo

from app.core.clock import utcnow
from app.core.config import settings

from app.models.campaign import Campaign, CampaignProspect, CampaignStateEvent
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.email_sequence import EmailSequence
from app.models.email_message import EmailMessage
from app.models.email_template import EmailTemplate
from app.schemas.campaign_schema import CampaignStatus, CampaignEnrollmentRequest
from app.domain.campaigns import REMOVED_BY_USER
from app.services import enrollment_rules
from app.services.audit_service import AuditService
from app.services.inbox_rotation import InboxRotation
from app.services.send_guards import require_active_inbox
from app.utils.email_utils import (
    normalize_unsubscribe_footer,
    strip_cta_content_no_link,
    has_effective_cta_link,
    normalize_cta_link,
    finalize_email_body,
    build_signature_block,
)
import logging

logger = logging.getLogger(__name__)


__all__ = ["CampaignEmailService", "REMOVED_BY_USER"]


def _pick_template(template_map: dict, sequence_id: str, persona_type: Optional[str]) -> Optional[EmailTemplate]:
    """The step's template for the persona, else its generic one, else any of the step's templates."""
    template = template_map.get((sequence_id, persona_type)) or template_map.get((sequence_id, None))
    if template:
        return template
    return next((t for key, t in template_map.items() if key[0] == sequence_id), None)


class CampaignEmailService:
    """
    Handles all email-related campaign operations:
    - Prospect enrollment
    - Stub content generation
    - Campaign launch (auto-approve + pre-schedule)
    - Placeholder substitution
    """

    def __init__(self, db: Session) -> None:
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

    def _screen_request(
        self, campaign: Campaign, request: CampaignEnrollmentRequest, skip_ids: Optional[set] = None,
    ) -> Tuple[List[Prospect], List[dict]]:
        """The campaign page's enrolment rules for a request: (eligible, rejections). Reads only."""
        candidate_ids = list(request.prospect_ids or [])
        if request.list_ids:
            candidate_ids.extend(enrollment_rules.list_member_ids(self.db, campaign.tenant_id, request.list_ids))
        if not candidate_ids:
            return [], []
        prospects, rejections = enrollment_rules.load_tenant_prospects(self.db, campaign.tenant_id, candidate_ids)
        cooloff_ids = enrollment_rules.sent_within(self.db, [p.prospect_id for p in prospects], hours=24)
        eligible, screened_out = enrollment_rules.screen(
            self.db, campaign.tenant_id, campaign.campaign_id, prospects, recently_contacted=cooloff_ids,
            skip_ids=skip_ids, allow_reenrol=True)
        return eligible, rejections + screened_out

    def check_enrollment(self, campaign: Campaign, request: CampaignEnrollmentRequest) -> dict:
        """Dry run of enroll_prospects_with_report (S-20): same rules, nothing written."""
        eligible, rejections = self._screen_request(campaign, request)
        return enrollment_rules.check(eligible, rejections)

    def enroll_prospects_with_report(
        self,
        campaign_id: str,
        user_id: str,
        request: CampaignEnrollmentRequest,
        skip_ids: Optional[set] = None,
    ) -> Tuple[int, List[dict]]:
        """
        Enroll prospects into campaign (Stage 3 Gate). Returns (enrolled_count, rejections),
        with a reason for every contact not enrolled (app/services/enrollment_rules.py).
        ``skip_ids``: contacts never to enrol here (a duplicate skipping its source's contacts).
        A contact removed earlier and never emailed is re-enrolled on its kept row (S-21).
        """
        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()
        if not campaign:
            raise ValueError("Campaign not found")

        if campaign.status not in [CampaignStatus.DRAFT.value, CampaignStatus.PAUSED.value, CampaignStatus.ACTIVE.value]:
            raise ValueError("Cannot enroll prospects in current campaign status")

        eligible, rejections = self._screen_request(campaign, request, skip_ids)
        if not eligible:
            return 0, rejections

        # Calculate Schedule Start
        step1 = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == 1
        ).first()
        start_delay_days = step1.wait_days if step1 else 0
        scheduled_time = utcnow() + timedelta(days=start_delay_days)

        reactivated = enrollment_rules.reactivate_removed(
            self.db, campaign.tenant_id, campaign_id, [p.prospect_id for p in eligible], scheduled_time)
        new_enrollments = [
            CampaignProspect(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                prospect_id=prospect.prospect_id,
                current_step=1,
                status="ACTIVE",
                enrolled_at=utcnow(),
                next_scheduled_at=scheduled_time,
            )
            for prospect in eligible if prospect.prospect_id not in reactivated
        ]
        enrolled_count = len(eligible)

        if new_enrollments:
            self.db.bulk_save_objects(new_enrollments)
        self.db.flush()

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
        if (campaign_prospect.status == "PAUSED"
                and campaign_prospect.stopped_reason == REMOVED_BY_USER):
            return False  # already removed: a repeated DELETE is a 404

        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]),
        ).update(
            {"status": "CANCELLED", "failure_reason": REMOVED_BY_USER},
            synchronize_session=False,
        )

        set_prospect_status(campaign_prospect, "PAUSED", stopped_reason=REMOVED_BY_USER)

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

        from app.services.sender_identity import campaign_sender_name
        sender_name = campaign_sender_name(self.db, campaign)

        updated = 0
        for msg in pending_messages:
            prospect = prospect_map.get(msg.prospect_id)
            if not prospect:
                continue

            # An email in a subject A/B test keeps its variant's subject (BR-SF-18);
            # only its body follows the template.
            final_subject = msg.subject if msg.subject_variant else self.substitute_placeholders(
                template.subject or "", prospect, sender_name, campaign.cta_link, campaign.sender_title
            )
            final_body = self.substitute_placeholders(
                template.body or "", prospect, sender_name, campaign.cta_link, campaign.sender_title
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

        # Campaign mail only goes from the campaign's own mailboxes, never the platform address
        require_active_inbox(campaign, "launch")

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
            t.approved_at = utcnow()

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
    ) -> None:
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

        logger.info("[PRESCHEDULE] Starting for campaign %s", campaign_id)

        campaign = self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()

        if not campaign:
            logger.error("[PRESCHEDULE] Campaign %s not found", campaign_id)
            return 0

        prospects = self.db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status == "ACTIVE"
        ).all()

        logger.info("[PRESCHEDULE] Found %s active prospects", len(prospects))

        if not prospects:
            return 0

        sequences = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).order_by(EmailSequence.step_number).all()

        logger.info("[PRESCHEDULE] Found %s sequences", len(sequences))

        if not sequences:
            return 0

        # Flush to ensure auto-approved templates are visible
        self.db.flush()

        # Get all templates for this campaign (for matching)
        templates = self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id
        ).all()

        logger.info("[PRESCHEDULE] Found %s templates", len(templates))

        # Build template lookup: (sequence_id, designation) -> template
        template_map = {}
        for t in templates:
            key = (t.sequence_id, t.designation)
            template_map[key] = t
            if t.designation is None or t.designation == "":
                template_map[(t.sequence_id, None)] = t

        base_time = self._schedule_base_time(campaign)

        from app.services.sender_identity import campaign_sender_name
        sender_name = campaign_sender_name(self.db, campaign)

        messages_created = 0
        new_messages: List[EmailMessage] = []
        # Keep ordering stable so spread/batch scheduling is deterministic.
        prospects = sorted(prospects, key=lambda cp: (cp.prospect_id or ""))
        total_prospects = len(prospects)
        active_inboxes = [inbox for inbox in (campaign.inboxes or []) if (inbox.status or "").upper() == "ACTIVE"]
        # Contacts are shared out by each mailbox's effective daily limit (S-18, inbox_rotation.py);
        # a contact keeps the mailbox an earlier step of theirs was already bound to (sticky sender)
        rotation = InboxRotation(active_inboxes)
        bound_inbox = self._bound_inboxes(campaign_id, {i.inbox_id: i for i in active_inboxes})

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

        # A step email cancelled because the user removed the contact does not count: a contact
        # re-enrolled after removal (never emailed, S-21) gets its steps scheduled again
        existing_message_keys = {
            (pid, sid) for pid, sid, status, reason in self.db.query(
                EmailMessage.prospect_id, EmailMessage.sequence_id, EmailMessage.status,
                EmailMessage.failure_reason,
            ).filter(EmailMessage.campaign_id == campaign_id).all()
            if not (status == "CANCELLED" and reason == REMOVED_BY_USER)
        }

        from app.models.prospect_persona import ProspectPersona
        persona_by_prospect_id = {
            pp.prospect_id: pp.persona_type
            for pp in self.db.query(ProspectPersona).filter(
                ProspectPersona.prospect_id.in_(prospect_ids)
            ).all()
        }

        # A/B subject test (BR-SF-18): pick each new email's subject variant up
        # front, balanced against the step's existing assignments. Emails that
        # already exist keep theirs (the idempotency guard below skips them).
        from app.services.subject_variant_service import SubjectVariantService
        needed_by_seq: dict = {}
        for cp in prospects:
            if cp.prospect_id not in prospect_by_id:
                continue
            for seq in sequences:
                if (cp.prospect_id, seq.sequence_id) not in existing_message_keys:
                    needed_by_seq.setdefault(seq.sequence_id, []).append(cp.prospect_id)
        variant_by_key = SubjectVariantService(self.db).assignments_for_new_messages(campaign_id, needed_by_seq)

        for prospect_index, cp in enumerate(prospects):
            prospect = prospect_by_id.get(cp.prospect_id)

            if not prospect:
                continue

            cumulative_wait_days = 0
            prev_scheduled_at = None  # planned time of this prospect's previous step
            assigned_inbox = None
            if active_inboxes and any((cp.prospect_id, seq.sequence_id) not in existing_message_keys
                                      for seq in sequences):
                assigned_inbox = bound_inbox.get(cp.prospect_id) or rotation.next()

            # Daily batch throttle: shift prospect's base_time by N business days
            # so the first batch goes day 1, second batch goes day 2, etc.
            if campaign.daily_batch_size and campaign.daily_batch_size > 0:
                from app.utils.business_calendar import batch_day_start
                day_offset = prospect_index // campaign.daily_batch_size
                prospect_base_time = batch_day_start(base_time, day_offset)
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
                # A follow-up's gap counts from the previous step's planned time, the
                # same rule the scheduler applies after a real send (B01): when step 1
                # was pushed (weekend/holiday launch, send window), later steps move too.
                if prev_scheduled_at is not None:
                    step_base_time = max(step_base_time,
                                         prev_scheduled_at + timedelta(days=seq.wait_days or 0))
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
                prev_scheduled_at = scheduled_at

                from app.services.ai_email_service import classify_prospect

                persona_type = persona_by_prospect_id.get(prospect.prospect_id) or classify_prospect(
                    prospect.designation or "", prospect.company_name)[0]
                template = _pick_template(template_map, seq.sequence_id, persona_type)
                if not template:
                    continue

                variant = variant_by_key.get((cp.prospect_id, seq.sequence_id))
                final_subject = self.substitute_placeholders(
                    (variant.subject if variant else None) or template.subject or f"[Step {seq.step_number}] Email",
                    prospect,
                    sender_name,
                    campaign.cta_link,
                    campaign.sender_title
                )
                final_body = self.substitute_placeholders(
                    template.body or "",
                    prospect,
                    sender_name,
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
                    # No active inbox: no sender yet (never the platform address); the
                    # scheduler binds one when the campaign has an active inbox again
                    from_email=assigned_inbox.email_address if assigned_inbox else None,
                    to_email=prospect.email,
                    subject=final_subject,
                    body_text=final_body,
                    status="SCHEDULED",
                    scheduled_at=scheduled_at,
                    sent_at=None,
                    template_id=template.template_id,
                    subject_variant=variant.label if variant else None,
                )
                new_messages.append(email_msg)
                messages_created += 1

        messages_created = self._insert_new_messages(new_messages)
        logger.info("[PRESCHEDULE] Created %s messages", messages_created)
        return messages_created

    @staticmethod
    def _schedule_base_time(campaign: Campaign) -> datetime:
        """Now, or the campaign's start date (midnight in its timezone, as naive UTC) when later."""
        base_time = utcnow()
        if campaign.start_date:
            try:
                tz = zoneinfo.ZoneInfo(campaign.campaign_timezone or "UTC")
                start_dt = datetime.combine(campaign.start_date, datetime.min.time(), tzinfo=tz)
                start_utc = start_dt.astimezone(zoneinfo.ZoneInfo("UTC")).replace(tzinfo=None)
                if start_utc > base_time:
                    base_time = start_utc
            except Exception as e:
                logger.error("Error parsing campaign start date: %s", e)
        return base_time

    def _bound_inboxes(self, campaign_id: str, active_by_id: dict) -> dict:
        """{prospect_id: active mailbox} of contacts that already have a step email bound to one."""
        if not active_by_id:
            return {}
        rows = self.db.query(EmailMessage.prospect_id, EmailMessage.inbox_id).filter(
            EmailMessage.campaign_id == campaign_id, EmailMessage.inbox_id.in_(list(active_by_id)),
        ).order_by(EmailMessage.sent_at.is_(None), EmailMessage.sent_at.desc()).all()
        out: dict = {}
        for pid, inbox_id in rows:
            out.setdefault(pid, active_by_id[inbox_id])
        return out

    def _insert_new_messages(self, messages: List[EmailMessage]) -> int:
        """
        Insert pre-scheduled step emails, tolerating the one-email-per-step key (item 19): a
        concurrent pre-schedule (double launch, enrolment during launch) may already have
        written some of them. All rows go in one savepoint; if the key refuses one, each row is
        retried in its own savepoint and the duplicates are skipped. Returns how many were added.
        """
        if not messages:
            self.db.flush()
            return 0
        try:
            with self.db.begin_nested():
                self.db.add_all(messages)
        except IntegrityError:
            logger.warning("[PRESCHEDULE] Some steps already have their email; inserting one by one")
        else:
            return len(messages)
        created = 0
        for msg in messages:
            try:
                with self.db.begin_nested():
                    self.db.add(msg)
                created += 1
            except IntegrityError:
                logger.info("[PRESCHEDULE] Skipped duplicate step email for prospect %s, step %s",
                            msg.prospect_id, msg.sequence_id)
        return created
