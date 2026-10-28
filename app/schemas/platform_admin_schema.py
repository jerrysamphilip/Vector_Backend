"""Request and response bodies of the platform admin API (app/routers/platform_admin_router.py)."""

from pydantic import BaseModel, EmailStr


class TenantSummary(BaseModel):
    tenant_id: str
    tenant_name: str
    status: str
    user_count: int
    campaign_count: int
    created_at: str | None


class TenantDetail(BaseModel):
    tenant_id: str
    tenant_name: str
    status: str
    created_at: str | None
    updated_at: str | None
    users: list[dict]
    campaign_count: int


class PlatformStats(BaseModel):
    total_tenants: int
    active_tenants: int
    suspended_tenants: int
    total_users: int
    active_users: int
    total_campaigns: int


class UpdateTenantStatusRequest(BaseModel):
    status: str  # ACTIVE | SUSPENDED


class UserSummary(BaseModel):
    user_id: str
    email: str
    first_name: str
    last_name: str
    role: str
    status: str
    tenant_id: str | None
    tenant_name: str | None
    last_login_at: str | None
    created_at: str | None


class CreateTenantRequest(BaseModel):
    tenant_name: str
    admin_email: EmailStr
    admin_first_name: str
    admin_last_name: str
