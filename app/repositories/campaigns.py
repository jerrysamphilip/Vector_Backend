"""
Campaign aggregate repository (ADR 0009): campaigns, their enrolments, state events and mailboxes, the
SQL behind duplicating a campaign, the extra enrolment rules, the lead-list timing columns and hiding
removed enrolments.

Every tenant entry point names the tenant and filters on it (through the campaign or the contact).
Helpers that take only a ``campaign_id`` are for a campaign the caller already scoped to the tenant.
Nothing here commits: the calling service owns the unit of work.

Sibling repositories of the aggregate (same unit of work, ``CampaignsUnitOfWork``):
app/repositories/campaigns_content.py (steps, templates, versions, attachments, subject variants),
campaigns_messages.py (step emails), campaigns_reads.py (analytics and schedule read models),
campaigns_wizard.py (lists, contacts and drafts for the wizard).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

from sqlalchemy import ColumnElement, Select, and_, func, not_, select
from sqlalchemy.orm import Session

from app.core.concurrency import claim_version
from app.domain.campaigns import REMOVED_BY_USER, CampaignListFilter, StepMessage
from app.domain.paging import Page, Paging
from app.models.automation_rule import AutomationRule
from app.models.campaign import Campaign, CampaignProspect, CampaignStateEvent
from app.models.campaign_group import CampaignGroupMember
from app.models.domain_reputation import SendingDomain
from app.models.email_attachment import EmailAttachment
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.metrics import CampaignMetricsRealtime
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.sending_inbox import SendingInbox
from app.models.subject_variant import EmailSubjectVariant
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId
from app.repositories.campaigns_content import CampaignContentRepository
from app.repositories.campaigns_messages import CampaignMessagesRepository
from app.repositories.campaigns_reads import CampaignReadsRepository
from app.repositories.campaigns_wizard import CampaignDraftsRepository, CampaignWizardRepository
from app.utils.sql_like import ilike

# Enrolment statuses whose sequence may still send (follow-ups are pre-scheduled, so OPENED counts)
IN_SEQUENCE_STATUSES = ("ACTIVE", "OPENED")


def not_removed() -> ColumnElement[bool]:
    """SQL filter: the enrolment was not removed from the campaign by the user (S-21)."""
    return not_(and_(CampaignProspect.status == "PAUSED",
                     func.coalesce(CampaignProspect.stopped_reason, "") == REMOVED_BY_USER))


class CampaignsRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ── campaigns ──────────────────────────────────────────────────────────────────────────────

    def get(self, tenant_id: TenantId, campaign_id: str) -> Optional[Campaign]:
        return self._s.execute(select(Campaign).where(
            Campaign.campaign_id == campaign_id, Campaign.tenant_id == tenant_id)).scalar_one_or_none()

    def lock(self, tenant_id: TenantId, campaign_id: str) -> Optional[Campaign]:
        """The campaign with its row locked until the unit of work ends (SELECT ... FOR UPDATE)."""
        return self._s.execute(select(Campaign).where(
            Campaign.campaign_id == campaign_id, Campaign.tenant_id == tenant_id
        ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()

    def by_id(self, campaign_id: str) -> Optional[Campaign]:
        """A campaign the caller already scoped to its tenant (legacy helpers)."""
        return self._s.get(Campaign, campaign_id)

    def exists(self, campaign_id: str) -> bool:
        return self._s.execute(select(Campaign.campaign_id).where(
            Campaign.campaign_id == campaign_id)).first() is not None

    def add(self, tenant_id: TenantId, entity: object) -> None:
        if getattr(entity, "tenant_id", tenant_id) != tenant_id:
            raise ValueError("entity belongs to another tenant")
        self._s.add(entity)

    def flush(self) -> None:
        self._s.flush()

    def refresh(self, entity: object) -> None:
        self._s.refresh(entity)

    def page(self, tenant_id: TenantId, flt: CampaignListFilter, paging: Paging) -> Page[Campaign]:
        """The tenant's campaigns, newest first."""
        q = select(Campaign).where(Campaign.tenant_id == tenant_id)
        if flt.status:
            q = q.where(Campaign.status == flt.status)
        if flt.created_by:
            q = q.where(Campaign.created_by == flt.created_by)
        if flt.campaign_name:
            q = q.where(ilike(Campaign.campaign_name, flt.campaign_name))
        total = int(self._s.execute(select(func.count()).select_from(q.subquery())).scalar() or 0)
        items = list(self._s.execute(q.order_by(Campaign.created_at.desc())
                                     .offset(paging.offset).limit(paging.limit)).scalars())
        return Page(items=items, total=total, paging=paging)

    def creator_names(self, user_ids: Iterable[str]) -> dict[str, str]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        rows = self._s.execute(select(User.user_id, User.first_name, User.last_name)
                               .where(User.user_id.in_(ids))).all()
        return {uid: f"{first} {last}" for uid, first, last in rows}

    def creator_full_name(self, user_id: str) -> Optional[str]:
        user = self._s.get(User, user_id)
        return user.full_name if user else None

    def add_state_event(self, campaign_id: str, from_state: Optional[str], to_state: str,
                        reason: Optional[str] = None) -> CampaignStateEvent:
        event = CampaignStateEvent(campaign_id=campaign_id, from_state=from_state, to_state=to_state, reason=reason)
        self._s.add(event)
        return event

    def delete_cascade(self, campaign: Campaign) -> None:
        """Delete a campaign and everything that belongs to it, children first."""
        cid = campaign.campaign_id
        s = self._s
        message_ids = select(EmailMessage.message_id).where(EmailMessage.campaign_id == cid)
        s.query(EmailEvent).filter(EmailEvent.message_id.in_(message_ids)).delete(synchronize_session=False)
        s.query(EmailMessage).filter(EmailMessage.campaign_id == cid).delete(synchronize_session=False)
        template_ids = list(s.execute(select(EmailTemplate.template_id)
                                      .where(EmailTemplate.campaign_id == cid)).scalars())
        if template_ids:
            # Attachment rows (not files: a duplicate may share a file with its source)
            s.query(EmailAttachment).filter(EmailAttachment.template_id.in_(template_ids)).delete(
                synchronize_session=False)
            s.query(EmailTemplateVersion).filter(EmailTemplateVersion.template_id.in_(template_ids)).delete(
                synchronize_session=False)
        s.query(EmailTemplate).filter(EmailTemplate.campaign_id == cid).delete()
        s.query(EmailSequence).filter(EmailSequence.campaign_id == cid).delete()
        s.query(CampaignProspect).filter(CampaignProspect.campaign_id == cid).delete()
        s.query(CampaignMetricsRealtime).filter(CampaignMetricsRealtime.campaign_id == cid).delete()
        s.query(CampaignStateEvent).filter(CampaignStateEvent.campaign_id == cid).delete()
        s.query(CampaignGroupMember).filter(CampaignGroupMember.campaign_id == cid).delete()
        s.query(AutomationRule).filter(AutomationRule.campaign_id == cid).delete()
        s.delete(campaign)
        s.flush()

    # ── mailboxes ──────────────────────────────────────────────────────────────────────────────

    def inboxes(self, tenant_id: TenantId, inbox_ids: Sequence[str]) -> list[SendingInbox]:
        if not inbox_ids:
            return []
        return list(self._s.execute(select(SendingInbox).where(
            SendingInbox.tenant_id == tenant_id, SendingInbox.inbox_id.in_(list(inbox_ids)))).scalars())

    def blacklisted_domains(self, tenant_id: TenantId, domains: Iterable[str]) -> list[str]:
        names = list(domains)
        if not names:
            return []
        rows = self._s.execute(select(SendingDomain.domain_name, SendingDomain.is_blacklisted).where(
            SendingDomain.tenant_id == tenant_id, SendingDomain.domain_name.in_(names))).all()
        return [name for name, blocked in rows if blocked]

    # ── enrolments ─────────────────────────────────────────────────────────────────────────────

    def enrolment(self, campaign_id: str, prospect_id: str) -> Optional[CampaignProspect]:
        return self._s.execute(select(CampaignProspect).where(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.prospect_id == prospect_id)).scalars().first()

    def enrolments(self, campaign_id: str, statuses: Optional[Sequence[str]] = None) -> list[CampaignProspect]:
        q = select(CampaignProspect).where(CampaignProspect.campaign_id == campaign_id)
        if statuses is not None:
            q = q.where(CampaignProspect.status.in_(list(statuses)))
        return list(self._s.execute(q).scalars())

    def enrolments_with_contacts(self, campaign_id: str) -> list[tuple[CampaignProspect, Prospect]]:
        rows = self._s.execute(select(CampaignProspect, Prospect)
                               .join(Prospect, CampaignProspect.prospect_id == Prospect.prospect_id)
                               .where(CampaignProspect.campaign_id == campaign_id)).all()
        return [(cp, p) for cp, p in rows]

    def enrolled_contact_ids(self, campaign_id: str) -> set[str]:
        return set(self._s.execute(select(CampaignProspect.prospect_id).where(
            CampaignProspect.campaign_id == campaign_id)).scalars())

    def enrolled_designations(self, campaign_id: str) -> list[Optional[str]]:
        return list(self._s.execute(select(Prospect.designation).join(
            CampaignProspect, Prospect.prospect_id == CampaignProspect.prospect_id
        ).where(CampaignProspect.campaign_id == campaign_id).distinct()).scalars())

    def add_enrolments(self, enrolments: Sequence[CampaignProspect]) -> None:
        if enrolments:
            self._s.bulk_save_objects(list(enrolments))

    def add_enrolment(self, enrolment: CampaignProspect) -> None:
        self._s.add(enrolment)

    def shift_current_steps(self, campaign_id: str, mapping: Mapping[int, int]) -> int:
        """Renumber enrolments' current step after steps were added / deleted (old number -> new number)."""
        moved = 0
        for cp in self.enrolments(campaign_id):
            new = mapping.get(cp.current_step or 1)
            if new is not None and new != cp.current_step:
                cp.current_step = new
                moved += 1
        return moved

    # ── contacts ───────────────────────────────────────────────────────────────────────────────

    def contacts(self, prospect_ids: Iterable[str]) -> dict[str, Prospect]:
        ids = list(set(prospect_ids))
        if not ids:
            return {}
        return {p.prospect_id: p for p in self._s.execute(
            select(Prospect).where(Prospect.prospect_id.in_(ids))).scalars()}

    def contact(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._s.execute(select(Prospect).where(
            Prospect.prospect_id == prospect_id, Prospect.tenant_id == tenant_id)).scalar_one_or_none()

    def personas(self, prospect_ids: Iterable[str]) -> dict[str, str]:
        ids = list(set(prospect_ids))
        if not ids:
            return {}
        return {pid: persona for pid, persona in self._s.execute(
            select(ProspectPersona.prospect_id, ProspectPersona.persona_type)
            .where(ProspectPersona.prospect_id.in_(ids))).all()}

    def latest_list_notes(self, prospect_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """{prospect_id: {notes, list_id}}: each contact's most recent non-empty list note."""
        if not prospect_ids:
            return {}
        rows = self._s.execute(
            select(ProspectListMember.prospect_id, ProspectListMember.notes, ProspectListMember.list_id)
            .where(ProspectListMember.prospect_id.in_(list(prospect_ids)), ProspectListMember.notes.isnot(None),
                   ProspectListMember.notes != "")
            .order_by(ProspectListMember.added_at.desc())).all()
        out: dict[str, dict[str, Any]] = {}
        for pid, note, list_id in rows:
            out.setdefault(pid, {"notes": note, "list_id": list_id})
        return out

    # ── content of a campaign (duplicate) ──────────────────────────────────────────────────────

    def _owned(self, tenant_id: TenantId, campaign_id: str) -> Select[tuple[str]]:
        return select(Campaign.campaign_id).where(Campaign.campaign_id == campaign_id,
                                                  Campaign.tenant_id == tenant_id)

    def sequences(self, tenant_id: TenantId, campaign_id: str) -> list[EmailSequence]:
        return list(self._s.execute(select(EmailSequence).where(
            EmailSequence.campaign_id.in_(self._owned(tenant_id, campaign_id))
        ).order_by(EmailSequence.step_number)).scalars())

    def templates(self, tenant_id: TenantId, campaign_id: str) -> list[EmailTemplate]:
        return list(self._s.execute(select(EmailTemplate).where(
            EmailTemplate.campaign_id.in_(self._owned(tenant_id, campaign_id))
        ).order_by(EmailTemplate.created_at, EmailTemplate.template_id)).scalars())

    def attachments(self, template_ids: Sequence[str]) -> list[EmailAttachment]:
        if not template_ids:
            return []
        return list(self._s.execute(select(EmailAttachment).where(
            EmailAttachment.template_id.in_(list(template_ids)))).scalars())

    def subject_variants(self, tenant_id: TenantId, campaign_id: str) -> list[EmailSubjectVariant]:
        return list(self._s.execute(select(EmailSubjectVariant).where(
            EmailSubjectVariant.campaign_id == campaign_id, EmailSubjectVariant.tenant_id == tenant_id
        ).order_by(EmailSubjectVariant.sequence_id, EmailSubjectVariant.label)).scalars())

    def automation_rules(self, tenant_id: TenantId, campaign_id: str) -> list[AutomationRule]:
        return list(self._s.execute(select(AutomationRule).where(
            AutomationRule.campaign_id.in_(self._owned(tenant_id, campaign_id))
        ).order_by(AutomationRule.created_at, AutomationRule.rule_id)).scalars())

    def enrolled_prospect_ids(self, tenant_id: TenantId, campaign_id: str) -> set[str]:
        return set(self._s.execute(select(CampaignProspect.prospect_id).where(
            CampaignProspect.campaign_id.in_(self._owned(tenant_id, campaign_id)))).scalars())

    def enrolled_personas(self, tenant_id: TenantId, campaign_id: str) -> list[tuple[Prospect, Optional[str]]]:
        """(contact, stored persona or None) for each enrolment that is not removed."""
        rows = self._s.execute(
            select(Prospect, ProspectPersona.persona_type)
            .join(CampaignProspect, CampaignProspect.prospect_id == Prospect.prospect_id)
            .outerjoin(ProspectPersona, ProspectPersona.prospect_id == Prospect.prospect_id)
            .where(CampaignProspect.campaign_id.in_(self._owned(tenant_id, campaign_id)),
                   Prospect.tenant_id == tenant_id, not_removed())
        ).all()
        seen: dict[str, tuple[Prospect, Optional[str]]] = {}
        for prospect, persona in rows:
            seen.setdefault(prospect.prospect_id, (prospect, persona))
        return list(seen.values())

    def designations_by_step(self, tenant_id: TenantId, campaign_id: str) -> dict[int, set[Optional[str]]]:
        out: dict[int, set[Optional[str]]] = {
            s.step_number: set() for s in self.sequences(tenant_id, campaign_id)}
        for step, designation in self._s.execute(
                select(EmailSequence.step_number, EmailTemplate.designation)
                .join(EmailTemplate, EmailTemplate.sequence_id == EmailSequence.sequence_id)
                .where(EmailSequence.campaign_id.in_(self._owned(tenant_id, campaign_id)))):
            out.setdefault(step, set()).add(designation or None)
        return out

    # ── enrolment rules (S-20) ─────────────────────────────────────────────────────────────────

    def active_in_other_active_campaigns(self, tenant_id: TenantId, prospect_ids: Sequence[str],
                                         exclude_campaign_id: Optional[str]) -> set[str]:
        """Contacts still mid-sequence in another ACTIVE campaign of the workspace (paused ones don't count)."""
        if not prospect_ids:
            return set()
        q = select(CampaignProspect.prospect_id).join(
            Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
        ).where(CampaignProspect.prospect_id.in_(list(prospect_ids)),
                CampaignProspect.status.in_(IN_SEQUENCE_STATUSES),
                Campaign.status == "ACTIVE", Campaign.tenant_id == tenant_id)
        if exclude_campaign_id:
            q = q.where(CampaignProspect.campaign_id != exclude_campaign_id)
        return set(self._s.execute(q.distinct()).scalars())

    def positive_repliers(self, tenant_id: TenantId, prospect_ids: Sequence[str]) -> set[str]:
        """Contacts with a reply classified as positive (EmailEvent.EVENT_POSITIVE_REPLY) in any campaign."""
        if not prospect_ids:
            return set()
        return set(self._s.execute(
            select(EmailMessage.prospect_id)
            .join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id)
            .join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id)
            .where(EmailMessage.prospect_id.in_(list(prospect_ids)), Prospect.tenant_id == tenant_id,
                   EmailEvent.event_type == EmailEvent.EVENT_POSITIVE_REPLY).distinct()).scalars())

    def removed_enrolments(self, tenant_id: TenantId, campaign_id: str,
                           prospect_ids: Sequence[str]) -> dict[str, bool]:
        """{prospect_id: has send history} for the given contacts removed from this campaign."""
        if not prospect_ids:
            return {}
        removed = set(self._s.execute(select(CampaignProspect.prospect_id).where(
            CampaignProspect.campaign_id.in_(self._owned(tenant_id, campaign_id)),
            CampaignProspect.prospect_id.in_(list(prospect_ids)), not_(not_removed()))).scalars())
        if not removed:
            return {}
        sent = set(self._s.execute(select(EmailMessage.prospect_id).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.prospect_id.in_(list(removed)),
            EmailMessage.sent_at.isnot(None)).distinct()).scalars())
        return {pid: pid in sent for pid in removed}

    def removed_rows(self, tenant_id: TenantId, campaign_id: str,
                     prospect_ids: Sequence[str]) -> list[CampaignProspect]:
        """The removed enrolments of these contacts (the service decides whether to reactivate them)."""
        if not prospect_ids:
            return []
        return list(self._s.execute(select(CampaignProspect).where(
            CampaignProspect.campaign_id.in_(self._owned(tenant_id, campaign_id)),
            CampaignProspect.prospect_id.in_(list(prospect_ids)), not_(not_removed()))).scalars())

    # ── lead list (F-12, S-21, S-22) ───────────────────────────────────────────────────────────

    def step_messages(self, tenant_id: TenantId, campaign_id: str,
                      prospect_ids: Iterable[str]) -> dict[str, list[StepMessage]]:
        ids = list(prospect_ids)
        if not ids:
            return {}
        rows = self._s.execute(
            select(EmailMessage.prospect_id, EmailSequence.step_number, EmailMessage.status,
                   EmailMessage.scheduled_at, EmailMessage.sent_at, EmailMessage.delivered_at,
                   EmailMessage.failure_reason, EmailSequence.stop_on_reply, EmailSequence.stop_on_bounce)
            .join(EmailSequence, EmailSequence.sequence_id == EmailMessage.sequence_id)
            .where(EmailMessage.campaign_id.in_(self._owned(tenant_id, campaign_id)),
                   EmailMessage.prospect_id.in_(ids),
                   func.coalesce(EmailMessage.direction, "OUTBOUND") == "OUTBOUND")
        ).all()
        out: dict[str, list[StepMessage]] = defaultdict(list)
        for pid, step, status, scheduled, sent, delivered, reason, on_reply, on_bounce in rows:
            out[pid].append(StepMessage(step_number=step, status=status or "", scheduled_at=scheduled,
                                        sent_at=sent, delivered_at=delivered, failure_reason=reason,
                                        stop_on_reply=on_reply, stop_on_bounce=on_bounce))
        return dict(out)

    def ooo_prospects(self, tenant_id: TenantId, campaign_id: str, prospect_ids: Iterable[str]) -> set[str]:
        ids = list(prospect_ids)
        if not ids:
            return set()
        return set(self._s.execute(
            select(EmailMessage.prospect_id)
            .join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id)
            .where(EmailMessage.campaign_id.in_(self._owned(tenant_id, campaign_id)),
                   EmailMessage.prospect_id.in_(ids),
                   EmailEvent.event_type == EmailEvent.EVENT_REPLY_OOO).distinct()).scalars())

    def sent_from(self, campaign_id: str, prospect_ids: Sequence[str]) -> dict[str, str]:
        """{prospect_id: the mailbox (or snapshot From) a SENT step email went from} (sticky sender)."""
        if not prospect_ids:
            return {}
        rows = self._s.execute(
            select(EmailMessage.prospect_id, EmailMessage.from_email, SendingInbox.email_address)
            .outerjoin(SendingInbox, EmailMessage.inbox_id == SendingInbox.inbox_id)
            .where(EmailMessage.campaign_id == campaign_id, EmailMessage.prospect_id.in_(list(prospect_ids)),
                   EmailMessage.status == "SENT")).all()
        return {pid: inbox_email or from_email or "System Default" for pid, from_email, inbox_email in rows}

    def step_count(self, tenant_id: TenantId, campaign_id: str) -> int:
        return int(self._s.execute(select(func.count(EmailSequence.sequence_id)).where(
            EmailSequence.campaign_id.in_(self._owned(tenant_id, campaign_id)))).scalar() or 0)

    def prospect_counts(self, campaign_ids: Sequence[str], include_removed: bool = False) -> dict[str, int]:
        """Enrolments per campaign (removed ones left out unless asked). Callers pass ids they scoped."""
        if not campaign_ids:
            return {}
        q = select(CampaignProspect.campaign_id, func.count(CampaignProspect.id)).where(
            CampaignProspect.campaign_id.in_(list(campaign_ids)))
        if not include_removed:
            q = q.where(not_removed())
        return {cid: int(n) for cid, n in self._s.execute(q.group_by(CampaignProspect.campaign_id)).all()}


