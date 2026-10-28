"""
SQL of the warm-up engine (chunk A8, ADR 0009): the inboxes to warm, daily warm-up metrics, the domain's external
reputation and the activity log. The cycle runs across workspaces (``inboxes_to_warm`` excludes suspended /
deleted ones); the read entry points for the UI name the tenant. Stage and flush only, never commit.
"""

from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.core.auth import BLOCKED_TENANT_STATUSES
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.inbox_warmup_metric import InboxWarmupMetric
from app.models.provider_reputation import ExternalReputationMetric
from app.models.sending_inbox import SendingInbox
from app.models.tenant import Tenant

__all__ = ["Session", "SqlAlchemyWarmupRepository"]


class SqlAlchemyWarmupRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    @classmethod
    def for_session(cls, db: Session) -> "SqlAlchemyWarmupRepository":
        return cls(db)

    def inboxes_to_warm(self, tenant_id: Optional[str] = None) -> List[SendingInbox]:
        """Inboxes of active workspaces (one workspace with ``tenant_id``), by tenant and address."""
        query = self._s.query(SendingInbox).join(Tenant, Tenant.tenant_id == SendingInbox.tenant_id).filter(
            func.upper(func.coalesce(Tenant.status, "ACTIVE")).notin_(sorted(BLOCKED_TENANT_STATUSES)))
        if tenant_id:
            query = query.filter(SendingInbox.tenant_id == tenant_id)
        return query.order_by(SendingInbox.tenant_id, SendingInbox.email_address).all()

    def inbox(self, inbox_id: str) -> Optional[SendingInbox]:
        inbox: Optional[SendingInbox] = self._s.query(SendingInbox).filter(
            SendingInbox.inbox_id == inbox_id).first()
        return inbox

    def tenant_inbox(self, tenant_id: str, inbox_id: str) -> Optional[SendingInbox]:
        inbox: Optional[SendingInbox] = self._s.query(SendingInbox).filter(
            SendingInbox.inbox_id == inbox_id, SendingInbox.tenant_id == tenant_id).first()
        return inbox

    def tenant_inboxes(self, tenant_id: str) -> List[SendingInbox]:
        return self._s.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id).all()

    # ── Daily metrics ───────────────────────────────────────────────────────

    def metric(self, inbox_id: str, metric_date: str) -> Optional[InboxWarmupMetric]:
        metric: Optional[InboxWarmupMetric] = self._s.query(InboxWarmupMetric).filter(
            InboxWarmupMetric.inbox_id == inbox_id,
            InboxWarmupMetric.metric_date == metric_date,
        ).first()
        return metric

    def add_metric(self, **fields: Any) -> InboxWarmupMetric:
        metric = InboxWarmupMetric(**fields)
        self._s.add(metric)
        self._s.flush()
        return metric

    def recent_metrics(self, inbox_id: str, excluding_date: str, limit: int) -> List[InboxWarmupMetric]:
        """The inbox's latest daily metrics other than ``excluding_date``, newest first."""
        return (
            self._s.query(InboxWarmupMetric)
            .filter(InboxWarmupMetric.inbox_id == inbox_id, InboxWarmupMetric.metric_date != excluding_date)
            .order_by(desc(InboxWarmupMetric.metric_date))
            .limit(limit)
            .all()
        )

    def tenant_metric_history(self, tenant_id: str, inbox_id: str, limit: int) -> List[InboxWarmupMetric]:
        return self._s.query(InboxWarmupMetric).filter(
            InboxWarmupMetric.inbox_id == inbox_id,
            InboxWarmupMetric.tenant_id == tenant_id,
        ).order_by(desc(InboxWarmupMetric.metric_date)).limit(limit).all()

    def external_reputation(self, tenant_id: str, domain: str) -> Optional[ExternalReputationMetric]:
        """The domain's latest external reputation metric for this workspace."""
        metric: Optional[ExternalReputationMetric] = self._s.query(ExternalReputationMetric).filter(
            ExternalReputationMetric.tenant_id == tenant_id,
            ExternalReputationMetric.domain_name == domain,
        ).order_by(desc(ExternalReputationMetric.metric_date)).first()
        return metric

    # ── Activity log ────────────────────────────────────────────────────────

    def add_event(self, **fields: Any) -> InboxWarmupEvent:
        event = InboxWarmupEvent(**fields)
        self._s.add(event)
        return event

    def tenant_events(self, tenant_id: str, inbox_id: str, limit: int) -> List[InboxWarmupEvent]:
        return self._s.query(InboxWarmupEvent).filter(
            InboxWarmupEvent.inbox_id == inbox_id,
            InboxWarmupEvent.tenant_id == tenant_id,
        ).order_by(desc(InboxWarmupEvent.event_time)).limit(limit).all()
