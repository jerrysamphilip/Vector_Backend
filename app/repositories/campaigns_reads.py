"""
Read models of one campaign (ADR 0009, chunk A2): the analytics page (daily bars, per-step counters,
lead-status donut, lifetime totals), the per-step replies and the schedule timeline.

Part of the campaign aggregate (``CampaignsUnitOfWork.reads``). Callers scope the campaign to the tenant
first and pass its id. The counter definitions come from app/repositories/reports_campaign_metrics.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.repositories.reports_campaign_metrics import sent_message_filters, unique_prospects_expr


class CampaignReadsRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ── enrolment status counts ────────────────────────────────────────────────────────────────

    def contacts_with_status(self, campaign_id: str, statuses: Sequence[str]) -> int:
        return int(self._s.execute(select(func.count(CampaignProspect.id)).where(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status.in_(list(statuses)))).scalar() or 0)

    def enrolment_count(self, campaign_id: str) -> int:
        return int(self._s.execute(select(func.count(CampaignProspect.id)).where(
            CampaignProspect.campaign_id == campaign_id)).scalar() or 0)

    def status_counts(self, campaign_id: str, statuses: Sequence[str]) -> dict[str, int]:
        return {status: int(n) for status, n in self._s.execute(
            select(CampaignProspect.status, func.count(CampaignProspect.id)).where(
                CampaignProspect.campaign_id == campaign_id, CampaignProspect.status.in_(list(statuses))
            ).group_by(CampaignProspect.status)).all()}

    # ── daily bars ─────────────────────────────────────────────────────────────────────────────

    def sent_per_day(self, campaign_id: str, since: date) -> dict[Any, int]:
        day = func.date(EmailMessage.sent_at)
        return {d: int(n) for d, n in self._s.execute(
            select(day, func.count(EmailMessage.message_id)).where(
                EmailMessage.campaign_id == campaign_id, EmailMessage.sent_at >= since, *sent_message_filters()
            ).group_by(day)).all()}

    def events_per_day(self, campaign_id: str, since: date, event_types: Sequence[int]) -> list[tuple[Any, int, int]]:
        day = func.date(EmailEvent.event_time)
        return [(d, t, int(n)) for d, t, n in self._s.execute(
            select(day, EmailEvent.event_type, func.count(EmailEvent.event_id))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .where(EmailMessage.campaign_id == campaign_id, EmailEvent.event_time >= since,
                   EmailEvent.event_type.in_(list(event_types)))
            .group_by(day, EmailEvent.event_type)).all()]

    def openers_per_day(self, campaign_id: str, since: date) -> dict[Any, int]:
        day = func.date(EmailEvent.event_time)
        return {d: int(n) for d, n in self._s.execute(
            select(day, unique_prospects_expr())
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .where(EmailMessage.campaign_id == campaign_id, EmailEvent.event_time >= since,
                   EmailEvent.event_type == EmailEvent.EVENT_OPEN)
            .group_by(day)).all()}

    # ── per step ───────────────────────────────────────────────────────────────────────────────

    def sent_per_step(self, campaign_id: str, sequence_ids: Sequence[str]) -> dict[str, int]:
        return {sid: int(n) for sid, n in self._s.execute(
            select(EmailMessage.sequence_id, func.count(EmailMessage.message_id)).where(
                EmailMessage.campaign_id == campaign_id, *sent_message_filters(),
                EmailMessage.sequence_id.in_(list(sequence_ids))
            ).group_by(EmailMessage.sequence_id)).all()}

    def unique_contacts_per_step(self, campaign_id: str, sequence_ids: Sequence[str],
                                 event_type: int) -> dict[str, int]:
        return {sid: int(n) for sid, n in self._s.execute(
            select(EmailMessage.sequence_id, unique_prospects_expr())
            .join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id)
            .where(EmailMessage.campaign_id == campaign_id, EmailMessage.sequence_id.in_(list(sequence_ids)),
                   EmailEvent.event_type == event_type)
            .group_by(EmailMessage.sequence_id)).all()}

    def event_messages_per_step(self, campaign_id: str, sequence_ids: Sequence[str],
                                event_types: Sequence[int]) -> list[tuple[str, int, int]]:
        return [(sid, t, int(n)) for sid, t, n in self._s.execute(
            select(EmailMessage.sequence_id, EmailEvent.event_type, func.count(func.distinct(EmailEvent.message_id)))
            .join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id)
            .where(EmailMessage.campaign_id == campaign_id, EmailMessage.sequence_id.in_(list(sequence_ids)),
                   EmailEvent.event_type.in_(list(event_types)))
            .group_by(EmailMessage.sequence_id, EmailEvent.event_type)).all()]

    # ── lifetime ───────────────────────────────────────────────────────────────────────────────

    def sent_total(self, campaign_id: str) -> int:
        return int(self._s.execute(select(func.count(EmailMessage.message_id)).where(
            EmailMessage.campaign_id == campaign_id, *sent_message_filters())).scalar() or 0)

    def unique_contacts(self, campaign_id: str, event_type: int) -> int:
        return int(self._s.execute(select(unique_prospects_expr()).select_from(EmailMessage).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).where(EmailMessage.campaign_id == campaign_id, EmailEvent.event_type == event_type)).scalar() or 0)

    def messages_with_event(self, campaign_id: str, event_type: int) -> int:
        all_ids = select(EmailMessage.message_id).where(EmailMessage.campaign_id == campaign_id)
        return int(self._s.execute(select(func.count(func.distinct(EmailEvent.message_id))).where(
            EmailEvent.message_id.in_(all_ids), EmailEvent.event_type == event_type)).scalar() or 0)

    # ── step replies / schedule ────────────────────────────────────────────────────────────────

    def recent_step_replies(self, campaign_id: str, sequence_id: str, limit: int) -> list[EmailMessage]:
        """Inbound messages in the same conversation as an outbound email of the step, newest first."""
        conversations = select(EmailMessage.conversation_id).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.direction == "OUTBOUND",
            EmailMessage.conversation_id.isnot(None))
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.direction == "INBOUND",
            EmailMessage.conversation_id.in_(conversations)
        ).order_by(EmailMessage.sent_at.desc()).limit(limit)).scalars())

    def schedule_rows(self, campaign_id: str) -> list[Any]:
        return list(self._s.execute(select(
            EmailMessage.message_id, EmailMessage.scheduled_at, EmailMessage.sent_at, EmailMessage.status,
            EmailMessage.sequence_id, EmailMessage.prospect_id, EmailMessage.subject_variant,
            Prospect.first_name, Prospect.last_name, Prospect.email,
        ).join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id).where(
            EmailMessage.campaign_id == campaign_id)).all())
