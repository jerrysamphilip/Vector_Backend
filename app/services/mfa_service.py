"""
Two-factor sign-in (TOTP, app/core/totp.py; chunk A5): the second sign-in step, enrolment, recovery codes,
turning it off, and the workspace policy that requires it.

``MfaService`` serves the /api/auth/mfa/* routes of app/routers/auth_router.py, one unit of work per call.
Wrong codes count towards a per-user (and per pending sign-in) lockout (app/core/rate_limit.py). A TOTP
time step and a recovery code are each accepted once, also under concurrency (conditional updates in
app/repositories/identity.py).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.core import rate_limit, totp
from app.core.clock import utcnow
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.core.security import decode_mfa_token, verify_password
from app.core.tokens import TokenError
from app.domain.identity import UserStatus
from app.models.user import User
from app.repositories.identity import IdentityAccess, IdentityUnitOfWork, Session, identity_uow
from app.services.audit_service import AuditService
from app.services.auth_session import issue_session
from app.services.identity_common import AuthenticationFailed, ensure_tenant_active

__all__ = ["MfaService", "mfa_audit"]

logger = logging.getLogger(__name__)

SIGN_IN_EXPIRED = "Your sign-in expired. Please sign in again."
NOT_ON = "Two-factor authentication is not on."
# One session per mfa_token: a used token is remembered for its whole lifetime
_TOKEN_USED_LIMIT = (1, 600)


def mfa_audit(session: Any, user: Any, action: str, entity_type: str = "mfa") -> None:
    """Audit-log a two-factor change (ENABLE_MFA, DISABLE_MFA, RECOVERY_CODE_USED_MFA, ...)."""
    logger.info("Two-factor %s %s for user %s", entity_type, action, user.user_id)
    if not user.tenant_id:
        return  # PLATFORM_ADMIN: no tenant to file the audit row under
    try:
        AuditService(session).log_action(user.tenant_id, user.user_id, action, entity_type, user.user_id)
    except Exception:  # noqa: BLE001 - the audit entry never blocks the change itself
        logger.exception("Could not write the two-factor audit entry")


def _accept_totp(access: IdentityAccess, user: User, code: Optional[str]) -> bool:
    """Accept a current TOTP code once: records its time step atomically so it can't be replayed."""
    step = totp.matching_step(user.mfa_secret, code, user.mfa_last_step)
    if step is None:
        return False
    return access.users.accept_totp_step(user, step)


def _accept_recovery_code(access: IdentityAccess, user: User, code: Optional[str]) -> bool:
    """Accept an unused recovery code and burn it (single use, also under concurrency)."""
    stored = user.mfa_recovery_codes
    remaining = totp.consume_recovery_code(stored, code or "")
    if remaining is None:
        return False
    return access.users.replace_recovery_codes(user, stored, remaining)


def check_second_factor(uow: IdentityUnitOfWork, user: User, code: Optional[str], recovery_code: Optional[str],
                        mfa_jti: Optional[str] = None, allow_recovery: bool = True) -> str:
    """Verify a TOTP or recovery code with per-user (and per pending sign-in) lockout.
    Returns "totp" or "recovery"; raises 401 on a wrong code, 429 when locked."""
    rate_limit.check_mfa_allowed(user.user_id, mfa_jti)
    used = None
    if code and _accept_totp(uow, user, code):
        used = "totp"
    elif allow_recovery and recovery_code and _accept_recovery_code(uow, user, recovery_code):
        used = "recovery"
    if not used:
        uow.rollback()
        rate_limit.record_mfa_failure(user.user_id, mfa_jti)
        raise AuthenticationFailed("Invalid authentication code")
    rate_limit.record_mfa_success(user.user_id)
    if used == "recovery":
        mfa_audit(uow.session, user, "RECOVERY_CODE_USED")
    return used


def _check_password(user: User, password: Optional[str]) -> None:
    """Accounts with a password confirm it (wrong ones count towards the two-factor lockout)."""
    if not user.password_hash:
        return
    rate_limit.check_mfa_allowed(user.user_id)
    if not password or not verify_password(password, user.password_hash):
        rate_limit.record_mfa_failure(user.user_id)
        raise AuthenticationFailed("Password is incorrect")


