"""
Real-time campaign counters repository (ADR 0009): ``campaign_metrics_realtime`` and the recount
from messages and events. Campaign ids are global; callers resolve the campaign in the tenant first.
Never commits (flushes so the counters are visible inside the caller's transaction).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaign import CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage
from app.models.metrics import CampaignMetricsRealtime

__all__ = ["Session", "SqlAlchemyCampaignMetricsRepository", "campaign_metrics_repo"]


class SqlAlchemyCampaignMetricsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, campaign_id: str) -> Optional[CampaignMetricsRealtime]:
        return self._session.query(CampaignMetricsRealtime).filter(
            CampaignMetricsRealtime.campaign_id == campaign_id).first()

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def flush(self) -> None:
        self._session.flush()

    def sent(self, campaign_id: str) -> int:
        return self._session.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.campaign_id == campaign_id, EmailMessage.status == "SENT").scalar() or 0

    def messages_with_event(self, campaign_id: str, event_type: int) -> int:
        """Distinct messages of the campaign with at least one event of ``event_type``."""
        return self._session.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(EmailMessage.campaign_id == campaign_id, EmailEvent.event_type == event_type).scalar() or 0

    def enrolments_in(self, campaign_id: str, statuses: list[str]) -> int:
        return self._session.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id, CampaignProspect.status.in_(statuses)).scalar() or 0


def campaign_metrics_repo(session: Session) -> SqlAlchemyCampaignMetricsRepository:
    return SqlAlchemyCampaignMetricsRepository(session)
