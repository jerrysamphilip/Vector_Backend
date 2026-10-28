"""
Platform admin reads (ADR 0009, chunk A5). These are cross-tenant by design: only PLATFORM_ADMIN routes
(app/routers/platform_admin_router.py) call them. Platform admins themselves are never counted or listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import func

from app.domain.identity import PLATFORM_ADMIN, TenantStatus, UserStatus
from app.models.campaign import Campaign
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.base import TenantId
from app.utils.sql_like import ilike

__all__ = ["PlatformRepository", "PlatformStats", "SqlAlchemyPlatformRepository", "TenantSummaryRow",
           "USER_LIST_LIMIT"]

USER_LIST_LIMIT = 200


@dataclass(frozen=True)
class PlatformStats:
    total_tenants: int
    active_tenants: int
    suspended_tenants: int
    total_users: int
    active_users: int
    total_campaigns: int


@dataclass(frozen=True)
class TenantSummaryRow:
    tenant_id: str
    tenant_name: str
    status: Optional[str]
    created_at: Optional[datetime]
    user_count: int
    campaign_count: int


class PlatformRepository(Protocol):
    def stats(self) -> PlatformStats: ...

    def tenant_summaries(self, search: Optional[str], status: Optional[str]) -> list[TenantSummaryRow]: ...

    def tenant_users(self, tenant_id: TenantId) -> list[User]: ...

    def campaign_count(self, tenant_id: TenantId) -> int: ...

    def users(self, search: Optional[str], role: Optional[str],
              tenant_id: Optional[TenantId]) -> list[tuple[User, Optional[str]]]: ...


class SqlAlchemyPlatformRepository:
    def __init__(self, session: Any) -> None:
        self._session = session

    def _count(self, column: Any, *criteria: Any) -> int:
        return self._session.query(func.count(column)).filter(*criteria).scalar() or 0

    def stats(self) -> PlatformStats:
        not_platform = User.role != PLATFORM_ADMIN
        return PlatformStats(
            total_tenants=self._count(Tenant.tenant_id),
            active_tenants=self._count(Tenant.tenant_id, Tenant.status == TenantStatus.ACTIVE),
            suspended_tenants=self._count(Tenant.tenant_id, Tenant.status == TenantStatus.SUSPENDED),
            total_users=self._count(User.user_id, not_platform),
            active_users=self._count(User.user_id, not_platform, User.status == UserStatus.ACTIVE),
            total_campaigns=self._count(Campaign.campaign_id),
        )

    def tenant_summaries(self, search: Optional[str], status: Optional[str]) -> list[TenantSummaryRow]:
        """Every tenant with its user (not platform admins) and campaign counts, newest first."""
        query = (
            self._session.query(
                Tenant.tenant_id,
                Tenant.tenant_name,
                Tenant.status,
                Tenant.created_at,
                func.count(func.distinct(User.user_id)).label("user_count"),
                func.count(func.distinct(Campaign.campaign_id)).label("campaign_count"),
            )
            .outerjoin(User, User.tenant_id == Tenant.tenant_id)
            .outerjoin(Campaign, Campaign.tenant_id == Tenant.tenant_id)
            .group_by(Tenant.tenant_id)
            .order_by(Tenant.created_at.desc())
        )
        if search:
            query = query.filter(ilike(Tenant.tenant_name, search))
        if status:
            query = query.filter(Tenant.status == status)
        query = query.filter((User.user_id.is_(None)) | (User.role != PLATFORM_ADMIN))
        return [TenantSummaryRow(r.tenant_id, r.tenant_name, r.status, r.created_at, r.user_count, r.campaign_count)
                for r in query.all()]

    def tenant_users(self, tenant_id: TenantId) -> list[User]:
        return (
            self._session.query(User)
            .filter(User.tenant_id == tenant_id, User.role != PLATFORM_ADMIN)
            .order_by(User.created_at.desc())
            .all()
        )

    def campaign_count(self, tenant_id: TenantId) -> int:
        return self._count(Campaign.campaign_id, Campaign.tenant_id == tenant_id)

    def users(self, search: Optional[str], role: Optional[str],
              tenant_id: Optional[TenantId]) -> list[tuple[User, Optional[str]]]:
        """Users of every tenant with their tenant's name, newest first (at most USER_LIST_LIMIT)."""
        query = (
            self._session.query(User, Tenant.tenant_name)
            .outerjoin(Tenant, Tenant.tenant_id == User.tenant_id)
            .filter(User.role != PLATFORM_ADMIN)
            .order_by(User.created_at.desc())
        )
        if search:
            query = query.filter(ilike(User.email, search) | ilike(User.first_name, search)
                                 | ilike(User.last_name, search))
        if role:
            query = query.filter(User.role == role)
        if tenant_id:
            query = query.filter(User.tenant_id == tenant_id)
        return [(u, name) for u, name in query.limit(USER_LIST_LIMIT).all()]
