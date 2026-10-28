"""
Editing a campaign after launch (P2 S-04 / F-04): POST /campaigns/{id}/edits (and its dry run
POST /campaigns/{id}/edits/checks), the step-email preview (POST /campaigns/{id}/email-previews) and
which persona template each contact gets per step (GET /campaigns/{id}/template-assignments).

Emails are rendered per contact when they are pre-scheduled (CampaignEmailService.preschedule) and the
sender trusts that snapshot, so an edit is pushed into the step emails that have not gone out yet
(QUEUED / SCHEDULED / PAUSED_BY_CAMPAIGN; never SENDING or SENT):

* content (step subject / body / persona versions, sender name / title, CTA link): unsent emails are
  re-rendered; a new persona version re-points the step's unsent emails to the best template per persona;
* schedule (send window, timezone, sending mode / gaps, daily batch size, wait days, business days):
  unsent emails are re-planned per contact in step order, the daily batch throttle kept for contacts
  not emailed yet;
* steps: a new step gets emails for the contacts still in the sequence who have not passed it; a step
  can be deleted only while none of its emails went out (its unsent ones are removed), then steps are
  renumbered and the schedule re-planned; existing steps are never reordered;
* mailboxes: unsent emails are re-bound with the weighted rotation (sticky sender kept);
* the start date is locked once the campaign was launched.

One edit is one unit of work: the campaign row is locked (SELECT ... FOR UPDATE) and its row_version
claimed (stale -> 409, T-3), the unsent emails are locked too so the sender cannot claim one mid-edit,
and everything is committed together or not at all. The dry run does the same work and rolls back, so
"N unsent emails will be updated" is exact. Rules: app/domain/campaigns_edit.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from app.core.clock import utcnow
from app.core.concurrency import PreconditionRequired, resolve_expected
from app.core.config import settings as app_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.domain.campaigns import is_removed
from app.domain.campaigns_edit import (CONTENT_FIELDS, IN_SEQUENCE_STATUSES, LAUNCHED_STATUSES, SCHEDULE_FIELDS,
                                       CampaignEdit, ContactPlan, EditConflict, EditRefused, ScheduleSettings,
                                       StepEdit, StepMail, StepPlan, TemplateEdit, build_template_map,
                                       changed_settings, check_editable, check_settings, contact_timezone,
                                       fresh_start, pick_template, plan_contact, plan_steps, renumber,
                                       schedule_base, starts_fresh)
from app.models.campaign import Campaign
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.services.ai_email_service import classify_prospect
from app.services.audit_service import AuditService
from app.services.campaign_calendar import schedule_settings
from app.services.campaign_common import CampaignServiceBase, require_campaign, validate_inboxes
from app.services.campaign_crud_service import CampaignCRUDService
from app.services.campaign_email_service import CampaignEmailService, render_context
from app.services.email_render import SendContext, finalize_for_send, render_step_email
from app.services.send_guards import active_inboxes
from app.services.sender_identity import get_sender_identity
from app.utils.email_utils import finalize_email_body, normalize_cta_link

EDIT_ACTION = f"{AuditService.ACTION_UPDATE}_EDIT"
DEFAULT_ASSIGNMENT_LIMIT = 200


@dataclass
class EditResult:
    """What one edit changed; the dry run returns the same numbers."""

    settings_changed: List[str] = field(default_factory=list)
    content_changed: bool = False
    schedule_changed: bool = False
    inboxes_changed: bool = False
    steps_added: List[int] = field(default_factory=list)
    steps_deleted: List[int] = field(default_factory=list)
    steps_renumbered: bool = False
    templates_changed: set = field(default_factory=set)
    repick_steps: set = field(default_factory=set)
    rerendered: set = field(default_factory=set)
    rescheduled: set = field(default_factory=set)
    rebound: int = 0
    created: set = field(default_factory=set)
    removed: int = 0

    def as_dict(self, dry_run: bool) -> Dict[str, Any]:
        touched = self.rerendered | self.rescheduled | self.created
        return {
            "dry_run": dry_run,
            "settings_changed": sorted(self.settings_changed),
            "steps_added": self.steps_added, "steps_deleted": self.steps_deleted,
            "steps_renumbered": self.steps_renumbered,
            "templates_changed": len(self.templates_changed),
            "emails_rerendered": len(self.rerendered), "emails_rescheduled": len(self.rescheduled),
            "emails_rebound": self.rebound, "emails_created": len(self.created), "emails_removed": self.removed,
            # the number shown as "N unsent emails will be updated"
            "unsent_emails_affected": len(touched) + self.rebound + self.removed,
        }


class CampaignEditService(CampaignServiceBase):
    """Route methods: ``edit`` (also the dry run), ``preview``, ``template_assignments``."""

    def __init__(self, db: Any = None, *, uow: Any = None, audit: Any = None, crud: Any = None,
                 emails: Any = None) -> None:
        super().__init__(db, uow=uow, audit=audit)
        self.crud = crud or CampaignCRUDService(db, uow=self.uow, audit=audit)
        self.emails = emails or CampaignEmailService(db, uow=self.uow, audit=audit)

    # ── edit ──────────────────────────────────────────────────────────────────────────────────

    def edit(self, user: Any, campaign_id: str, edit: CampaignEdit, if_match: Optional[int] = None,
             dry_run: bool = False) -> Dict[str, Any]:
        expected = resolve_expected(edit.expected_version, if_match)
        if expected is None:
            raise PreconditionRequired("Send the campaign's row_version (expected_version or If-Match).",
                                       detail={"code": "VERSION_REQUIRED",
                                               "message": "Send the campaign's row_version you loaded."})
        with self.uow as uow:
            campaign = require_campaign(uow.campaigns, user.tenant_id, campaign_id, lock=True)
            try:
                check_editable(campaign.status)
                uow.claim_version(campaign, expected, "campaign")
                uow.messages.lock_unsent(campaign_id)
                result = self._apply(campaign, edit, user.user_id)
            except EditConflict as exc:
                raise ConflictError(str(exc), code="EDIT_CONFLICT") from exc
            except ValueError as exc:  # EditRefused and the mailbox checks
                raise BadRequestError(str(exc), code="EDIT_REFUSED") from exc
            answer = result.as_dict(dry_run)
            if dry_run:
                uow.rollback()
                return answer
            self.log(campaign, user.user_id, EDIT_ACTION)
            uow.commit()
            answer["campaign"] = self.crud._to_response(campaign)
            return answer

    def delete_step(self, user: Any, sequence_id: str) -> Dict[str, Any]:
        """DELETE /campaigns/sequences/{id}: refused (409) once an email of the step went out; else its unsent
        emails, templates and variants go, later steps move up one number and a launched campaign is re-planned."""
        with self.uow as uow:
            step = uow.content.step_for_tenant(user.tenant_id, sequence_id)
            if step is None:
                raise NotFoundError("Sequence step not found")
            campaign = require_campaign(uow.campaigns, user.tenant_id, step.campaign_id, lock=True)
            uow.messages.lock_unsent(campaign.campaign_id)
            remaining = [StepEdit(sequence_id=s.sequence_id) for s in uow.content.steps(campaign.campaign_id)
                         if s.sequence_id != sequence_id]
            result = EditResult()
            try:
                if remaining:
                    self._apply_steps(campaign, tuple(remaining), user.user_id, result)
                else:
                    self._delete_step(step, result)
                uow.campaigns.flush()
                if campaign.status in LAUNCHED_STATUSES:
                    self._propagate(campaign, result)
            except EditConflict as exc:
                raise ConflictError(str(exc), code="EDIT_CONFLICT") from exc
            self.log(campaign, user.user_id, f"{AuditService.ACTION_UPDATE}_DELETED_STEP")
            uow.commit()
            return result.as_dict(False)

    def _apply(self, campaign: Campaign, edit: CampaignEdit, user_id: str) -> EditResult:
        result = EditResult()
        self._apply_settings(campaign, edit.settings, result)
        if edit.inbox_ids is not None:
            self._apply_inboxes(campaign, list(edit.inbox_ids), result)
        if edit.steps is not None:
            self._apply_steps(campaign, edit.steps, user_id, result)
        self.uow.campaigns.flush()
        if campaign.status in LAUNCHED_STATUSES:
            self._propagate(campaign, result)
        return result

    # ── settings / mailboxes ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_settings(campaign: Campaign, requested: Mapping[str, Any], result: EditResult) -> None:
        wanted = {name: (normalize_cta_link(v) if v else None) if name == "cta_link" else v
                  for name, v in requested.items()}
        current = {name: getattr(campaign, name) for name in wanted}
        changed = changed_settings(current, wanted)
        merged = {name: changed.get(name, getattr(campaign, name)) for name in
                  ("send_window_start", "send_window_end", "start_date", "end_date", "campaign_timezone")}
        check_settings(campaign.status, changed, merged)
        for name, value in changed.items():
            setattr(campaign, name, value)
        result.settings_changed = sorted(changed)
        result.content_changed = bool(set(changed) & CONTENT_FIELDS)
        result.schedule_changed = bool(set(changed) & SCHEDULE_FIELDS)

    def _apply_inboxes(self, campaign: Campaign, inbox_ids: List[str], result: EditResult) -> None:
        inboxes = validate_inboxes(self.uow.campaigns, campaign.tenant_id, inbox_ids)
        if campaign.status in LAUNCHED_STATUSES and not inboxes:
            raise EditRefused("A launched campaign needs at least one active mailbox")
        if {i.inbox_id for i in inboxes} != {i.inbox_id for i in (campaign.inboxes or [])}:
            campaign.inboxes = inboxes
            result.inboxes_changed = True

    # ── steps ─────────────────────────────────────────────────────────────────────────────────

    def _apply_steps(self, campaign: Campaign, steps: tuple[StepEdit, ...], user_id: str, result: EditResult) -> None:
        content = self.uow.content
        existing = content.steps(campaign.campaign_id)
        by_id = {s.sequence_id: s for s in existing}
        plan = plan_steps([(s.sequence_id, s.step_number) for s in existing], steps)
        for sequence_id in plan.delete:
            self._delete_step(by_id[sequence_id], result)
        old_to_new = {by_id[sid].step_number: number for sid, number, _ in plan.keep}
        for sequence_id, number, step_edit in plan.keep:
            step = by_id[sequence_id]
            step.step_number = number
            self._update_step(campaign, step, step_edit, user_id, result)
        for number, step_edit in plan.add:
            self._add_step(campaign, number, step_edit, user_id, result)
        content.flush()
        result.steps_renumbered = plan.renumbered or bool(plan.add or plan.delete)
        if plan.changes_order:
            self._renumber_enrolments(campaign, plan, old_to_new)

    def _renumber_enrolments(self, campaign: Campaign, plan: StepPlan, old_to_new: Dict[int, int]) -> None:
        mapping = {old: renumber(old, old_to_new) for old in range(1, 8)}
        self.uow.campaigns.shift_current_steps(campaign.campaign_id, mapping)

    def _delete_step(self, step: EmailSequence, result: EditResult) -> None:
        sent = self.uow.messages.sent_or_sending_of_step(step.sequence_id)
        if sent:
            raise EditConflict(
                f"Step {step.step_number} has already been sent to {sent} contact(s), so it can't be deleted. "
                f"Edit its email instead, or duplicate the campaign to start over without it.")
        result.removed += self.uow.messages.delete_step_emails(step.sequence_id)
        self.uow.content.delete_step_variants(step.sequence_id)
        self.uow.content.delete_step_templates(step.sequence_id)
        self.uow.content.delete(step)
        self.uow.content.flush()
        result.steps_deleted.append(step.step_number)
        result.schedule_changed = True

    def _update_step(self, campaign: Campaign, step: EmailSequence, edit: StepEdit, user_id: str,
                     result: EditResult) -> None:
        if edit.wait_days is not None and edit.wait_days != (step.wait_days or 0):
            step.wait_days = edit.wait_days
            result.schedule_changed = True
        for flag in ("stop_on_reply", "stop_on_bounce"):
            value = getattr(edit, flag)
            if value is not None:
                setattr(step, flag, value)
        for template_edit in edit.templates:
            self._apply_template(campaign, step, template_edit, user_id, result)

    def _add_step(self, campaign: Campaign, number: int, edit: StepEdit, user_id: str, result: EditResult) -> None:
        emails = [t for t in edit.templates if not t.template_id and (t.subject or "").strip() and (t.body or "").strip()]
        if not emails:
            raise EditRefused(f"The new step {number} needs an email (subject and body)")
        step = EmailSequence(sequence_id=str(uuid.uuid4()), campaign_id=campaign.campaign_id, step_number=number,
                             wait_days=edit.wait_days or 0,
                             stop_on_reply=True if edit.stop_on_reply is None else edit.stop_on_reply,
                             stop_on_bounce=True if edit.stop_on_bounce is None else edit.stop_on_bounce)
        self.uow.content.add(step)
        self.uow.content.flush()
        for template_edit in emails:
            self._apply_template(campaign, step, template_edit, user_id, result)
        result.steps_added.append(number)

    def _apply_template(self, campaign: Campaign, step: EmailSequence, edit: TemplateEdit, user_id: str,
                        result: EditResult) -> None:
        content = self.uow.content
        if edit.template_id:
            template = content.template(campaign.campaign_id, edit.template_id)
            if template is None or template.sequence_id != step.sequence_id:
                raise EditRefused(f"Template {edit.template_id} is not an email of step {step.step_number}")
            if self._change_template(template, edit):
                self._new_version(template, user_id)
                result.templates_changed.add(template.template_id)
                if edit.designation is not None:
                    result.repick_steps.add(step.sequence_id)
            return
        subject, body = (edit.subject or "").strip(), edit.body or ""
        if not subject or not body.strip():
            raise EditRefused(f"A new email of step {step.step_number} needs a subject and a body")
        template = EmailTemplate(template_id=str(uuid.uuid4()), campaign_id=campaign.campaign_id,
                                 sequence_id=step.sequence_id, designation=(edit.designation or None),
                                 subject=subject, body=finalize_email_body(body),
                                 is_ai_generated=False, approved_by=user_id, approved_at=utcnow())
        content.add(template)
        content.flush()
        self._new_version(template, user_id)
        result.templates_changed.add(template.template_id)
        result.repick_steps.add(step.sequence_id)

    @staticmethod
    def _change_template(template: EmailTemplate, edit: TemplateEdit) -> bool:
        changed = False
        if edit.subject is not None and edit.subject.strip() != template.subject:
            if not edit.subject.strip():
                raise EditRefused("An email's subject can't be empty")
            template.subject, changed = edit.subject.strip(), True
        if edit.body is not None:
            if not edit.body.strip():
                raise EditRefused("An email's body can't be empty")
            body = finalize_email_body(edit.body)
            if body != template.body:
                template.body, changed = body, True
        if edit.designation is not None and (edit.designation or None) != (template.designation or None):
            template.designation, changed = (edit.designation or None), True
        return changed

    def _new_version(self, template: EmailTemplate, user_id: str) -> None:
        content = self.uow.content
        version = EmailTemplateVersion(version_id=str(uuid.uuid4()), template_id=template.template_id,
                                       subject=template.subject, body=template.body,
                                       version_number=content.version_count(template.template_id) + 1,
                                       created_by=user_id, is_active=True)
        content.add(version)
        content.flush()
        content.deactivate_other_versions(template.template_id, version.version_id)

    # ── pushing the edit into the unsent emails ───────────────────────────────────────────────

    def _propagate(self, campaign: Campaign, result: EditResult) -> None:
        if result.steps_added or result.repick_steps:
            self._backfill(campaign, result)
        if result.repick_steps:
            self._repick(campaign, result)
        self._rerender(campaign, result)
        if result.inboxes_changed:
            result.rebound = self.crud._rebind_pending_messages_for_campaign(campaign)
        if result.schedule_changed:
            result.rescheduled = self.reschedule(campaign)
        elif result.created:
            # Only the new emails and the later steps of the contacts who got one move
            result.rescheduled = self.reschedule(campaign, self._after_created(campaign, result.created))
        self._sync_current_steps(campaign)

    def _after_created(self, campaign: Campaign, created: set) -> set:
        numbers = self.uow.content.step_numbers(campaign.campaign_id)
        first_new: Dict[str, int] = {}
        mails = self.uow.messages.step_mails(campaign.campaign_id)
        for m in mails:
            if m.message_id in created:
                number = numbers.get(m.sequence_id or "", 99)
                first_new[m.prospect_id] = min(number, first_new.get(m.prospect_id, number))
        return set(created) | {m.message_id for m in mails if m.prospect_id in first_new and m.sent_at is None
                               and numbers.get(m.sequence_id or "", 0) > first_new[m.prospect_id]}

    def _backfill(self, campaign: Campaign, result: EditResult) -> None:
        """Emails of new steps (and of persona versions that cover contacts who had none) for contacts still in
        the sequence who have not passed that step."""
        created = self.emails.preschedule(campaign, IN_SEQUENCE_STATUSES, skip_passed=True)
        if campaign.status == "PAUSED":
            for message in created:  # the state pause() leaves every other unsent email in
                message.status = "PAUSED_BY_CAMPAIGN"
        self.uow.messages.flush()
        result.created |= {m.message_id for m in created}

    def _repick(self, campaign: Campaign, result: EditResult) -> None:
        """Point each unsent email of the steps that got a persona version at the template its contact's persona
        should get now (own version > generic > first, as at launch)."""
        template_map = build_template_map(self.uow.content.templates(campaign.campaign_id))
        for sequence_id in result.repick_steps:
            pending = self.uow.messages.unsent_of_step(sequence_id)
            personas = self.uow.campaigns.personas(m.prospect_id for m in pending)
            contacts = self.uow.campaigns.contacts(m.prospect_id for m in pending)
            for message in pending:
                prospect = contacts.get(message.prospect_id)
                if not prospect:
                    continue
                persona = personas.get(message.prospect_id) or classify_prospect(
                    prospect.designation or "", prospect.company_name)[0]
                template = pick_template(template_map, sequence_id, persona)
                if template and template.template_id != message.template_id:
                    message.template_id = template.template_id
                    result.templates_changed.add(template.template_id)
        self.uow.messages.flush()  # the session does not autoflush: the re-render below queries by template

    def _rerender(self, campaign: Campaign, result: EditResult) -> None:
        messages = self.uow.messages
        if result.content_changed:
            targets = messages.unsent(campaign.campaign_id)
        else:
            targets = [m for tid in result.templates_changed for m in messages.unsent_of_template(tid)]
        targets = [m for m in targets if m.message_id not in result.created]
        if not targets:
            return
        self.emails.rerender(campaign, targets)
        result.rerendered |= {m.message_id for m in targets if m.template_id}

    def reschedule(self, campaign: Campaign, only: Optional[set] = None) -> set:
        """Re-plan the send time of the campaign's unsent step emails; returns the ids that moved."""
        steps = [(s.sequence_id, s.wait_days or 0) for s in self.uow.content.steps(campaign.campaign_id)]
        orm_by_id: Dict[str, Any] = {}
        by_contact: Dict[str, Dict[str, StepMail]] = {}
        for m in self.uow.messages.step_mails(campaign.campaign_id):
            orm_by_id[m.message_id] = m
            by_contact.setdefault(m.prospect_id, {})[m.sequence_id] = StepMail(m.message_id, m.status or "",
                                                                               m.sent_at, m.scheduled_at)
        movable = sorted(pid for pid, mails in by_contact.items() if any(x.editable for x in mails.values()))
        contacts = self.uow.campaigns.contacts(movable)
        settings = schedule_settings(campaign)
        now = utcnow()
        base = schedule_base(now, settings)
        fresh_index, moved = 0, set()
        for index, pid in enumerate(movable):
            mails = by_contact[pid]
            fresh = starts_fresh(steps, mails)
            plan = ContactPlan(steps=steps, mails=mails, index=index, total=len(movable),
                               timezone=contact_timezone(settings, getattr(contacts.get(pid), "poc_state", None)),
                               fresh_base=fresh_start(base, settings, fresh_index) if fresh else base)
            fresh_index += 1 if fresh else 0
            for message_id, when in plan_contact(plan, settings, now, only).items():
                if orm_by_id[message_id].scheduled_at != when:
                    orm_by_id[message_id].scheduled_at = when
                    moved.add(message_id)
        self.uow.messages.flush()
        return moved

    def _sync_current_steps(self, campaign: Campaign) -> None:
        """Enrolments still in the sequence point at their next unsent step (after steps moved)."""
        numbers = self.uow.content.step_numbers(campaign.campaign_id)
        next_step: Dict[str, int] = {}
        for m in self.uow.messages.unsent(campaign.campaign_id):
            number = numbers.get(m.sequence_id or "")
            if number is not None:
                next_step[m.prospect_id] = min(number, next_step.get(m.prospect_id, number))
        for cp in self.uow.campaigns.enrolments(campaign.campaign_id, IN_SEQUENCE_STATUSES):
            if cp.prospect_id in next_step and cp.current_step != next_step[cp.prospect_id]:
                cp.current_step = next_step[cp.prospect_id]

    # ── preview ───────────────────────────────────────────────────────────────────────────────

    def preview(self, user: Any, campaign_id: str, prospect_id: str, step_number: int,
                template_id: Optional[str] = None, subject: Optional[str] = None,
                body: Optional[str] = None) -> Dict[str, Any]:
        """One step email exactly as it will be sent to one enrolled contact (nothing is written)."""
        campaign = require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        prospect = self.uow.campaigns.contact(user.tenant_id, prospect_id)
        enrolment = self.uow.campaigns.enrolment(campaign_id, prospect_id) if prospect else None
        if prospect is None or enrolment is None or is_removed(enrolment.status, enrolment.stopped_reason):
            raise NotFoundError("Contact is not enrolled in this campaign")
        step = self.uow.content.step(campaign_id, step_number)
        if step is None:
            raise NotFoundError("Sequence step not found")
        message = self.uow.messages.step_email(campaign_id, prospect_id, step.sequence_id)
        persona = self.uow.campaigns.personas([prospect_id]).get(prospect_id) or classify_prospect(
            prospect.designation or "", prospect.company_name)[0]
        source, template, rendered = self._preview_content(campaign, step, prospect, persona, message,
                                                           template_id, subject, body)
        sender = self._preview_sender(campaign, message)
        identity = get_sender_identity(campaign.tenant_id, self.uow.session)
        ctx = render_context(self.uow.session, campaign)
        final = finalize_for_send(rendered[0], rendered[1], prospect.email, SendContext(
            message_id=message.message_id if message else "preview", sender_email=sender or app_settings.SENDER_EMAIL,
            base_url=app_settings.BASE_URL, sender_name=ctx.sender_name, sender_title=campaign.sender_title,
            unsubscribe_mode=campaign.unsubscribe_mode or "plain", postal_address=identity.postal_address,
            footer_line=identity.footer_line, company_name=identity.company_name))
        return {
            "campaign_id": campaign_id, "prospect_id": prospect_id, "step_number": step.step_number,
            "sequence_id": step.sequence_id, "persona": persona, "source": source,
            "template_id": template.template_id if template else (message.template_id if message else None),
            "designation": template.designation if template else None,
            "message_id": message.message_id if message else None,
            "message_status": message.status if message else None,
            "scheduled_at": message.scheduled_at if message else None, "sent_at": message.sent_at if message else None,
            "from_name": ctx.sender_name or ctx.default_sender_name, "from_email": sender, "to_email": prospect.email,
            "subject": final.subject, "body": final.body, "is_html": final.is_html,
            "is_internal": final.is_internal, "subject_variant": message.subject_variant if message else None,
        }

    def _preview_content(self, campaign: Campaign, step: EmailSequence, prospect: Any, persona: str,
                         message: Any, template_id: Optional[str], subject: Optional[str],
                         body: Optional[str]) -> tuple[str, Optional[EmailTemplate], tuple[str, str]]:
        ctx = render_context(self.uow.session, campaign)
        if subject is not None or body is not None:
            return "draft", None, render_step_email(subject or "", body or "", prospect, ctx)
        if template_id:
            template = self.uow.content.template(campaign.campaign_id, template_id)
            if template is None or template.sequence_id != step.sequence_id:
                raise NotFoundError("Template not found")
            return "template", template, render_step_email(template.subject, template.body, prospect, ctx)
        if message is not None and message.sent_at is None and (message.subject or "").strip() \
                and (message.body_text or "").strip():
            return "scheduled_email", None, (message.subject, message.body_text)
        if message is not None and message.sent_at is not None:
            return "sent_email", None, (message.subject or "", message.body_text or "")
        template = pick_template(build_template_map(self.uow.content.templates(campaign.campaign_id)),
                                 step.sequence_id, persona)
        if template is None:
            raise NotFoundError("This step has no email for this contact yet")
        return "template", template, render_step_email(template.subject, template.body, prospect, ctx)

    @staticmethod
    def _preview_sender(campaign: Campaign, message: Any) -> Optional[str]:
        if message is not None and message.from_email:
            return message.from_email
        inboxes = active_inboxes(campaign)
        return inboxes[0].email_address if inboxes else None

    # ── template assignments ──────────────────────────────────────────────────────────────────

    def template_assignments(self, user: Any, campaign_id: str, limit: int = DEFAULT_ASSIGNMENT_LIMIT,
                             offset: int = 0) -> Dict[str, Any]:
        """Which persona template each enrolled contact gets per step: the template of their step email when it
        exists, else the one launch / a backfill would pick for their persona."""
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        steps = self.uow.content.steps(campaign_id)
        templates = self.uow.content.templates(campaign_id)
        template_map = build_template_map(templates)
        enrolled = sorted(((cp, p) for cp, p in self.uow.campaigns.enrolments_with_contacts(campaign_id)
                           if not is_removed(cp.status, cp.stopped_reason)),
                          key=lambda row: ((row[1].email or "").lower(), row[1].prospect_id))
        personas = self.uow.campaigns.personas(p.prospect_id for _, p in enrolled)
        emailed = {(m.prospect_id, m.sequence_id): m for m in self.uow.messages.step_mails(campaign_id)
                   if m.status != "CANCELLED"}
        counts: Dict[tuple, int] = {}
        rows = []
        for cp, p in enrolled:
            persona = personas.get(p.prospect_id) or classify_prospect(p.designation or "", p.company_name)[0]
            assignments = []
            for step in steps:
                message = emailed.get((p.prospect_id, step.sequence_id))
                tid = message.template_id if message else getattr(pick_template(
                    template_map, step.sequence_id, persona), "template_id", None)
                counts[(step.sequence_id, tid)] = counts.get((step.sequence_id, tid), 0) + 1
                assignments.append({"step_number": step.step_number, "sequence_id": step.sequence_id,
                                    "template_id": tid, "source": "email" if message else "planned",
                                    "message_status": message.status if message else None})
            rows.append({"prospect_id": p.prospect_id, "name": f"{p.first_name or ''} {p.last_name or ''}".strip(),
                         "email": p.email, "designation": p.designation, "persona": persona,
                         "enrolment_status": cp.status, "assignments": assignments})
        return {
            "campaign_id": campaign_id,
            "steps": [self._step_assignment(step, templates, counts) for step in steps],
            "total": len(rows), "limit": limit, "offset": offset, "prospects": rows[offset:offset + limit],
        }

    @staticmethod
    def _step_assignment(step: EmailSequence, templates: List[EmailTemplate], counts: Dict[tuple, int]) -> Dict:
        own = sorted((t for t in templates if t.sequence_id == step.sequence_id),
                     key=lambda t: ((t.designation or ""), t.template_id))
        return {
            "sequence_id": step.sequence_id, "step_number": step.step_number, "wait_days": step.wait_days or 0,
            "templates": [{"template_id": t.template_id, "designation": t.designation or None, "subject": t.subject,
                           "body": t.body, "recipients": counts.get((step.sequence_id, t.template_id), 0)}
                          for t in own],
            "unassigned": counts.get((step.sequence_id, None), 0),
        }
