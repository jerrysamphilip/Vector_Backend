# app/services/campaign_email_service.py
"""
Campaign email service: prospect enrollment, stub generation, placeholder substitution, launch and the
pre-scheduling of every step email (the per-contact snapshot the sender sends).

SQL lives in the campaign aggregate's repositories (app/repositories/campaigns*.py); rendering in
app/services/email_render.py; the scheduling rules in app/domain/campaigns_edit.py. The methods here run
inside the caller's unit of work (the route-facing services commit); none of them commits.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from app.core.clock import utcnow
from app.core.config import settings
from app.domain.campaigns import REMOVED_BY_USER, missing_templates_message, uncovered_steps
from app.domain.campaigns_edit import (ScheduleSettings, build_template_map, contact_timezone, launch_times,
                                       passed_step, pick_template, schedule_base)
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailMessage
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.schemas.campaign_schema import CampaignEnrollmentRequest, CampaignStatus
from app.services import enrollment_rules
from app.services.ai_email_service import classify_prospect
from app.services.audit_service import AuditService
from app.services.campaign_calendar import schedule_settings
from app.services.campaign_common import CampaignServiceBase, record_state_change
from app.services.email_render import RenderContext, render_body, render_step_email, substitute_placeholders
from app.services.inbox_rotation import InboxRotation
from app.services.send_guards import active_inboxes, require_active_inbox
from app.services.sender_identity import campaign_sender_name, get_sender_identity, require_postal_address
from app.services.subject_variant_service import SubjectVariantService
from app.utils.business_calendar import batch_day_start
from app.utils.campaign_prospect_status import set_prospect_status

logger = logging.getLogger(__name__)

__all__ = ["CampaignEmailService", "REMOVED_BY_USER", "render_context"]

PENDING_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")


def render_context(db: Any, campaign: Campaign) -> RenderContext:
    """The campaign's sender values for rendering (sender name falls back to the creator, as the sender does)."""
    return RenderContext(
        sender_name=campaign_sender_name(db, campaign), sender_title=campaign.sender_title,
        cta_link=campaign.cta_link, our_company=get_sender_identity(campaign.tenant_id).company_name,
        default_sender_name=settings.SENDER_NAME,
    )


@dataclass
class _Preschedule:
    """Everything the per-contact loop of a pre-schedule needs, loaded once."""

    campaign: Campaign
    steps: list[Any]
    template_map: dict
    contacts: dict[str, Prospect]
    existing: set
    personas: dict[str, str]
    variants: dict
    ctx: RenderContext
    settings: ScheduleSettings
    base_time: datetime
    rotation: InboxRotation
    bound: dict
    has_inboxes: bool
    sent_steps: dict[str, set] = field(default_factory=dict)
    skip_passed: bool = False