class CampaignsUnitOfWork(SqlAlchemyUnitOfWork):
    """Unit of work with the campaign aggregate's repositories on its session."""

    def __enter__(self) -> "CampaignsUnitOfWork":
        super().__enter__()
        return self

    @property
    def campaigns(self) -> CampaignsRepository:
        return CampaignsRepository(self.session)

    @property
    def content(self) -> CampaignContentRepository:
        return CampaignContentRepository(self.session)

    @property
    def messages(self) -> CampaignMessagesRepository:
        return CampaignMessagesRepository(self.session)

    @property
    def reads(self) -> CampaignReadsRepository:
        return CampaignReadsRepository(self.session)

    @property
    def wizard(self) -> CampaignWizardRepository:
        return CampaignWizardRepository(self.session)

    @property
    def drafts(self) -> CampaignDraftsRepository:
        return CampaignDraftsRepository(self.session)

    def claim_version(self, entity: Any, expected: Optional[int], record: str) -> int:
        """T-3 optimistic concurrency (app/core/concurrency.py): 409 when ``expected`` is stale."""
        return claim_version(self.session, entity, expected, record=record)

    def savepoint(self) -> Any:
        return self.session.begin_nested()

    @property
    def session(self) -> Session:
        """Inside the ``with`` block its session; a request-session unit of work (``for_session``) also lends
        its session outside the block, so helpers called inside a caller's transaction (``borrow``) work."""
        if self._session is None and self._borrowed is not None:
            return self._borrowed
        return super().session

    @classmethod
    def for_session(cls, session: Session) -> "CampaignsUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        uow._borrowed = session
        return uow

    _borrowed: Optional[Session] = None


def borrow(db: Session) -> CampaignsUnitOfWork:
    """The aggregate's repositories inside a caller's transaction (legacy ``fn(db, ...)`` helpers): the
    caller commits; nothing is opened or closed here."""
    return CampaignsUnitOfWork.for_session(db)

