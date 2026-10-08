# app/core/auth.py
"""
FastAPI authentication dependencies.
Provides get_current_user, require_role, and require_permission for route-level protection.

Role hierarchy (highest → lowest):
  PLATFORM_ADMIN — Neutrino Tech staff. tenant_id is NULL. Platform routes only.
  SUPER_ADMIN    — Tenant creator. Full control inside their tenant.
  ADMIN          — Tenant administrator.
  MANAGER        — Runs campaigns; cannot manage users.
  AGENT          — Works assigned campaigns.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=True)

# Tenants in these states cannot sign in or call the API (PLATFORM_ADMIN has no tenant and is unaffected)
BLOCKED_TENANT_STATUSES = frozenset({"SUSPENDED", "INACTIVE", "DELETED"})


def ensure_tenant_active(db: Session, user: User) -> None:
    """Raise 403 "Tenant suspended" when the user's tenant is suspended/inactive."""
    if user.role == "PLATFORM_ADMIN" or not user.tenant_id:
        return
    from app.models.tenant import Tenant
    tenant_status = db.query(Tenant.status).filter(Tenant.tenant_id == user.tenant_id).scalar()
    if (tenant_status or "ACTIVE").upper() in BLOCKED_TENANT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant suspended",
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Extract and validate the Bearer JWT, then load the user from DB.
    Raises 401 if the token is missing, expired, or the user doesn't exist.
    PLATFORM_ADMIN users have tenant_id = NULL — this is allowed.
    """
    token = credentials.credentials
    try:
        payload = decode_access_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    user = db.query(User).filter(User.user_id == user_id).first()
    if not user or user.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    ensure_tenant_active(db, user)
    return user


def require_role(*allowed_roles: str):
    """
    Factory that returns a dependency which checks the current user's role.

    Usage:
        @router.get("/admin-only")
        def admin_endpoint(current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN"))):
            ...

    Note: PLATFORM_ADMIN is intentionally excluded from all tenant business routes.
    """

    async def _role_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        # PLATFORM_ADMIN must never access tenant business routes
        if current_user.role == "PLATFORM_ADMIN" and "PLATFORM_ADMIN" not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Platform admins cannot access tenant data.",
            )
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not authorised for this action",
            )
        return current_user

    return _role_checker


# Default permissions per role — must stay in sync with user_router.py
_DEFAULT_PERMS: dict[str, frozenset] = {
    "PLATFORM_ADMIN": frozenset(["manage_platform"]),          # platform-only
    "SUPER_ADMIN":    frozenset(["manage_campaigns", "manage_templates", "manage_inboxes",
                                 "manage_prospects", "view_analytics", "export_data", "manage_team"]),
    "ADMIN":          frozenset(["manage_campaigns", "manage_templates", "manage_inboxes",
                                 "manage_prospects", "view_analytics", "export_data", "manage_team"]),
    "MANAGER":        frozenset(["manage_campaigns", "manage_templates", "view_analytics"]),
}


def require_permission(permission: str):
    """
    Dependency that enforces a feature-level permission.
    - SUPER_ADMIN always passes (within tenant).
    - PLATFORM_ADMIN is blocked from all tenant permissions.
    - Others are checked against custom_permissions (if set) or role defaults.
    """

    async def _checker(current_user: User = Depends(get_current_user)) -> None:
        # PLATFORM_ADMIN cannot hold any tenant permission
        if current_user.role == "PLATFORM_ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Platform admins cannot access tenant data.",
            )

        # SUPER_ADMIN bypasses all tenant permission checks
        if current_user.role == "SUPER_ADMIN":
            return

        effective: frozenset = (
            frozenset(current_user.custom_permissions)
            if current_user.custom_permissions is not None
            else _DEFAULT_PERMS.get(current_user.role, frozenset())
        )

        if permission not in effective:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your account does not have the '{permission}' permission.",
            )

    return _checker
