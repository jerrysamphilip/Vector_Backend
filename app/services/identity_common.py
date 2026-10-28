"""
Shared pieces of the identity services (auth, MFA, team, invitations, platform admin; chunk A5): the errors
they add to app/core/errors.py, the translation of ``app.domain.identity`` rule errors, and the tenant
gate every sign-in passes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Optional

from app.core.errors import BadRequestError, ConflictError, DomainError, NotFoundError, PermissionDenied
from app.domain.identity import PLATFORM_ADMIN, IdentityRuleError, tenant_blocked
from app.repositories.identity import IdentityAccess, TenantFlags

__all__ = ["AuthenticationFailed", "NotConfigured", "SIGNUP_DISABLED", "TENANT_SUSPENDED", "domain_error",
           "ensure_tenant_active", "identity_rules", "tenant_flags"]

SIGNUP_DISABLED = "Self sign-up is disabled. Ask your administrator for an invitation."
TENANT_SUSPENDED = "Tenant suspended"


class AuthenticationFailed(DomainError):
    """401: the credential (password, token, code) is missing, wrong or expired. Same body as the
    HTTPException(401) it replaced: {"detail": message}."""

    status_code = 401
    default_code = "UNAUTHENTICATED"


class NotConfigured(DomainError):
    """501: the feature is not configured on this deployment (e.g. Google sign-in)."""

    status_code = 501
    default_code = "NOT_CONFIGURED"


_BY_STATUS: dict[int, type[DomainError]] = {
    400: BadRequestError, 401: AuthenticationFailed, 403: PermissionDenied, 404: NotFoundError, 409: ConflictError,
}


def domain_error(exc: IdentityRuleError) -> DomainError:
    """The app.core.errors error for a refused identity rule (same status and message)."""
    return _BY_STATUS.get(exc.status, BadRequestError)(exc.message)


@contextmanager
def identity_rules() -> Iterator[None]:
    """Run app.domain.identity rules; a refusal becomes the matching domain error."""
    try:
        yield
    except IdentityRuleError as exc:
        raise domain_error(exc) from exc


def tenant_flags(access: IdentityAccess, user: Any) -> Optional[TenantFlags]:
    """Status and two-factor policy of the user's tenant; None for PLATFORM_ADMIN / no tenant."""
    if user.role == PLATFORM_ADMIN or not user.tenant_id:
        return None
    return access.tenants.flags(user.tenant_id)


def ensure_tenant_active(access: IdentityAccess, user: Any) -> None:
    """403 "Tenant suspended" when the user's tenant is suspended / inactive / deleted."""
    flags = tenant_flags(access, user)
    if tenant_blocked(flags.status if flags else None):
        raise PermissionDenied(TENANT_SUSPENDED)