class MfaService:
    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> MfaService:
        return cls(identity_uow(db))

    def verify(self, mfa_token: str, code: Optional[str], recovery_code: Optional[str]) -> dict:
        """Second sign-in step: the mfa_token from the first step plus a code gives a session."""
        try:
            payload = decode_mfa_token(mfa_token)
        except TokenError:
            raise AuthenticationFailed(SIGN_IN_EXPIRED)
        jti = payload["jti"]
        if rate_limit.limiter.blocked(f"mfa-token-used:{jti}", *_TOKEN_USED_LIMIT):
            raise AuthenticationFailed(SIGN_IN_EXPIRED)
        if not (code or recovery_code):
            raise BadRequestError("Enter the code from your authenticator app or a recovery code")
        with self.uow as uow:
            user = uow.users.get(payload["sub"])
            if not user or user.status != UserStatus.ACTIVE or not user.mfa_enabled:
                raise AuthenticationFailed(SIGN_IN_EXPIRED)
            ensure_tenant_active(uow, user)
            check_second_factor(uow, user, code, recovery_code, mfa_jti=jti)
            rate_limit.limiter.hit(f"mfa-token-used:{jti}", *_TOKEN_USED_LIMIT)
            return issue_session(uow, user, first_login=bool(payload.get("fl")))

    def setup(self, user: User) -> dict:
        """Start enrolment: a new secret (not active until ``enable`` confirms a code from it)."""
        if user.mfa_enabled:
            raise ConflictError("Two-factor authentication is already on. "
                                "Turn it off first to use a new authenticator.")
        with self.uow as uow:
            secret = totp.generate_secret()
            user.mfa_secret = secret
            user.mfa_last_step = None
            uow.commit()
        return {
            "secret": secret,
            "otpauth_uri": totp.provisioning_uri(secret, user.email),
            "issuer": totp.ISSUER,
            "account": user.email,
        }

    def enable(self, user: User, code: str) -> dict:
        """Confirm the authenticator with a code and turn two-factor on; the recovery codes are returned
        once (only their hashes are kept)."""
        if user.mfa_enabled:
            raise ConflictError("Two-factor authentication is already on.")
        if not user.mfa_secret:
            raise BadRequestError("Start the setup first.")
        with self.uow as uow:
            check_second_factor(uow, user, code, None, allow_recovery=False)
            codes, hashes = totp.generate_recovery_codes()
            user.mfa_enabled = True
            user.mfa_enabled_at = utcnow()
            user.mfa_recovery_codes = hashes
            mfa_audit(uow.session, user, "ENABLE")
            uow.commit()
        return {"mfa_enabled": True, "recovery_codes": codes}

    def disable(self, user: User, password: Optional[str], code: Optional[str], recovery_code: Optional[str]) -> dict:
        """Turn two-factor off: the password (accounts that have one) and a code or recovery code. Not while
        the workspace requires two-factor."""
        if not user.mfa_enabled:
            raise BadRequestError(NOT_ON)
        if user.tenant is not None and user.tenant.require_mfa:
            raise PermissionDenied("Your workspace requires two-factor authentication, so it can't be turned off.")
        _check_password(user, password)
        if not (code or recovery_code):
            raise BadRequestError("Enter a code from your authenticator app or a recovery code")
        with self.uow as uow:
            check_second_factor(uow, user, code, recovery_code)
            user.mfa_enabled = False
            user.mfa_secret = None
            user.mfa_recovery_codes = None
            user.mfa_enabled_at = None
            user.mfa_last_step = None
            mfa_audit(uow.session, user, "DISABLE")
            uow.commit()
        return {"mfa_enabled": False}

    def regenerate_recovery_codes(self, user: User, code: str) -> dict:
        """Replace all recovery codes (needs a current authenticator code); the new ones are returned once."""
        if not user.mfa_enabled:
            raise BadRequestError(NOT_ON)
        with self.uow as uow:
            check_second_factor(uow, user, code, None, allow_recovery=False)
            codes, hashes = totp.generate_recovery_codes()
            user.mfa_recovery_codes = hashes
            mfa_audit(uow.session, user, "REGENERATE_RECOVERY_CODES")
            uow.commit()
        return {"recovery_codes": codes}

    def set_tenant_policy(self, user: User, require_mfa: bool) -> dict:
        """SUPER_ADMIN: require two-factor for everyone in the workspace (or stop requiring it)."""
        with self.uow as uow:
            tenant = uow.tenants.get(user.tenant_id)
            if not tenant:
                raise NotFoundError("Workspace not found")
            if require_mfa and not user.mfa_enabled:
                raise BadRequestError("Turn on two-factor for your own account before requiring it for everyone.")
            if bool(tenant.require_mfa) != require_mfa:
                tenant.require_mfa = require_mfa
                mfa_audit(uow.session, user, "REQUIRE" if require_mfa else "UNREQUIRE", "tenant_mfa_policy")
                uow.commit()
            return {"require_mfa": bool(tenant.require_mfa)}
