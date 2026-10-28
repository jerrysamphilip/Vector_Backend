"""
Issuing a session after sign-in (chunk A5): the access + refresh token pair, the two-factor hand-off and
the user profile every sign-in and /api/auth/me return.

Token encoding stays behind app/core/security.py -> app/core/tokens.py (S1). Refresh tokens carry the
session lineage (app/services/session_limits.py). Each function works inside the caller's identity unit
of work and commits it when the session is issued.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.clock import utcnow
from app.core.security import create_access_token, create_mfa_token, generate_refresh_token, hash_token
from app.domain.identity import PLATFORM_ADMIN, effective_permissions
from app.repositories.identity import IdentityUnitOfWork
from app.services import session_limits
from app.services.user_activity import record_for

__all__ = ["complete_sign_in", "issue_session", "user_profile"]


def _workspace(user: Any) -> Any:
    """The user's tenant for the two-factor policy (None for PLATFORM_ADMIN / no tenant)."""
    return getattr(user, "tenant", None) if user.tenant_id and user.role != PLATFORM_ADMIN else None


def user_profile(user: Any) -> dict:
    """The signed-in user as /api/auth/me, /session and every sign-in return it."""
    tenant = _workspace(user)
    require_mfa = bool(tenant is not None and tenant.require_mfa)
    tenant_row = getattr(user, "tenant", None)
    return {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "tenant_name": tenant_row.tenant_name if tenant_row else None,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "auth_provider": user.auth_provider or "local",
        "email_verified": user.email_verified or False,
        "avatar_url": user.avatar_url,
        "permissions": effective_permissions(user.role, user.custom_permissions),
        "sales_level": user.sales_level,
        "manager_id": user.manager_id,
        "mfa_enabled": bool(user.mfa_enabled),
        "mfa_required": require_mfa,
        "mfa_setup_required": require_mfa and not user.mfa_enabled,
    }


def issue_session(uow: IdentityUnitOfWork, user: Any, device_info: Optional[str] = None, first_login: bool = False,
                  lineage: Optional[session_limits.SessionLineage] = None) -> dict:
    """Create access + refresh tokens, persist the refresh token and commit.

    lineage: the session a rotated token belongs to (refresh); None starts a new session (sign-in)."""
    now = utcnow()
    lineage = lineage or session_limits.new_lineage(now)
    access_token = create_access_token(user.user_id, user.tenant_id, user.role)
    raw_refresh = generate_refresh_token()
    uow.tokens.add_refresh(
        user_id=user.user_id,
        token_hash=hash_token(raw_refresh),
        device_info=device_info,
        expires_at=session_limits.refresh_expiry(
            now=now, session_started_at=lineage.session_started_at,
            refresh_ttl=session_limits.refresh_ttl(), absolute=session_limits.absolute_limit()),
        session_id=lineage.session_id,
        session_started_at=lineage.session_started_at,
        last_activity_at=lineage.last_activity_at,
    )
    user.last_login_at = utcnow()
    uow.commit()
    # Signing in counts as activity for the CRM adoption KPI
    record_for(uow.session, user)
    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "user": user_profile(user),
        "first_login": first_login,
    }


def complete_sign_in(uow: IdentityUnitOfWork, user: Any, first_login: bool = False) -> dict:
    """First factor passed (password, magic link, Google, sign-up). Users with two-factor on get a
    short-lived mfa_token to exchange at /api/auth/mfa/verify; everyone else gets a session."""
    if first_login:
        # An invited user has joined: their open invitation is accepted
        uow.invitations.accept_open(user.tenant_id, user.email)
    if user.mfa_enabled:
        uow.commit()  # keep what the first step changed (e.g. magic link marked used)
        return {"mfa_required": True, "mfa_token": create_mfa_token(user.user_id, first_login),
                "first_login": first_login}
    return issue_session(uow, user, first_login=first_login)
