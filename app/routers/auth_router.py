# app/routers/auth_router.py
"""
Authentication endpoints: register, login, Google OAuth, token refresh, profile, two-factor (TOTP).

Browser clients send "X-Auth-Mode: cookie" and get httpOnly cookies instead of tokens in the JSON
body (app/core/auth_cookies.py); other clients keep getting tokens in the body.

When a user has two-factor on, password / magic link / Google sign-in returns
{mfa_required: true, mfa_token} instead of tokens; POST /api/auth/mfa/verify with a code then
issues the session.

Layered (ADR 0009, chunk A5): handlers read the request (body, cookies, headers, client rate limits) and
make one call to AuthService (app/services/auth_service.py) or MfaService (app/services/mfa_service.py),
injected through the providers below so tests swap them with ``app.dependency_overrides``. Issued tokens
go into cookies here (``_deliver``); the rules and SQL live in the services and repositories.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from app.core import rate_limit
from app.core.auth import get_current_user, require_role
from app.core.auth_cookies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookies,
    require_csrf,
    set_auth_cookies,
    wants_cookies,
)
from app.core.database import get_db
from app.schemas.auth_schema import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    GoogleCallbackRequest,
    LoginRequest,
    MagicLoginRequest,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaTenantPolicyRequest,
    MfaVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SetPasswordRequest,
    TokenResponse,
    UpdateProfileRequest,
)
from app.services import google_identity, session_limits
from app.services.auth_service import AuthService, PasswordChange
from app.services.mfa_service import MfaService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


# ── Providers: one unit of work on the request's session (get_db) ──

def auth_service(db=Depends(get_db)) -> AuthService:
    return AuthService.for_session(db)


def mfa_service(db=Depends(get_db)) -> MfaService:
    return MfaService.for_session(db)


Auth = Depends(auth_service)
Mfa = Depends(mfa_service)


# ── Request adapters ──────────────────────────────────────

class _LoginThrottle:
    """Sign-in lockout per client IP and (email, IP) pair (app/core/rate_limit.py)."""

    def __init__(self, request: Request, email: str) -> None:
        self.request = request
        self.email = email

    def check(self) -> None:
        rate_limit.check_login_allowed(self.request, self.email)

    def failed(self) -> None:
        rate_limit.record_login_failure(self.request, self.email)

    def succeeded(self) -> None:
        rate_limit.record_login_success(self.request, self.email)


class _RegisterThrottle:
    """Sign-ups per client IP."""

    def __init__(self, request: Request) -> None:
        self.request = request

    def check(self) -> None:
        rate_limit.check(self.request, "register", rate_limit.REGISTER_PER_IP)

    def failed(self) -> None:
        return None

    def succeeded(self) -> None:
        return None


def _deliver(result: dict, request: Request, response: Response) -> dict:
    """Cookie mode: move issued tokens into httpOnly cookies and leave them out of the body."""
    if "access_token" in result and wants_cookies(request):
        set_auth_cookies(response, result["access_token"], result["refresh_token"])
        result = {**result, "access_token": None, "refresh_token": None, "token_type": None}
    return result


def _refresh_token_from(request: Request, body: RefreshRequest | None, csrf: bool) -> str | None:
    """The refresh token from the JSON body, else from the httpOnly cookie (CSRF-checked when csrf)."""
    if body is not None and body.refresh_token:
        return body.refresh_token
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw and csrf:
        require_csrf(request)
    return raw


def _presented_access_token(request: Request) -> Optional[str]:
    """The request's access token: Authorization: Bearer, else the access cookie."""
    auth_header = request.headers.get("authorization") or ""
    return auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else request.cookies.get(ACCESS_COOKIE)


def _password_changed(change: PasswordChange, request: Request, response: Response) -> dict:
    if change.session is not None:
        _deliver(change.session, request, response)
    return change.body


