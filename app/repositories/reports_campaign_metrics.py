"""
Campaign counter SQL (ADR 0009, chunk A6): the one definition of the campaign counters shared by
every screen (campaign list / detail, step analytics, /metrics, dashboard, global analytics,
comparison), and the per-campaign counts behind ``CampaignAnalyticsService``.

    sent    = outbound campaign messages that actually left (status SENT, or BOUNCED / COMPLAINED
              after a send), inbound replies excluded;
    opened  = UNIQUE prospects with an open event in the campaign;
    clicked = UNIQUE prospects with a click event in the campaign (a contact clicking two links counts
              once; click rate = clicked / opened);
    replied / bounced = distinct messages with that event.

Campaign ids reach these queries only after the caller resolved them inside the tenant (the campaign
services load the campaign tenant-scoped first).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy import ColumnElement, func
from sqlalchemy.orm import Session

from app.models.campaign import CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage

SENT_STATUSES = ("SENT", "BOUNCED", "COMPLAINED")

# Event types counted per campaign (distinct messages) next to the unique openers
COUNTED_EVENTS = (
    EmailEvent.EVENT_OPEN, EmailEvent.EVENT_REPLY, EmailEvent.EVENT_BOUNCE, EmailEvent.EVENT_SENDER_BOUNCE,
    EmailEvent.EVENT_POSITIVE_REPLY, EmailEvent.EVENT_REPLY_OOO, EmailEvent.EVENT_UNSUBSCRIBE,
)


def sent_message_filters() -> list[ColumnElement[bool]]:
    """SQL conditions selecting the EmailMessage rows that count as 'sent'."""
    return [
        EmailMessage.status.in_(SENT_STATUSES),
        EmailMessage.sent_at.isnot(None),
        func.coalesce(EmailMessage.direction, "OUTBOUND") != "INBOUND",
    ]


def unique_prospects_expr() -> ColumnElement[int]:
    """Unique (campaign, prospect) pairs over EmailEvent x EmailMessage rows.

    Filter the joined rows on one event type (open, click): within one campaign this is the number of
    distinct prospects with that event; across campaigns it is the sum of those per-campaign numbers.
    """
    return func.count(func.distinct(func.concat(EmailMessage.campaign_id, ":", EmailMessage.prospect_id)))


# Opened / clicked both count unique prospects (see the definition above).
unique_openers_expr = unique_prospects_expr
unique_clickers_expr = unique_prospects_expr


class CampaignMetricsRepository(Protocol):
    """Raw per-campaign counters; ``CampaignAnalyticsService`` applies the fallbacks."""

    def sent_count(self, campaign_id: str) -> int: ...
    def unique_openers(self, campaign_id: str) -> int: ...
    def event_counts(self, campaign_id: str) -> dict[int, int]: ...
    def prospect_status_count(self, campaign_id: str, statuses: Sequence[str]) -> int: ...
    def bulk_sent(self, campaign_ids: Sequence[str]) -> dict[str, int]: ...
    def bulk_openers(self, campaign_ids: Sequence[str]) -> dict[str, int]: ...
    def bulk_event_counts(self, campaign_ids: Sequence[str]) -> list[tuple[str, int, int]]: ...
    def bulk_prospect_status_counts(self, campaign_ids: Sequence[str], statuses: Sequence[str]) -> list[tuple[str, int]]: ...


class SqlAlchemyCampaignMetricsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def sent_count(self, campaign_id: str) -> int:
        return self._session.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.campaign_id == campaign_id, *sent_message_filters()).scalar() or 0

    def unique_openers(self, campaign_id: str) -> int:
        return self._session.query(unique_openers_expr()).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).filter(EmailMessage.campaign_id == campaign_id, EmailEvent.event_type == EmailEvent.EVENT_OPEN).scalar() or 0

    def event_counts(self, campaign_id: str) -> dict[int, int]:
        """Distinct messages per counted event type."""
        rows = self._session.query(EmailEvent.event_type, func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailEvent.message_id == EmailMessage.message_id
        ).filter(EmailMessage.campaign_id == campaign_id, EmailEvent.event_type.in_(COUNTED_EVENTS)
                 ).group_by(EmailEvent.event_type).all()
        return {r[0]: r[1] for r in rows}

    def prospect_status_count(self, campaign_id: str, statuses: Sequence[str]) -> int:
        status_filter = (CampaignProspect.status.in_(list(statuses)) if len(statuses) > 1
                         else CampaignProspect.status == statuses[0])
        return self._session.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id, status_filter).scalar() or 0

    def bulk_sent(self, campaign_ids: Sequence[str]) -> dict[str, int]:
        rows = self._session.query(EmailMessage.campaign_id, func.count(EmailMessage.message_id)).filter(
            EmailMessage.campaign_id.in_(campaign_ids), *sent_message_filters()
        ).group_by(EmailMessage.campaign_id).all()
        return {r[0]: r[1] for r in rows}

    def bulk_openers(self, campaign_ids: Sequence[str]) -> dict[str, int]:
        return dict(self._session.query(EmailMessage.campaign_id, unique_openers_expr()).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).filter(EmailMessage.campaign_id.in_(campaign_ids), EmailEvent.event_type == EmailEvent.EVENT_OPEN
                 ).group_by(EmailMessage.campaign_id).all())

    def bulk_event_counts(self, campaign_ids: Sequence[str]) -> list[tuple[str, int, int]]:
        rows: list[Any] = self._session.query(
            EmailMessage.campaign_id, EmailEvent.event_type, func.count(func.distinct(EmailEvent.message_id))
        ).join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id).filter(
            EmailMessage.campaign_id.in_(campaign_ids), EmailEvent.event_type.in_(COUNTED_EVENTS)
        ).group_by(EmailMessage.campaign_id, EmailEvent.event_type).all()
        return [(cid, etype, count) for cid, etype, count in rows]

    def bulk_prospect_status_counts(self, campaign_ids: Sequence[str], statuses: Sequence[str]) -> list[tuple[str, int]]:
        status_filter = (CampaignProspect.status.in_(list(statuses)) if len(statuses) > 1
                         else CampaignProspect.status == statuses[0])
        rows: list[Any] = self._session.query(CampaignProspect.campaign_id, func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id.in_(campaign_ids), status_filter
        ).group_by(CampaignProspect.campaign_id).all()
        return [(cid, count) for cid, count in rows]
