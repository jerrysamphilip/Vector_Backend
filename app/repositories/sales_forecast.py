"""
Forecast snapshot repository (ADR 0009, chunk A3; BRD section 8 forecast accuracy). Tenant-scoped;
never commits. ``active_tenant_ids`` is the worker loop's entry point across workspaces.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, Optional, Protocol

from sqlalchemy import func

from app.models.sales import Opportunity, SalesStage
from app.models.sales_extra import ForecastSnapshot
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.base import TenantId
from app.repositories.sales_common import SessionRepository

ForecastRow = tuple[str, str, str, Optional[str], int, Any]
"""(owner_id, status, stage_id, forecast_category, deal count, amount) per group."""


class ForecastRepository(Protocol):
    def stages(self, tenant_id: TenantId) -> dict[str, SalesStage]: ...
    def grouped_deals(self, tenant_id: TenantId, start: date, end: date) -> list[ForecastRow]: ...
    def won_amount(self, tenant_id: TenantId, start: date, end: date, owners: Optional[Iterable[str]]) -> Any: ...
    def snapshot_count(self, tenant_id: TenantId, period_type: str, start: date) -> int: ...
    def delete_snapshots(self, tenant_id: TenantId, period_type: str, start: date) -> None: ...
    def active_sales_users(self, tenant_id: TenantId, roles: Sequence[str]) -> set[str]: ...
    def active_tenant_ids(self) -> list[str]: ...
    def has_committed(self, tenant_id: TenantId, period_type: str, start: date) -> bool: ...
    def snapshot_rows(self, tenant_id: TenantId, period_type: str, start: date,
                      owners: Optional[Iterable[str]]) -> list[ForecastSnapshot]: ...
    def period_starts(self, tenant_id: TenantId, period_type: str, limit: int) -> list[date]: ...
    def add(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...


class SqlAlchemyForecastRepository(SessionRepository):
    def stages(self, tenant_id: TenantId) -> dict[str, SalesStage]:
        return {s.stage_id: s for s in self._session.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)}

    def grouped_deals(self, tenant_id: TenantId, start: date, end: date) -> list[ForecastRow]:
        """Won and open deals with a close date in [start, end), grouped by owner, status, stage and category."""
        rows = self._session.query(
            Opportunity.owner_id, Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category,
            func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).filter(
            Opportunity.tenant_id == tenant_id, Opportunity.close_date >= start, Opportunity.close_date < end,
            Opportunity.status.in_(("WON", "OPEN"))).group_by(
            Opportunity.owner_id, Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category).all()
        return [(o, s, st, c, n, a) for o, s, st, c, n, a in rows]

    def won_amount(self, tenant_id: TenantId, start: date, end: date, owners: Optional[Iterable[str]]) -> Any:
        q = self._session.query(func.coalesce(func.sum(Opportunity.amount), 0)).filter(
            Opportunity.tenant_id == tenant_id, Opportunity.status == "WON",
            Opportunity.close_date >= start, Opportunity.close_date < end)
        if owners is not None:
            q = q.filter(Opportunity.owner_id.in_(set(owners) or {"-"}))
        return q.scalar()

    def _period(self, tenant_id: TenantId, period_type: str, start: date) -> Any:
        return self._session.query(ForecastSnapshot).filter(
            ForecastSnapshot.tenant_id == tenant_id, ForecastSnapshot.period_type == period_type,
            ForecastSnapshot.period_start == start)

    def snapshot_count(self, tenant_id: TenantId, period_type: str, start: date) -> int:
        q = self._period(tenant_id, period_type, start)
        return q.count() if q.first() else 0

    def delete_snapshots(self, tenant_id: TenantId, period_type: str, start: date) -> None:
        self._period(tenant_id, period_type, start).delete(synchronize_session=False)

    def active_sales_users(self, tenant_id: TenantId, roles: Sequence[str]) -> set[str]:
        return {u for (u,) in self._session.query(User.user_id).filter(
            User.tenant_id == tenant_id, User.status == "ACTIVE", User.role.in_(tuple(roles)))}

    def active_tenant_ids(self) -> list[str]:
        return [t for (t,) in self._session.query(Tenant.tenant_id).filter(Tenant.status == "ACTIVE")]

    def has_committed(self, tenant_id: TenantId, period_type: str, start: date) -> bool:
        """Whether any row of the period carries the committed figure (snapshots before 0014 do not)."""
        return self._period(tenant_id, period_type, start).filter(
            ForecastSnapshot.committed.isnot(None)).first() is not None

    def snapshot_rows(self, tenant_id: TenantId, period_type: str, start: date,
                      owners: Optional[Iterable[str]]) -> list[ForecastSnapshot]:
        q = self._period(tenant_id, period_type, start)
        if owners is not None:
            q = q.filter(ForecastSnapshot.owner_id.in_(set(owners) or {"-"}))
        return q.all()

    def period_starts(self, tenant_id: TenantId, period_type: str, limit: int) -> list[date]:
        return [s for (s,) in self._session.query(ForecastSnapshot.period_start).filter(
            ForecastSnapshot.tenant_id == tenant_id, ForecastSnapshot.period_type == period_type).distinct()
            .order_by(ForecastSnapshot.period_start.desc()).limit(limit)]