# ── Endpoints ─────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, response_model_exclude_none=True,
             status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, response: Response, svc: AuthService = Auth):
    """
    Register the first SUPER_ADMIN user and create a new tenant workspace.
    Subsequent users should be invited via /api/users/invite.
    """
    result = svc.register(body.first_name, body.last_name, body.email, body.password, body.tenant_name,
                          _RegisterThrottle(request))
    return _deliver(result, request, response)


@router.post("/login", response_model=TokenResponse, response_model_exclude_none=True)
def login(body: LoginRequest, request: Request, response: Response, svc: AuthService = Auth):
    """Authenticate with email + password and receive JWT tokens."""
    result = svc.login(body.email, body.password, _LoginThrottle(request, body.email))
    return _deliver(result, request, response)


@router.post("/refresh", response_model=TokenResponse, response_model_exclude_none=True)
def refresh_token(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    svc: AuthService = Auth,
):
    """Exchange a valid refresh token (body or cookie) for a new access + refresh token pair.

    Session limits (ADR 0007, app/services/session_limits.py): refused with 401 and the session
    cookies cleared once the session passed SESSION_ABSOLUTE_HOURS since sign-in or
    SESSION_IDLE_MINUTES without user activity, and when an already-rotated token is replayed (which
    also revokes the whole session). The SPA reports user activity in the X-User-Activity header
    (seconds since the last interaction); a refresh without it does not count as activity."""
    raw = _refresh_token_from(request, body, csrf=True)
    try:
        result = svc.refresh(raw, request.headers.get(session_limits.ACTIVITY_HEADER))
    except session_limits.SessionRefused as refused:
        refusal = JSONResponse(status_code=401, content={"detail": refused.detail})
        if refused.clear_cookies:
            clear_auth_cookies(refusal)
        return refusal
    return _deliver(result, request, response)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    body: RefreshRequest | None = None,
    svc: AuthService = Auth,
) -> Response:
    """Revoke the refresh token (body or cookie) and the access token (bearer or cookie), and clear
    the session cookies.

    No access token is needed: holding the refresh token is the proof, and an expired session must
    still be able to sign out. A still-valid access token sent with the request is revoked too, so a
    copy of it stops working at once instead of at its expiry."""
    svc.logout(_refresh_token_from(request, body, csrf=False), _presented_access_token(request))
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_auth_cookies(result)
    return result


# ── Password reset and magic link ─────────────────────────

@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, request: Request, svc: AuthService = Auth):
    """
    Request a password reset link.
    Always returns a generic success message to prevent account enumeration.
    """
    rate_limit.check(request, "reset", rate_limit.EMAIL_REQUESTS_PER_IP, body.email, rate_limit.EMAIL_REQUESTS_PER_EMAIL)
    return svc.forgot_password(body.email, request)


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, svc: AuthService = Auth):
    """Reset password using a valid single-use reset token."""
    return svc.reset_password(body.token, body.new_password)


@router.post("/magic-login", response_model=TokenResponse, response_model_exclude_none=True)
def magic_login(body: MagicLoginRequest, request: Request, response: Response, svc: AuthService = Auth):
    """Exchange a one-time magic login token for normal auth tokens."""
    rate_limit.check(request, "magic", rate_limit.LOGIN_ATTEMPTS_PER_IP)
    return _deliver(svc.magic_login(body.token), request, response)


# ── Google OAuth ──────────────────────────────────────────

@router.get("/google")
def google_auth_url():
    """
    Build Google OAuth consent URL for redirect-based sign-in.
    (One Tap flow can still call /google/callback directly.)
    """
    return google_identity.consent_url()


@router.post("/google/callback", response_model=TokenResponse, response_model_exclude_none=True)
def google_callback(body: GoogleCallbackRequest, request: Request, response: Response,
                    svc: AuthService = Auth):
    """
    Verify Google ID token from the frontend, then login or register the user.
    The frontend uses Google Sign-In and sends the credential (ID token) here.
    """
    return _deliver(svc.google_sign_in(body.credential), request, response)


