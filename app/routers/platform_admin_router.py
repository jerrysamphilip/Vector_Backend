# app/routers/platform_admin_router.py
"""
Platform Admin routes — accessible only to PLATFORM_ADMIN users.
Provides cross-tenant management: tenant listing, user overview, platform stats.

Layered (ADR 0009, chunk A5): each handler makes one call to PlatformAdminService
(app/services/platform_admin_service.py), injected through ``platform_service`` so tests swap it with
``app.dependency_overrides``.
"""

import logging

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.auth import require_role
from app.core.database import get_db
from app.schemas.platform_admin_schema import (
    CreateTenantRequest,
    PlatformStats,
    TenantDetail,
    TenantSummary,
    UpdateTenantStatusRequest,
    UserSummary,
)
from app.services.platform_admin_service import PlatformAdminService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["Platform Admin"],
    dependencies=[Depends(require_role("PLATFORM_ADMIN"))],
)


# Service provider: one unit of work on the request's session (get_db)
def platform_service(db=Depends(get_db)) -> PlatformAdminService:
    return PlatformAdminService.for_session(db)


Platform = Depends(platform_service)


# ── Endpoints ────────────────────────────────────────────────

@router.get("/stats", response_model=PlatformStats)
def get_platform_stats(
    current_user=Depends(require_role("PLATFORM_ADMIN")),
    svc: PlatformAdminService = Platform,
):
    """Platform-wide aggregate statistics."""
    return svc.stats()


@router.get("/tenants", response_model=list[TenantSummary])
def list_tenants(
    search: str = Query(None, description="Search by tenant name"),
    status_filter: str = Query(None, alias="status", description="Filter by status: ACTIVE, SUSPENDED"),
    current_user=Depends(require_role("PLATFORM_ADMIN")),
    svc: PlatformAdminService = Platform,
):
    """List all tenants with user/campaign counts."""
    return svc.list_tenants(search, status_filter)


@router.get("/tenants/{tenant_id}", response_model=TenantDetail)
def get_tenant_detail(
    tenant_id: str,
    current_user=Depends(require_role("PLATFORM_ADMIN")),
    svc: PlatformAdminService = Platform,
):
    """Detailed view of a single tenant — includes user list."""
    return svc.tenant_detail(tenant_id)


@router.put("/tenants/{tenant_id}/status")
def update_tenant_status(
    tenant_id: str,
    body: UpdateTenantStatusRequest,
    current_user=Depends(require_role("PLATFORM_ADMIN")),
    svc: PlatformAdminService = Platform,
):
    """Suspend or reactivate a tenant."""
    return svc.update_tenant_status(current_user, tenant_id, body.status)


@router.get("/users", response_model=list[UserSummary])
def list_all_users(
    search: str = Query(None, description="Search by email or name"),
    role: str = Query(None, description="Filter by role"),
    tenant_id: str = Query(None, description="Filter by tenant"),
    current_user=Depends(require_role("PLATFORM_ADMIN")),
    svc: PlatformAdminService = Platform,
):
    """List all users across all tenants (excludes PLATFORM_ADMIN users)."""
    return svc.list_users(search, role, tenant_id)


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    current_user=Depends(require_role("PLATFORM_ADMIN")),
    svc: PlatformAdminService = Platform,
):
    """Create a new tenant and its initial Super Admin user, then send an activation invite."""
    return svc.create_tenant(body.tenant_name, body.admin_email, body.admin_first_name, body.admin_last_name,
                             request)
