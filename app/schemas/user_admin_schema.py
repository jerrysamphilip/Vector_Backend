# app/schemas/user_admin_schema.py
"""Request and response bodies of the team management API (app/routers/user_router.py)."""

from datetime import datetime

from pydantic import BaseModel, EmailStr


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "AGENT"


class AcceptInviteRequest(BaseModel):
    token: str
    first_name: str
    last_name: str
    password: str


class ChangeRoleRequest(BaseModel):
    role: str


class UserOut(BaseModel):
    user_id: str
    tenant_id: str
    first_name: str
    last_name: str
    email: str
    role: str
    status: str | None  # S10: column is nullable
    auth_provider: str
    email_verified: bool
    avatar_url: str | None
    created_at: datetime | None
    last_login_at: datetime | None
    permissions: list[str]  # Effective permissions (custom or role default)


class UpdatePermissionsRequest(BaseModel):
    permissions: list[str]
