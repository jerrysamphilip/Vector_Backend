"""
Outbound webhooks repository and unit of work (ADR 0009, chunk A7; BR-SF-20).

Endpoints a workspace registers and the delivery outbox the worker sends from. Every tenant entry
point takes the tenant; the worker's due-delivery scan is cross-tenant by design (it runs under the
job's named lock). Never commits; the services own the transaction.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import BLOCKED_TENANT_STATUSES
from app.domain.webhooks import DeliveryFilter
from app.models.campaign import Campaign
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, SalesStage
from app.models.tenant import Tenant
from app.models.user import User
from app.models.webhook import WebhookDelivery, WebhookEndpoint
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId

__all__ = [
    "Session", "SqlAlchemyWebhookRepository", "SqlAlchemyWebhooksUnitOfWork", "WebhookRepository",
    "WebhooksAccess", "WebhooksUnitOfWork", "borrow", "webhooks_uow",
]


class WebhookRepository(Protocol):
    # endpoints
    def endpoints(self, tenant_id: TenantId) -> list[WebhookEndpoint]: ...
    def endpoint(self, tenant_id: TenantId, endpoint_id: str) -> Optional[WebhookEndpoint]: ...
    def endpoint_by_id(self, endpoint_id: str) -> Optional[WebhookEndpoint]: ...
    def count_endpoints(self, tenant_id: TenantId) -> int: ...
    def subscribers(self, tenant_id: Optional[str]) -> list[tuple[str, Any]]: ...
    # deliveries
    def deliveries(self, tenant_id: TenantId, endpoint_id: str,
                   criteria: DeliveryFilter) -> tuple[list[WebhookDelivery], int]: ...
    def delivery(self, tenant_id: TenantId, endpoint_id: str, delivery_id: str) -> Optional[WebhookDelivery]: ...
    def delivery_by_id(self, delivery_id: str) -> Optional[WebhookDelivery]: ...
    def due(self, now: datetime, limit: int) -> list[tuple[WebhookDelivery, WebhookEndpoint]]: ...
    def cancel_pending(self, endpoint_id: str, reason: str) -> int: ...
    def delete_deliveries(self, endpoint_id: str) -> int: ...
    def purge_before(self, cutoff: datetime) -> int: ...
    # payload sources
    def active_admin_ids(self, tenant_id: TenantId) -> list[str]: ...
    def prospect(self, prospect_id: Optional[str]) -> Optional[Prospect]: ...
    def lead(self, lead_id: str) -> Optional[Lead]: ...
    def opportunity(self, opportunity_id: str) -> Optional[Opportunity]: ...
    def stage(self, stage_id: Optional[str]) -> Optional[SalesStage]: ...
    def campaign(self, campaign_id: str) -> Optional[Campaign]: ...
    # writes
    def add(self, entity: Any) -> Any: ...
    def delete(self, entity: Any) -> None: ...
    def refresh(self, entity: Any) -> None: ...


class SqlAlchemyWebhookRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # endpoints ---------------------------------------------------------------
    def endpoints(self, tenant_id: TenantId) -> list[WebhookEndpoint]:
        return self._session.query(WebhookEndpoint).filter(WebhookEndpoint.tenant_id == tenant_id) \
            .order_by(WebhookEndpoint.created_at.desc()).all()

    def endpoint(self, tenant_id: TenantId, endpoint_id: str) -> Optional[WebhookEndpoint]:
        return self._session.query(WebhookEndpoint).filter(WebhookEndpoint.endpoint_id == endpoint_id,
                                                           WebhookEndpoint.tenant_id == tenant_id).first()

    def endpoint_by_id(self, endpoint_id: str) -> Optional[WebhookEndpoint]:
        return self._session.get(WebhookEndpoint, endpoint_id)

    def count_endpoints(self, tenant_id: TenantId) -> int:
        return self._session.query(WebhookEndpoint).filter(WebhookEndpoint.tenant_id == tenant_id).count()

    def subscribers(self, tenant_id: Optional[str]) -> list[tuple[str, Any]]:
        """Active endpoints of the workspace as (endpoint_id, event types)."""
        rows = self._session.query(WebhookEndpoint.endpoint_id, WebhookEndpoint.event_types).filter(
            WebhookEndpoint.tenant_id == tenant_id, WebhookEndpoint.active.is_(True)).all()
        return [(eid, types) for eid, types in rows]

    # deliveries --------------------------------------------------------------
    def deliveries(self, tenant_id: TenantId, endpoint_id: str,
                   criteria: DeliveryFilter) -> tuple[list[WebhookDelivery], int]:
        query = self._session.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == endpoint_id,
                                                            WebhookDelivery.tenant_id == tenant_id)
        if criteria.status:
            query = query.filter(WebhookDelivery.status == criteria.status)
        if criteria.event_type:
            query = query.filter(WebhookDelivery.event_type == criteria.event_type)
        total = query.count()
        rows = query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.delivery_id) \
            .offset(criteria.offset).limit(criteria.limit).all()
        return rows, total

    def delivery(self, tenant_id: TenantId, endpoint_id: str, delivery_id: str) -> Optional[WebhookDelivery]:
        return self._session.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == delivery_id,
                                                           WebhookDelivery.endpoint_id == endpoint_id,
                                                           WebhookDelivery.tenant_id == tenant_id).first()

    def delivery_by_id(self, delivery_id: str) -> Optional[WebhookDelivery]:
        return self._session.get(WebhookDelivery, delivery_id)

    def due(self, now: datetime, limit: int) -> list[tuple[WebhookDelivery, WebhookEndpoint]]:
        """Due PENDING deliveries of active endpoints. Those of suspended / deleted workspaces (DEC-20) stay
        PENDING, untouched, and go out once the workspace is reactivated."""
        rows = self._session.query(WebhookDelivery, WebhookEndpoint).join(
            WebhookEndpoint, WebhookEndpoint.endpoint_id == WebhookDelivery.endpoint_id
        ).join(Tenant, Tenant.tenant_id == WebhookEndpoint.tenant_id
               ).filter(WebhookDelivery.status == "PENDING", WebhookDelivery.next_attempt_at <= now,
                        WebhookEndpoint.active.is_(True),
                        func.upper(func.coalesce(Tenant.status, "ACTIVE")).notin_(sorted(BLOCKED_TENANT_STATUSES))
                        ).order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.created_at).limit(limit).all()
        return [(d, ep) for d, ep in rows]

    def cancel_pending(self, endpoint_id: str, reason: str) -> int:
        return self._session.query(WebhookDelivery).filter(
            WebhookDelivery.endpoint_id == endpoint_id, WebhookDelivery.status == "PENDING"
        ).update({WebhookDelivery.status: "CANCELLED", WebhookDelivery.last_error: reason[:500],
                  WebhookDelivery.next_attempt_at: None}, synchronize_session=False)

    def delete_deliveries(self, endpoint_id: str) -> int:
        return self._session.query(WebhookDelivery).filter(
            WebhookDelivery.endpoint_id == endpoint_id).delete(synchronize_session=False)

    def purge_before(self, cutoff: datetime) -> int:
        return self._session.query(WebhookDelivery).filter(
            WebhookDelivery.created_at < cutoff, WebhookDelivery.status != "PENDING").delete(synchronize_session=False)

    # payload sources -----------------------------------------------------------
    def active_admin_ids(self, tenant_id: TenantId) -> list[str]:
        return [u for (u,) in self._session.query(User.user_id).filter(
            User.tenant_id == tenant_id, User.role.in_(("SUPER_ADMIN", "ADMIN")), User.status == "ACTIVE")]

    def get(self, model: type, entity_id: Any) -> Any:
        return self._session.get(model, entity_id) if entity_id else None

    def prospect(self, prospect_id: Optional[str]) -> Optional[Prospect]:
        return self.get(Prospect, prospect_id)

    def lead(self, lead_id: str) -> Optional[Lead]:
        return self.get(Lead, lead_id)

    def opportunity(self, opportunity_id: str) -> Optional[Opportunity]:
        return self.get(Opportunity, opportunity_id)

    def stage(self, stage_id: Optional[str]) -> Optional[SalesStage]:
        return self.get(SalesStage, stage_id)

    def campaign(self, campaign_id: str) -> Optional[Campaign]:
        return self.get(Campaign, campaign_id)

    # writes ------------------------------------------------------------------
    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def add_all(self, entities: Collection[Any]) -> None:
        self._session.add_all(list(entities))

    def delete(self, entity: Any) -> None:
        self._session.delete(entity)

    def refresh(self, entity: Any) -> None:
        self._session.refresh(entity)


class WebhooksAccess(Protocol):
    @property
    def webhooks(self) -> WebhookRepository: ...

    @property
    def session(self) -> Session: ...


@dataclass(frozen=True)
class _Borrowed:
    session: Session

    @property
    def webhooks(self) -> SqlAlchemyWebhookRepository:
        return SqlAlchemyWebhookRepository(self.session)


def borrow(session: Session) -> WebhooksAccess:
    """Repository access on a caller's session; the caller owns the transaction."""
    return _Borrowed(session)


class WebhooksUnitOfWork(Protocol):
    @property
    def webhooks(self) -> WebhookRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> WebhooksUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyWebhooksUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyWebhooksUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyWebhooksUnitOfWork:
        super().__enter__()
        return self

    @property
    def webhooks(self) -> SqlAlchemyWebhookRepository:
        return SqlAlchemyWebhookRepository(self.session)


def webhooks_uow(session: Session) -> SqlAlchemyWebhooksUnitOfWork:
    return SqlAlchemyWebhooksUnitOfWork.for_session(session)
