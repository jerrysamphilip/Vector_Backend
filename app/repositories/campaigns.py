"""
Campaign aggregate repository (ADR 0009): the SQL behind duplicating a campaign, the extra
enrolment rules, the lead-list timing columns and hiding removed enrolments.

Every call names the tenant and filters on it (through the campaign or the contact). Nothing here
commits: the calling service owns the unit of work.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Optional

from sqlalchemy import ColumnElement, Select, and_, func, not_, select
from sqlalchemy.orm import Session

from app.domain.campaigns import REMOVED_BY_USER, StepMessage
from app.models.automation_rule import AutomationRule
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_attachment import EmailAttachment
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.models.prospect_persona import ProspectPersona
from app.models.subject_variant import EmailSubjectVariant
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId

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

    def add(self, tenant_id: TenantId, entity: object) -> None:
        if getattr(entity, "tenant_id", tenant_id) != tenant_id:
            raise ValueError("entity belongs to another tenant")
        self._s.add(entity)

    def flush(self) -> None:
        self._s.flush()

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
    """Unit of work with the campaigns repository on its session."""

    def __enter__(self) -> "CampaignsUnitOfWork":
        super().__enter__()
        return self

    @property
    def campaigns(self) -> CampaignsRepository:
        return CampaignsRepository(self.session)

    @classmethod
    def for_session(cls, session: Session) -> "CampaignsUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