# ── Profile ───────────────────────────────────────────────

@router.get("/me")
def get_me(current_user=Depends(get_current_user)):
    """Return the current authenticated user's profile (includes mfa_enabled / mfa_setup_required)."""
    return AuthService.profile(current_user)


@router.get("/session")
def get_session(current_user=Depends(get_current_user)):
    """Is this browser signed in? 200 with the user (and two-factor state) or 401."""
    return AuthService.session_state(current_user)


@router.put("/me")
def update_me(
    body: UpdateProfileRequest,
    current_user=Depends(get_current_user),
    svc: AuthService = Auth,
):
    """Update the current user's profile (name, avatar)."""
    return svc.update_profile(current_user, body.first_name, body.last_name, body.avatar_url)


@router.put("/me/password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user=Depends(get_current_user),
    svc: AuthService = Auth,
):
    """Change the current user's password. Revokes all refresh tokens."""
    change = svc.change_password(current_user, body.current_password, body.new_password, wants_cookies(request))
    return _password_changed(change, request, response)


@router.post("/me/set-password")
def set_initial_password(
    body: SetPasswordRequest,
    request: Request,
    response: Response,
    current_user=Depends(get_current_user),
    svc: AuthService = Auth,
):
    """
    First password for an invited account (I-13). Invitees never receive a password: they sign
    in once with the emailed link (/magic-login) and choose one here, so no current password is
    asked. Only for a local account that has no password yet; everyone else uses PUT /me/password.
    """
    change = svc.set_initial_password(current_user, body.new_password, wants_cookies(request))
    return _password_changed(change, request, response)


# ── Two-factor authentication (TOTP) ──────────────────────

@router.post("/mfa/verify", response_model=TokenResponse, response_model_exclude_none=True)
def mfa_verify(body: MfaVerifyRequest, request: Request, response: Response, svc: MfaService = Mfa):
    """Second sign-in step: exchange the mfa_token from /login (or magic link / Google) plus a
    6-digit code or a recovery code for a session. Public; the mfa_token is the credential."""
    rate_limit.check(request, "mfa", rate_limit.MFA_ATTEMPTS_PER_IP)
    return _deliver(svc.verify(body.mfa_token, body.code, body.recovery_code), request, response)


@router.post("/mfa/setup")
def mfa_setup(current_user=Depends(get_current_user), svc: MfaService = Mfa):
    """Start enrolment: a new secret (not active until /mfa/enable confirms a code from it)."""
    return svc.setup(current_user)


@router.post("/mfa/enable")
def mfa_enable(body: MfaCodeRequest, current_user=Depends(get_current_user), svc: MfaService = Mfa):
    """Confirm the authenticator with a code and turn two-factor on. Returns the recovery codes
    (shown once; only their hashes are kept)."""
    return svc.enable(current_user, body.code)


@router.post("/mfa/disable")
def mfa_disable(body: MfaDisableRequest, current_user=Depends(get_current_user), svc: MfaService = Mfa):
    """Turn two-factor off: needs the password (accounts that have one) and a code or recovery code.
    Not allowed while the workspace requires two-factor."""
    return svc.disable(current_user, body.password, body.code, body.recovery_code)


@router.post("/mfa/recovery-codes")
def mfa_regenerate_recovery_codes(body: MfaCodeRequest, current_user=Depends(get_current_user),
                                  svc: MfaService = Mfa):
    """Replace all recovery codes (needs a current authenticator code). Returns the new codes once."""
    return svc.regenerate_recovery_codes(current_user, body.code)


@router.put("/mfa/tenant-policy")
def mfa_tenant_policy(body: MfaTenantPolicyRequest, current_user=Depends(require_role("SUPER_ADMIN")),
                      svc: MfaService = Mfa):
    """SUPER_ADMIN: require two-factor for everyone in this workspace (or stop requiring it).
    Users without it are sent to set it up before they can use anything else."""
    return svc.set_tenant_policy(current_user, body.require_mfa)