class CampaignEmailService(CampaignServiceBase):
    """
    Handles all email-related campaign operations:
    - Prospect enrollment
    - Stub content generation
    - Campaign launch (auto-approve + pre-schedule)
    - Placeholder substitution
    """

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
        """Replace placeholders in template with prospect data (app/services/email_render.py)."""
        if not template:
            return template
        # The sending tenant's own company name (never another tenant's)
        our_company = get_sender_identity(getattr(prospect, "tenant_id", None)).company_name
        return substitute_placeholders(template, prospect, RenderContext(
            sender_name=sender_name, sender_title=sender_title, cta_link=cta_link, our_company=our_company,
            default_sender_name=settings.SENDER_NAME))

    def render_context(self, campaign: Campaign) -> RenderContext:
        return render_context(self.uow.session, campaign)

    # =============================
    # ENROLLMENT
    # =============================

    def generate_stub_content(self, campaign_id: str, user_id: str) -> int:
        """
        Stage 4 Stub: Generate placeholder templates for all steps.
        Groups by 'designation' of enrolled prospects to create persona-based templates.
        """
        content = self.uow.content
        sequences = content.steps(campaign_id)
        if not sequences:
            raise ValueError("No sequence steps defined")
        designations: List[Optional[str]] = self.uow.campaigns.enrolled_designations(campaign_id) or [None]
        count = 0
        for designation in designations:
            for seq in sequences:
                if content.has_template(campaign_id, seq.sequence_id, designation):
                    continue
                label = designation if designation else "General"
                content.add(EmailTemplate(
                    template_id=str(uuid.uuid4()), campaign_id=campaign_id, sequence_id=seq.sequence_id,
                    designation=designation, subject=f"[{label}] Subject for Step {seq.step_number}",
                    body=f"Hi {{{{first_name}}}},\n\nThis is a placeholder {label} email for step {seq.step_number}.",
                    is_ai_generated=False, approved_by=None, approved_at=None,
                ))
                count += 1
        content.flush()
        return count

    def enroll_prospects(self, campaign_id: str, user_id: str, request: CampaignEnrollmentRequest) -> int:
        return self.enroll_prospects_with_report(campaign_id, user_id, request)[0]

    def _screen_request(
        self, campaign: Campaign, request: CampaignEnrollmentRequest, skip_ids: Optional[set] = None,
    ) -> Tuple[List[Prospect], List[dict]]:
        """The campaign page's enrolment rules for a request: (eligible, rejections). Reads only."""
        db = self.uow.session
        candidate_ids = list(request.prospect_ids or [])
        if request.list_ids:
            candidate_ids.extend(enrollment_rules.list_member_ids(db, campaign.tenant_id, request.list_ids))
        if not candidate_ids:
            return [], []
        prospects, rejections = enrollment_rules.load_tenant_prospects(db, campaign.tenant_id, candidate_ids)
        cooloff_ids = enrollment_rules.sent_within(db, [p.prospect_id for p in prospects], hours=24)
        eligible, screened_out = enrollment_rules.screen(
            db, campaign.tenant_id, campaign.campaign_id, prospects, recently_contacted=cooloff_ids,
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
        repo = self.uow.campaigns
        campaign = repo.by_id(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")
        if campaign.status not in (CampaignStatus.DRAFT.value, CampaignStatus.PAUSED.value,
                                   CampaignStatus.ACTIVE.value):
            raise ValueError("Cannot enroll prospects in current campaign status")

        eligible, rejections = self._screen_request(campaign, request, skip_ids)
        if not eligible:
            return 0, rejections

        step1 = self.uow.content.step(campaign_id, 1)
        scheduled_time = utcnow() + timedelta(days=(step1.wait_days or 0) if step1 else 0)
        reactivated = enrollment_rules.reactivate_removed(
            self.uow.session, campaign.tenant_id, campaign_id, [p.prospect_id for p in eligible], scheduled_time)
        repo.add_enrolments([
            CampaignProspect(id=str(uuid.uuid4()), campaign_id=campaign_id, prospect_id=prospect.prospect_id,
                             current_step=1, status="ACTIVE", enrolled_at=utcnow(), next_scheduled_at=scheduled_time)
            for prospect in eligible if prospect.prospect_id not in reactivated
        ])
        repo.flush()
        enrolled_count = len(eligible)
        self.log(campaign, user_id, f"{AuditService.ACTION_UPDATE}_ENROLLED_{enrolled_count}")

        # A launched campaign only reads pre-existing step emails: build the new contacts' ones now (the
        # pre-schedule skips contacts / steps that already have an email, so only the newcomers get theirs).
        if campaign.status in (CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value):
            self._preschedule_all_emails(campaign_id)
        return enrolled_count, rejections

    def remove_prospect(self, campaign_id: str, prospect_id: str, user_id: str) -> bool:
        """
        Remove a prospect from a campaign (post-launch prospect-list editing): cancels the contact's unsent
        step emails and marks the enrolment PAUSED through the status ladder (a BOUNCED / REPLIED /
        UNSUBSCRIBED status is never downgraded). The row is kept for history.
        """
        repo = self.uow.campaigns
        campaign = repo.by_id(campaign_id)
        if not campaign:
            return False
        enrolment = repo.enrolment(campaign_id, prospect_id)
        if not enrolment:
            return False
        if enrolment.status == "PAUSED" and enrolment.stopped_reason == REMOVED_BY_USER:
            return False  # already removed: a repeated DELETE is a 404
        self.uow.messages.cancel_contact(campaign_id, prospect_id, REMOVED_BY_USER)
        set_prospect_status(enrolment, "PAUSED", stopped_reason=REMOVED_BY_USER)
        self.log(campaign, user_id, f"{AuditService.ACTION_UPDATE}_REMOVED_PROSPECT")
        repo.flush()
        return True

    def propagate_template_edit(self, template_id: str) -> int:
        """
        Re-render subject/body of the not-yet-sent step emails built from this template. The emails are
        rendered per contact at launch and the sender trusts that snapshot, so without this an edit after
        launch would not reach emails that have not gone out yet.
        """
        template = self.uow.content.template_by_id(template_id)
        if not template or not template.campaign_id:
            return 0
        campaign = self.uow.campaigns.by_id(template.campaign_id)
        if not campaign:
            return 0
        return self.rerender(campaign, self.uow.messages.unsent_of_template(template_id), {template_id: template})

    def rerender(self, campaign: Campaign, messages: Sequence[EmailMessage],
                 templates: Optional[dict] = None) -> int:
        """Re-render unsent emails from their templates with the campaign's current values. An email in a
        subject A/B test keeps its variant's subject (BR-SF-18); only its body follows the template."""
        messages = [m for m in messages if m.template_id]
        if not messages:
            return 0
        templates = templates or {}
        missing = {m.template_id for m in messages if m.template_id not in templates}
        templates.update(self.uow.content.templates_by_ids(missing))
        contacts = self.uow.campaigns.contacts(m.prospect_id for m in messages)
        ctx = self.render_context(campaign)
        updated = 0
        for msg in messages:
            prospect, template = contacts.get(msg.prospect_id), templates.get(msg.template_id)
            if not prospect or not template:
                continue
            if not msg.subject_variant:
                msg.subject = substitute_placeholders(template.subject or "", prospect, ctx)
            msg.body_text = render_body(template.body, prospect, ctx)
            updated += 1
        self.uow.messages.flush()
        return updated

    # =============================
    # LAUNCH / PRE-SCHEDULING
    # =============================

    def launch(self, campaign_id: str, user_id: str) -> Optional[Campaign]:
        """Launch a draft campaign (set to ACTIVE): auto-approves all templates then pre-schedules all emails."""
        campaign = self.uow.campaigns.by_id(campaign_id)
        if not campaign:
            return None
        if campaign.status != CampaignStatus.DRAFT.value:
            raise ValueError(f"Cannot launch campaign with status: {campaign.status}")
        # Campaign mail only goes from the campaign's own mailboxes, never the platform address
        require_active_inbox(campaign, "launch")
        # CAN-SPAM: every email must carry the sender's own postal address (400 with the reason)
        require_postal_address(self.uow.session, campaign.tenant_id)

        content = self.uow.content
        steps = content.steps(campaign_id)
        if not steps:
            raise ValueError("Cannot launch campaign without sequence steps")
        # Every step needs a template of its own (a count of templates let a step with none pass when
        # another step had several persona versions; that step was then silently skipped)
        missing = uncovered_steps(((s.step_number, s.sequence_id) for s in steps),
                                  (t.sequence_id for t in content.templates(campaign_id)))
        if missing:
            raise ValueError(missing_templates_message(missing))
        for t in content.unapproved_templates(campaign_id):
            t.approved_by = user_id
            t.approved_at = utcnow()
        content.flush()

        self._preschedule_all_emails(campaign_id)
        old_status = campaign.status
        campaign.status = CampaignStatus.ACTIVE.value
        record_state_change(self.uow, campaign_id, old_status, campaign.status, notify=False)
        self.log(campaign, user_id, AuditService.ACTION_LAUNCH)
        self.uow.campaigns.flush()
        return campaign

    def _preschedule_all_emails(self, campaign_id: str) -> int:
        """Pre-generate the step emails of every ACTIVE enrolment (launch, enrolment after launch)."""
        campaign = self.uow.campaigns.by_id(campaign_id)
        if not campaign:
            logger.error("[PRESCHEDULE] Campaign %s not found", campaign_id)
            return 0
        return len(self.preschedule(campaign))

    def preschedule(self, campaign: Campaign, lead_statuses: Sequence[str] = ("ACTIVE",),
                    skip_passed: bool = False) -> List[EmailMessage]:
        """
        Create the missing step emails of the campaign's enrolments in ``lead_statuses`` (one per contact
        and step; existing ones are kept). ``skip_passed``: no email for a step a contact is already past
        (a later step of theirs went out), used when a step is added after launch. Returns the new emails.
        """
        logger.info("[PRESCHEDULE] Starting for campaign %s", campaign.campaign_id)
        enrolments = sorted(self.uow.campaigns.enrolments(campaign.campaign_id, lead_statuses),
                            key=lambda cp: cp.prospect_id or "")
        steps = self.uow.content.steps(campaign.campaign_id)
        if not enrolments or not steps:
            return []
        self.uow.content.flush()  # auto-approved templates are visible
        plan = self._preschedule_inputs(campaign, enrolments, steps, skip_passed)
        new_messages: List[EmailMessage] = []
        for index, cp in enumerate(enrolments):
            new_messages.extend(self._contact_emails(plan, cp, index, len(enrolments)))
        created = self.uow.messages.insert(new_messages)
        logger.info("[PRESCHEDULE] Created %s messages", created)
        return new_messages

    def _preschedule_inputs(self, campaign: Campaign, enrolments: List[CampaignProspect], steps: List[Any],
                            skip_passed: bool) -> _Preschedule:
        prospect_ids = [cp.prospect_id for cp in enrolments]
        contacts = self.uow.campaigns.contacts(prospect_ids)
        existing = self.uow.messages.existing_step_keys(campaign.campaign_id)
        inboxes = [i for i in (campaign.inboxes or []) if (i.status or "").upper() == "ACTIVE"]
        plan = _Preschedule(
            campaign=campaign, steps=steps, template_map=build_template_map(self.uow.content.templates(
                campaign.campaign_id)), contacts=contacts, existing=existing,
            personas=self.uow.campaigns.personas(prospect_ids), variants={}, ctx=self.render_context(campaign),
            settings=schedule_settings(campaign), base_time=schedule_base(utcnow(), schedule_settings(campaign)),
            # Contacts are shared out by each mailbox's effective daily limit (S-18); a contact keeps the
            # mailbox an earlier step of theirs was already bound to (sticky sender)
            rotation=InboxRotation(inboxes), bound=self._bound_inboxes(campaign.campaign_id,
                                                                       {i.inbox_id: i for i in inboxes}),
            has_inboxes=bool(inboxes), skip_passed=skip_passed,
        )
        if skip_passed:
            plan.sent_steps = self._sent_steps(campaign.campaign_id)
        # A/B subject test (BR-SF-18): each new email's variant, balanced against the step's existing ones
        needed: dict = {}
        for cp in enrolments:
            if cp.prospect_id in contacts:
                for seq in steps:
                    if self._needs_email(plan, cp.prospect_id, seq.sequence_id):
                        needed.setdefault(seq.sequence_id, []).append(cp.prospect_id)
        plan.variants = SubjectVariantService(self.uow.session).assignments_for_new_messages(
            campaign.campaign_id, needed)
        return plan

    def _sent_steps(self, campaign_id: str) -> dict[str, set]:
        out: dict[str, set] = {}
        for m in self.uow.messages.step_mails(campaign_id):
            if m.sent_at is not None or m.status == "SENDING":
                out.setdefault(m.prospect_id, set()).add(m.sequence_id)
        return out

    @staticmethod
    def _needs_email(plan: _Preschedule, prospect_id: str, sequence_id: str) -> bool:
        if (prospect_id, sequence_id) in plan.existing:
            return False
        if plan.skip_passed and passed_step([s.sequence_id for s in plan.steps], sequence_id,
                                            plan.sent_steps.get(prospect_id, set())):
            return False
        return True

    def _contact_emails(self, plan: _Preschedule, cp: CampaignProspect, index: int, total: int) -> List[EmailMessage]:
        prospect = plan.contacts.get(cp.prospect_id)
        if not prospect:
            return []
        missing = [s for s in plan.steps if self._needs_email(plan, cp.prospect_id, s.sequence_id)]
        if not missing:
            return []
        inbox = (plan.bound.get(cp.prospect_id) or plan.rotation.next()) if plan.has_inboxes else None
        base = plan.base_time
        if plan.settings.daily_batch_size and plan.settings.daily_batch_size > 0:
            # Daily batch throttle: the Nth batch of contacts starts N business days later
            base = batch_day_start(base, index // plan.settings.daily_batch_size)
        skip = {s.sequence_id for s in plan.steps} - {s.sequence_id for s in missing}
        times = launch_times([(s.sequence_id, s.wait_days or 0) for s in plan.steps], skip, base,
                             contact_timezone(plan.settings, prospect.poc_state), plan.settings, index, total)
        persona = plan.personas.get(prospect.prospect_id) or classify_prospect(
            prospect.designation or "", prospect.company_name)[0]
        out = []
        for seq in missing:
            template = pick_template(plan.template_map, seq.sequence_id, persona)
            if template:
                out.append(self._new_email(plan, cp, prospect, seq, template, inbox, times[seq.sequence_id]))
        return out

    @staticmethod
    def _new_email(plan: _Preschedule, cp: CampaignProspect, prospect: Prospect, seq: Any, template: EmailTemplate,
                   inbox: Any, scheduled_at: datetime) -> EmailMessage:
        variant = plan.variants.get((cp.prospect_id, seq.sequence_id))
        subject, body = render_step_email(
            (variant.subject if variant else None) or template.subject or f"[Step {seq.step_number}] Email",
            template.body, prospect, plan.ctx)
        return EmailMessage(
            message_id=str(uuid.uuid4()), campaign_id=plan.campaign.campaign_id, prospect_id=cp.prospect_id,
            sequence_id=seq.sequence_id, inbox_id=inbox.inbox_id if inbox else None,
            # No active inbox: no sender yet (never the platform address); the scheduler binds one when the
            # campaign has an active inbox again
            from_email=inbox.email_address if inbox else None, to_email=prospect.email, subject=subject,
            body_text=body, status="SCHEDULED", scheduled_at=scheduled_at, sent_at=None,
            template_id=template.template_id, subject_variant=variant.label if variant else None,
        )

    @staticmethod
    def _schedule_base_time(campaign: Campaign) -> datetime:
        """Now, or the campaign's start date (midnight in its timezone, as naive UTC) when later."""
        return schedule_base(utcnow(), schedule_settings(campaign))

    def _bound_inboxes(self, campaign_id: str, active_by_id: dict) -> dict:
        """{prospect_id: active mailbox} of contacts that already have a step email bound to one."""
        out: dict = {}
        for pid, inbox_id in self.uow.messages.bound_inboxes(campaign_id, list(active_by_id)):
            out.setdefault(pid, active_by_id[inbox_id])
        return out

    def _insert_new_messages(self, messages: List[EmailMessage]) -> int:
        """Insert pre-scheduled step emails, skipping ones the one-email-per-step key refuses."""
        return self.uow.messages.insert(messages)

    def active_inboxes(self, campaign: Campaign) -> list:
        return active_inboxes(campaign)

    def contacts(self, prospect_ids: Iterable[str]) -> dict:
        return self.uow.campaigns.contacts(prospect_ids)
