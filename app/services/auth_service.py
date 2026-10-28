"""
Authentication (chunk A5, ADR 0009): sign-up, password / magic-link / Google sign-in, refresh, sign-out,
password reset and change, and the profile. Two-factor lives in app/services/mfa_service.py.

``AuthService`` serves app/routers/auth_router.py: one method per route, each one unit of work over the
identity repositories (app/repositories/identity*.py) that it commits; errors are domain errors. HTTP
concerns stay in the router: rate limits keyed on the request (passed in as a ``Throttle``), cookies
(the router moves issued tokens into httpOnly cookies) and the refresh refusal response.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any, NamedTuple, Optional, Protocol

from app.core import totp
from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.core.security import decode_access_token, hash_password, hash_token, verify_password
from app.core.tokens import TokenError
from app.domain.identity import (
    SIGN_IN_STATUSES,
    SUPER_ADMIN,
    SUSPENDED_ACCOUNT,
    UserStatus,
    check_password_sign_in,
    email_domain,
    may_link_google,
    password_too_short,
)
from app.models.user import User
from app.repositories.identity import DuplicateAccount, IdentityAccess, IdentityUnitOfWork, Session, identity_uow
from app.services import password_reset_email, session_limits, token_revocation
from app.services.audit_service import AuditService
from app.services.auth_session import complete_sign_in, issue_session, user_profile
from app.services.google_identity import GOOGLE_NOT_CONFIGURED, GoogleIdentity, verify_credential
from app.services.identity_common import (
    SIGNUP_DISABLED,
    AuthenticationFailed,
    NotConfigured,
    ensure_tenant_active,
    identity_rules,
)
from app.services.invitation_email import HeaderSource
from app.services.user_lookup import login_account

__all__ = ["AuthService", "GOOGLE_LINK_REFUSED", "PasswordChange", "Throttle", "link_google_account"]

logger = logging.getLogger(__name__)

GOOGLE_LINK_REFUSED = ("An account with this email already exists. Sign in with your email and password; "
                       "signing in with Google is not linked to existing accounts automatically.")
EMAIL_REGISTERED = "Email already registered"
INVALID_REFRESH = "Invalid or expired refresh token"
INACTIVE_USER = "User not found or inactive"
NEW_PASSWORD_TOO_SHORT = "New password must be at least 8 characters"
FORGOT_PASSWORD_REPLY = {"message": "If an account exists for that email, a password reset link has been sent."}


class Throttle(Protocol):
    """Rate limit of one request (keyed on the client by the router): ``check`` raises 429 when over the
    limit; ``failed`` / ``succeeded`` count a wrong / right password."""

    def check(self) -> None: ...

    def failed(self) -> None: ...

    def succeeded(self) -> None: ...


class PasswordChange(NamedTuple):
    """The response body, and a fresh session for this browser when one was asked for."""

    body: dict
    session: Optional[dict]


def link_google_account(session: Any, user: Any, google_id: str, email: str) -> None:
    """Attach a Google identity to an existing account found by its (Google-verified) email.

    Only for email domains listed in SSO_LINK_ALLOWED_DOMAINS (default: none), otherwise 409: whoever controls
    a Google account for the address must not silently take over a password account (I-12). An account
    already linked to a different Google identity is never re-linked. Every link is audit-logged."""
    domain = email_domain(email)
    if not may_link_google(user.google_id, email, settings.sso_link_allowed_domains):
        logger.warning("Google sign-in refused for existing account %s (auto-link not allowed for %s)", user.user_id, domain)
        raise ConflictError(GOOGLE_LINK_REFUSED)
    user.google_id = google_id
    user.auth_provider = "google"
    logger.info("Google identity linked to existing account %s (domain %s)", user.user_id, domain)
    if user.tenant_id:
        AuditService(session).log_action(user.tenant_id, user.user_id, "LINK", "google_sso", user.user_id)


def _flush_new_account(uow: IdentityAccess) -> None:
    """Write a new sign-up account; another live account with the address (one per email across
    workspaces, F017; a concurrent sign-up) is a 409, not a 500."""
    try:
        uow.flush_account()
    except DuplicateAccount as exc:
        raise ConflictError(EMAIL_REGISTERED) from exc


def _require_self_signup() -> None:
    if not settings.self_signup_enabled:
        raise PermissionDenied(SIGNUP_DISABLED)


class AuthService:
    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> AuthService:
        return cls(identity_uow(db))

    # ── Sign-up and sign-in ──

    def register(self, first_name: str, last_name: str, email: str, password: str, tenant_name: str,
                 throttle: Throttle) -> dict:
        """The first SUPER_ADMIN of a new workspace (self sign-up)."""
        _require_self_signup()
        throttle.check()
        with self.uow as uow:
            if uow.users.any_with_email(email):
                raise ConflictError(EMAIL_REGISTERED)
            if password_too_short(password):
                raise BadRequestError("Password must be at least 8 characters")
            tenant = uow.tenants.add(tenant_name=tenant_name)
            uow.flush()
            user = uow.users.add(
                tenant_id=tenant.tenant_id, first_name=first_name, last_name=last_name, email=email,
                password_hash=hash_password(password), role=SUPER_ADMIN, auth_provider="local", email_verified=True,
            )
            _flush_new_account(uow)
            logger.info(f"Registered new SUPER_ADMIN: {user.email} in tenant {tenant.tenant_id}")
            return complete_sign_in(uow, user)

    def login(self, email: str, password: str, throttle: Throttle) -> dict:
        """Email + password sign-in, with lockout per (email, client) pair."""
        throttle.check()
        with self.uow as uow:
            user = login_account(uow.users, email)
            if not user or not user.password_hash or not verify_password(password, user.password_hash):
                throttle.failed()
                raise AuthenticationFailed("Invalid email or password")
            throttle.succeeded()
            with identity_rules():
                check_password_sign_in(user.status)
            ensure_tenant_active(uow, user)
            return complete_sign_in(uow, user)

    def magic_login(self, token: str) -> dict:
        """Exchange a one-time magic login (set-password) link for a session; an invited account joins."""
        with self.uow as uow:
            link = uow.tokens.unused_magic_link(hash_token(token))
            if not link:
                raise AuthenticationFailed("Invalid or expired magic login token")
            if link.expires_at < utcnow():
                link.used_at = utcnow()
                uow.commit()
                raise AuthenticationFailed("Magic login token has expired")
            user = uow.users.get(link.user_id)
            if not user or user.status not in SIGN_IN_STATUSES:
                raise AuthenticationFailed(INACTIVE_USER)
            ensure_tenant_active(uow, user)
            # Activate invited user on first login via magic link
            first_login = user.status == UserStatus.INVITED
            if first_login:
                user.status = UserStatus.ACTIVE
            link.used_at = utcnow()
            return complete_sign_in(uow, user, first_login=first_login)

    def google_sign_in(self, credential: str) -> dict:
        """Sign in (or, with self sign-up, register) with a Google ID token."""
        if not settings.GOOGLE_CLIENT_ID:
            raise NotConfigured(GOOGLE_NOT_CONFIGURED)
        identity = verify_credential(credential, settings.GOOGLE_CLIENT_ID)
        with self.uow as uow:
            user = uow.users.by_google_id(identity.google_id) or login_account(uow.users, identity.email)
            if user:
                self._google_existing_account(uow, user, identity)
            else:
                user = self._google_new_account(uow, identity)
            return complete_sign_in(uow, user)

    @staticmethod
    def _google_existing_account(uow: IdentityUnitOfWork, user: User, identity: GoogleIdentity) -> None:
        if user.status not in SIGN_IN_STATUSES:
            raise PermissionDenied(SUSPENDED_ACCOUNT)
        ensure_tenant_active(uow, user)
        if user.google_id != identity.google_id:
            link_google_account(uow.session, user, identity.google_id, identity.email)
        if identity.avatar and not user.avatar_url:
            user.avatar_url = identity.avatar
        user.email_verified = True

    @staticmethod
    def _google_new_account(uow: IdentityUnitOfWork, identity: GoogleIdentity) -> User:
        """New user: a new workspace with them as its SUPER_ADMIN (tenant creator)."""
        _require_self_signup()
        tenant = uow.tenants.add(tenant_name=f"{identity.first_name}'s Workspace")
        uow.flush()
        user = uow.users.add(
            tenant_id=tenant.tenant_id, first_name=identity.first_name, last_name=identity.last_name,
            email=identity.email, google_id=identity.google_id, auth_provider="google", email_verified=True,
            avatar_url=identity.avatar, role=SUPER_ADMIN,
        )
        _flush_new_account(uow)
        logger.info(f"Google OAuth: created SUPER_ADMIN {identity.email} in new tenant")
        return user

    # ── Session ──

    def refresh(self, raw_token: Optional[str], activity_header: Optional[str]) -> dict:
        """Rotate a refresh token within its session (limits: app/services/session_limits.py). Raises
        session_limits.SessionRefused when the session is over or the token was replayed."""
        if not raw_token:
            raise AuthenticationFailed(INVALID_REFRESH)
        with self.uow as uow:
            presented, lineage = session_limits.begin_refresh(uow, hash_token(raw_token), activity_header)
            user = uow.users.get(presented.user_id)
            if not user or user.status != UserStatus.ACTIVE:
                raise AuthenticationFailed(INACTIVE_USER)
            ensure_tenant_active(uow, user)
            # Revoke old refresh token (rotation); the new one stays in the same session
            session_limits.mark_rotated(presented)
            return issue_session(uow, user, lineage=lineage)

    def logout(self, raw_refresh: Optional[str], access_token: Optional[str]) -> None:
        """Revoke the refresh token and the access token (when given and still valid)."""
        with self.uow as uow:
            if raw_refresh:
                presented = uow.tokens.active_refresh(hash_token(raw_refresh))
                if presented:
                    presented.revoked_at = utcnow()
                    presented.revoked_reason = session_limits.REASON_LOGOUT
                    uow.commit()
            if not access_token:
                return
            try:
                payload = decode_access_token(access_token)
            except TokenError:
                return  # expired / invalid tokens are refused anyway
            if token_revocation.revoke_payload(uow.tokens, payload):
                uow.commit()

    # ── Passwords ──

    def forgot_password(self, email: str, request: Optional[HeaderSource]) -> dict:
        """Email a reset link to an active account. Always the same answer (no account enumeration)."""
        with self.uow as uow:
            user = login_account(uow.users, email)
            if not user or user.status != UserStatus.ACTIVE:
                return dict(FORGOT_PASSWORD_REPLY)
            raw_token = secrets.token_urlsafe(48)
            uow.tokens.add_password_reset(user.user_id, hash_token(raw_token),
                                          utcnow() + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES))
            uow.commit()
            password_reset_email.deliver_reset_link(user.email, raw_token, request)
        return dict(FORGOT_PASSWORD_REPLY)

    def reset_password(self, token: str, new_password: str) -> dict:
        """Set a new password with a single-use reset link; every session of the account ends."""
        if password_too_short(new_password):
            raise BadRequestError(NEW_PASSWORD_TOO_SHORT)
        with self.uow as uow:
            record = uow.tokens.unused_password_reset(hash_token(token))
            if not record:
                raise BadRequestError("Invalid or expired reset token")
            if record.expires_at < utcnow():
                record.used_at = utcnow()
                uow.commit()
                raise BadRequestError("Reset token has expired")
            user = uow.users.get(record.user_id)
            if not user or user.status != UserStatus.ACTIVE:
                raise NotFoundError(INACTIVE_USER)
            user.password_hash = hash_password(new_password)
            if not user.auth_provider:
                user.auth_provider = "local"
            record.used_at = utcnow()
            uow.tokens.revoke_user_sessions(user.user_id, utcnow())
            uow.commit()
            logger.info(f"Password reset completed for {user.email}")
            return {"message": "Password reset successful"}

    def change_password(self, user: User, current_password: str, new_password: str,
                        issue_new_session: bool) -> PasswordChange:
        """Change the password; every refresh token is revoked (other devices sign in again)."""
        if not user.password_hash:
            raise BadRequestError("Account uses Google sign-in, no password set")
        if not verify_password(current_password, user.password_hash):
            raise AuthenticationFailed("Current password is incorrect")
        if password_too_short(new_password):
            raise BadRequestError(NEW_PASSWORD_TOO_SHORT)
        return self._replace_password(user, new_password, "Password changed successfully", issue_new_session)

    def set_initial_password(self, user: User, new_password: str, issue_new_session: bool) -> PasswordChange:
        """First password of an invited local account (I-13): no current password is asked."""
        if user.password_hash or (user.auth_provider or "local") != "local":
            raise ConflictError("A password is already set. Use change password instead.")
        if password_too_short(new_password):
            raise BadRequestError(NEW_PASSWORD_TOO_SHORT)
        change = self._replace_password(user, new_password, "Password set successfully", issue_new_session)
        logger.info("Initial password set for user %s", user.user_id)
        return change

    def _replace_password(self, user: User, new_password: str, message: str, issue_new_session: bool) -> PasswordChange:
        with self.uow as uow:
            user.password_hash = hash_password(new_password)
            # Other sessions end; a browser on cookies keeps a fresh one (its refresh cookie was just revoked)
            uow.tokens.revoke_user_sessions(user.user_id, utcnow())
            uow.commit()
            session = issue_session(uow, user) if issue_new_session else None
        return PasswordChange({"message": message}, session)

    # ── Profile ──

    @staticmethod
    def profile(user: User) -> dict:
        return user_profile(user)

    @staticmethod
    def session_state(user: User) -> dict:
        """Is this browser signed in: the user and their two-factor state."""
        profile = user_profile(user)
        return {
            "authenticated": True,
            "user": profile,
            "mfa_enabled": profile["mfa_enabled"],
            "mfa_required": profile["mfa_required"],
            "mfa_setup_required": profile["mfa_setup_required"],
            "recovery_codes_remaining": totp.remaining_recovery_codes(user.mfa_recovery_codes)
            if user.mfa_enabled else 0,
        }

    def update_profile(self, user: User, first_name: Optional[str], last_name: Optional[str],
                       avatar_url: Optional[str]) -> dict:
        with self.uow as uow:
            if first_name is not None:
                user.first_name = first_name
            if last_name is not None:
                user.last_name = last_name
            if avatar_url is not None:
                user.avatar_url = avatar_url
            uow.commit()
            return user_profile(user)
