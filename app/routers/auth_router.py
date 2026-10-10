# app/routers/auth_router.py
"""
Authentication endpoints: register, login, Google OAuth, token refresh, profile, two-factor (TOTP).

Browser clients send "X-Auth-Mode: cookie" and get httpOnly cookies instead of tokens in the JSON
body (app/core/auth_cookies.py); other clients keep getting tokens in the body.

When a user has two-factor on, password / magic link / Google sign-in returns
{mfa_required: true, mfa_token} instead of tokens; POST /api/auth/mfa/verify with a code then
issues the session.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlencode, urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jose import JWTError
from pydantic import BaseModel, EmailStr
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import rate_limit, totp
from app.core.auth import ensure_tenant_active, get_current_user, require_role
from app.core.auth_cookies import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    require_csrf,
    set_auth_cookies,
    wants_cookies,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    create_mfa_token,
    decode_mfa_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.magic_login_token import MagicLoginToken
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


# ── Request / Response Schemas ────────────────────────────

class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    password: str
    tenant_name: str = "My Workspace"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    # Optional: browser clients send it in the httpOnly refresh_token cookie instead
    refresh_token: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class MagicLoginRequest(BaseModel):
    token: str


class UpdateProfileRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    avatar_url: str | None = None


class GoogleCallbackRequest(BaseModel):
    credential: str  # Google ID token from frontend


class TokenResponse(BaseModel):
    # Tokens are omitted in cookie mode (X-Auth-Mode: cookie) and when a second factor is required
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    user: dict | None = None
    first_login: bool = False
    mfa_required: bool = False
    mfa_token: str | None = None


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str | None = None
    recovery_code: str | None = None


class MfaCodeRequest(BaseModel):
    code: str


class MfaDisableRequest(BaseModel):
    password: str | None = None
    code: str | None = None
    recovery_code: str | None = None


class MfaTenantPolicyRequest(BaseModel):
    require_mfa: bool


# ── Helpers ───────────────────────────────────────────────

_DEFAULT_PERMS: dict[str, list[str]] = {
    "PLATFORM_ADMIN": ["manage_platform"],
    "SUPER_ADMIN":    ["manage_campaigns", "manage_templates", "manage_inboxes",
                       "manage_prospects", "view_analytics", "export_data", "manage_team"],
    "ADMIN":          ["manage_campaigns", "manage_templates", "manage_inboxes",
                       "manage_prospects", "view_analytics", "export_data", "manage_team"],
    "MANAGER":        ["manage_campaigns", "manage_templates", "view_analytics"],
}


def _user_dict(user: User) -> dict:
    tenant = getattr(user, "tenant", None) if user.tenant_id and user.role != "PLATFORM_ADMIN" else None
    effective_permissions = (
        user.custom_permissions
        if user.custom_permissions is not None
        else _DEFAULT_PERMS.get(user.role, [])
    )
    return {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "tenant_name": user.tenant.tenant_name if getattr(user, "tenant", None) else None,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "auth_provider": user.auth_provider or "local",
        "email_verified": user.email_verified or False,
        "avatar_url": user.avatar_url,
        "permissions": effective_permissions,
        "sales_level": user.sales_level,
        "manager_id": user.manager_id,
        "mfa_enabled": bool(user.mfa_enabled),
        "mfa_required": bool(tenant.require_mfa) if tenant is not None else False,
        "mfa_setup_required": bool(tenant is not None and tenant.require_mfa and not user.mfa_enabled),
    }


def _issue_tokens(user: User, db: Session, device_info: str = None, first_login: bool = False) -> dict:
    """Create access + refresh tokens and persist the refresh token."""
    access_token = create_access_token(user.user_id, user.tenant_id, user.role)
    raw_refresh = generate_refresh_token()

    rt = RefreshToken(
        user_id=user.user_id,
        token_hash=hash_token(raw_refresh),
        device_info=device_info,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(rt)

    user.last_login_at = datetime.utcnow()
    db.commit()

    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "user": _user_dict(user),
        "first_login": first_login,
    }


def _deliver_tokens(result: dict, request: Request, response: Response) -> dict:
    """Cookie mode: move the tokens into httpOnly cookies and leave them out of the body."""
    if wants_cookies(request):
        set_auth_cookies(response, result["access_token"], result["refresh_token"])
        result = {**result, "access_token": None, "refresh_token": None, "token_type": None}
    return result


def _complete_sign_in(user: User, db: Session, request: Request, response: Response,
                      first_login: bool = False) -> dict:
    """First factor passed (password, magic link, Google, sign-up). Users with two-factor on get a
    short-lived mfa_token to exchange at /api/auth/mfa/verify; everyone else gets a session."""
    if user.mfa_enabled:
        db.commit()  # keep what the first step changed (e.g. magic link marked used)
        return {"mfa_required": True, "mfa_token": create_mfa_token(user.user_id, first_login),
                "first_login": first_login}
    return _deliver_tokens(_issue_tokens(user, db, first_login=first_login), request, response)


def _get_ses_client():
    client_kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", **client_kwargs)


def _build_reset_link(token: str) -> str:
    # Prefer request-origin mapping when available to avoid cross-env reset links.
    # (e.g., localhost -> localhost, sales -> sales, outreach -> outreach)
    # This prevents stage BASE_URL from leaking into dev reset emails.
    return _build_reset_link_for_origin(token, None)


def _build_reset_link_for_origin(token: str, request: Request | None) -> str:
    def _normalized_origin(raw_url: str | None) -> str:
        if not raw_url:
            return ""
        try:
            parsed = urlparse(raw_url)
            if not parsed.scheme or not parsed.netloc:
                return ""
            host = (parsed.hostname or "").lower()
            if host == "sales.neutrino-ai.com":
                return "https://sales.neutrino-ai.com"
            if host == "outreach360.neutrinoaistudio.com":
                return "https://outreach360.neutrinoaistudio.com"
            if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
                return f"{parsed.scheme}://{parsed.netloc}"
            return ""
        except Exception:
            return ""

    request_origin = ""
    if request is not None:
        request_origin = _normalized_origin(request.headers.get("origin"))
        if not request_origin:
            request_origin = _normalized_origin(request.headers.get("referer"))

    reset_url = (settings.RESET_PASSWORD_URL or "").strip()
    backend_base = settings.BASE_URL.rstrip("/")
    frontend_base = (settings.FRONTEND_URL or "").strip().rstrip("/")
    profile = (os.getenv("APP_PROFILE", "") or "").strip().lower()

    def _is_local(url: str) -> bool:
        if not url:
            return True
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in {"localhost", "127.0.0.1", "0.0.0.0"}

    profile_base = ""
    if _is_local(backend_base) and _is_local(frontend_base):
        if profile == "dev":
            profile_base = "https://sales.neutrino-ai.com"
        elif profile == "stage":
            profile_base = "https://outreach360.neutrinoaistudio.com"

    frontend_non_local = bool(frontend_base) and not _is_local(frontend_base)
    backend_non_local = bool(backend_base) and not _is_local(backend_base)
    resolved_base = (
        profile_base
        or (frontend_base if frontend_non_local else "")
        or (backend_base if backend_non_local else "")
        or frontend_base
        or backend_base
    )
    base = request_origin and f"{request_origin}/reset-password" or reset_url or f"{resolved_base}/reset-password"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={quote_plus(token)}"


def _send_password_reset_email(to_email: str, reset_link: str):
    sender_email = (settings.SENDER_EMAIL or "").strip()
    if not sender_email:
        raise RuntimeError("SENDER_EMAIL is not configured")

    sender_name = (settings.SENDER_NAME or "").strip() or "Sales Pro"
    support_email = sender_email

    subject = "Reset your password"
    body = "\n".join(
        [
            "Hi,",
            "",
            "We received a request to reset your password.",
            "",
            f"Reset Password: {reset_link}",
            "",
            f"This link expires in {settings.PASSWORD_RESET_EXPIRE_MINUTES} minutes.",
            "",
            "If you did not request this, you can ignore this email.",
            "",
            f"Need help? Contact {support_email}.",
        ]
    )

    ses_client = _get_ses_client()
    ses_client.send_email(
        Source=f"{sender_name} <{sender_email}>",
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )


# ── Endpoints ─────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, response_model_exclude_none=True,
             status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Register the first SUPER_ADMIN user and create a new tenant workspace.
    Subsequent users should be invited via /api/users/invite.
    """
    if not settings.self_signup_enabled:
        raise HTTPException(status_code=403, detail="Self sign-up is disabled. Ask your administrator for an invitation.")
    rate_limit.check(request, "register", rate_limit.REGISTER_PER_IP)
    # Check for existing user with same email
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Create tenant
    tenant = Tenant(tenant_name=body.tenant_name)
    db.add(tenant)
    db.flush()

    # Create SUPER_ADMIN user (tenant creator)
    user = User(
        tenant_id=tenant.tenant_id,
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        password_hash=hash_password(body.password),
        role="SUPER_ADMIN",
        auth_provider="local",
        email_verified=True,
    )
    db.add(user)
    db.flush()

    logger.info(f"Registered new SUPER_ADMIN: {user.email} in tenant {tenant.tenant_id}")
    return _complete_sign_in(user, db, request, response)


@router.post("/login", response_model=TokenResponse, response_model_exclude_none=True)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Authenticate with email + password and receive JWT tokens."""
    rate_limit.check_login_allowed(request, body.email)
    user = db.query(User).filter(User.email == body.email).first()

    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        rate_limit.record_login_failure(body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    rate_limit.record_login_success(body.email)

    if user.status not in ("ACTIVE", "INVITED"):
        raise HTTPException(status_code=403, detail="Account is suspended or inactive")
    ensure_tenant_active(db, user)

    # Activate invited user on first login
    is_first_login = user.status == "INVITED"
    if is_first_login:
        user.status = "ACTIVE"

    return _complete_sign_in(user, db, request, response, first_login=is_first_login)


def _refresh_token_from(request: Request, body: RefreshRequest | None, csrf: bool) -> str | None:
    """The refresh token from the JSON body, else from the httpOnly cookie (CSRF-checked when csrf)."""
    if body is not None and body.refresh_token:
        return body.refresh_token
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw and csrf:
        require_csrf(request)
    return raw


@router.post("/refresh", response_model=TokenResponse, response_model_exclude_none=True)
def refresh_token(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    db: Session = Depends(get_db),
):
    """Exchange a valid refresh token (body or cookie) for a new access + refresh token pair."""
    raw = _refresh_token_from(request, body, csrf=True)
    if not raw:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    token_hash = hash_token(raw)
    rt = db.query(RefreshToken).filter(
        RefreshToken.token_hash == token_hash,
        RefreshToken.revoked_at.is_(None),
    ).first()

    if not rt or rt.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = db.query(User).filter(User.user_id == rt.user_id).first()
    if not user or user.status != "ACTIVE":
        raise HTTPException(status_code=401, detail="User not found or inactive")
    ensure_tenant_active(db, user)

    # Revoke old refresh token (rotation)
    rt.revoked_at = datetime.utcnow()

    return _deliver_tokens(_issue_tokens(user, db), request, response)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    body: RefreshRequest | None = None,
    db: Session = Depends(get_db),
):
    """Revoke the refresh token (body or cookie) and clear the session cookies.

    No access token is needed: holding the refresh token is the proof, and an expired session must
    still be able to sign out."""
    raw = _refresh_token_from(request, body, csrf=False)
    if raw:
        rt = db.query(RefreshToken).filter(
            RefreshToken.token_hash == hash_token(raw),
            RefreshToken.revoked_at.is_(None),
        ).first()
        if rt:
            rt.revoked_at = datetime.utcnow()
            db.commit()
    result = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_auth_cookies(result)
    return result


# ── Google OAuth ──────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """
    Request a password reset link.
    Always returns a generic success message to prevent account enumeration.
    """
    rate_limit.check(request, "reset", rate_limit.EMAIL_REQUESTS_PER_IP, body.email, rate_limit.EMAIL_REQUESTS_PER_EMAIL)
    generic_response = {
        "message": "If an account exists for that email, a password reset link has been sent."
    }

    user = db.query(User).filter(User.email == body.email).first()
    if not user or user.status != "ACTIVE":
        return generic_response

    raw_token = secrets.token_urlsafe(48)
    reset_record = PasswordResetToken(
        user_id=user.user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
    )
    db.add(reset_record)
    db.commit()

    try:
        reset_link = _build_reset_link_for_origin(raw_token, request)
        _send_password_reset_email(user.email, reset_link)
        logger.info(f"Password reset email sent to {user.email}")
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        logger.error(f"Password reset email failed for {user.email}: {exc}")
    except Exception as exc:
        logger.exception(f"Unexpected password reset error for {user.email}: {exc}")

    return generic_response


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a valid single-use reset token."""
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    token_hash = hash_token(body.token)
    reset_record = db.query(PasswordResetToken).filter(
        PasswordResetToken.token_hash == token_hash,
        PasswordResetToken.used_at.is_(None),
    ).first()

    if not reset_record:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    if reset_record.expires_at < datetime.utcnow():
        reset_record.used_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=400, detail="Reset token has expired")

    user = db.query(User).filter(User.user_id == reset_record.user_id).first()
    if not user or user.status != "ACTIVE":
        raise HTTPException(status_code=404, detail="User not found or inactive")

    user.password_hash = hash_password(body.new_password)
    if not user.auth_provider:
        user.auth_provider = "local"

    reset_record.used_at = datetime.utcnow()

    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.user_id,
        RefreshToken.revoked_at.is_(None),
    ).update({"revoked_at": datetime.utcnow()})

    db.commit()
    logger.info(f"Password reset completed for {user.email}")
    return {"message": "Password reset successful"}


@router.post("/magic-login", response_model=TokenResponse, response_model_exclude_none=True)
def magic_login(body: MagicLoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Exchange a one-time magic login token for normal auth tokens."""
    rate_limit.check(request, "magic", rate_limit.LOGIN_ATTEMPTS_PER_IP)
    token_hash = hash_token(body.token)
    magic_record = db.query(MagicLoginToken).filter(
        MagicLoginToken.token_hash == token_hash,
        MagicLoginToken.used_at.is_(None),
    ).first()

    if not magic_record:
        raise HTTPException(status_code=401, detail="Invalid or expired magic login token")

    if magic_record.expires_at < datetime.utcnow():
        magic_record.used_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=401, detail="Magic login token has expired")

    user = db.query(User).filter(User.user_id == magic_record.user_id).first()
    if not user or user.status not in ("ACTIVE", "INVITED"):
        raise HTTPException(status_code=401, detail="User not found or inactive")
    ensure_tenant_active(db, user)

    # Activate invited user on first login via magic link
    is_first_login = user.status == "INVITED"
    if is_first_login:
        user.status = "ACTIVE"

    magic_record.used_at = datetime.utcnow()
    return _complete_sign_in(user, db, request, response, first_login=is_first_login)


@router.get("/google")
def google_auth_url():
    """
    Build Google OAuth consent URL for redirect-based sign-in.
    (One Tap flow can still call /google/callback directly.)
    """
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_REDIRECT_URI:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    state = secrets.token_urlsafe(24)
    params = urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return {"auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}", "state": state}

@router.post("/google/callback", response_model=TokenResponse, response_model_exclude_none=True)
def google_callback(body: GoogleCallbackRequest, request: Request, response: Response,
                    db: Session = Depends(get_db)):
    """
    Verify Google ID token from the frontend, then login or register the user.
    The frontend uses Google Sign-In and sends the credential (ID token) here.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
    except ImportError:
        logger.error("Google sign-in requested but the google-auth package is not installed")
        raise HTTPException(status_code=501, detail="Google OAuth not configured")
    try:
        idinfo = id_token.verify_oauth2_token(
            body.credential,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    google_id = idinfo["sub"]
    email = idinfo.get("email", "")
    first_name = idinfo.get("given_name", "")
    last_name = idinfo.get("family_name", "")
    avatar = idinfo.get("picture", "")
    if not email or idinfo.get("email_verified") not in (True, "true"):
        raise HTTPException(status_code=401, detail="Google account email is not verified")

    # Check if user exists by google_id or email
    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()

    if user:
        if user.status not in ("ACTIVE", "INVITED"):
            raise HTTPException(status_code=403, detail="Account is suspended or inactive")
        ensure_tenant_active(db, user)
        # Existing user — link Google if not already
        if not user.google_id:
            user.google_id = google_id
            user.auth_provider = "google"
        if avatar and not user.avatar_url:
            user.avatar_url = avatar
        user.email_verified = True
    else:
        if not settings.self_signup_enabled:
            raise HTTPException(status_code=403, detail="Self sign-up is disabled. Ask your administrator for an invitation.")
        # New user — create tenant + SUPER_ADMIN (tenant creator)
        tenant = Tenant(tenant_name=f"{first_name}'s Workspace")
        db.add(tenant)
        db.flush()

        user = User(
            tenant_id=tenant.tenant_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            google_id=google_id,
            auth_provider="google",
            email_verified=True,
            avatar_url=avatar,
            role="SUPER_ADMIN",
        )
        db.add(user)
        db.flush()
        logger.info(f"Google OAuth: created SUPER_ADMIN {email} in new tenant")

    return _complete_sign_in(user, db, request, response)


# ── Profile ───────────────────────────────────────────────

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Return the current authenticated user's profile (includes mfa_enabled / mfa_setup_required)."""
    return _user_dict(current_user)


@router.get("/session")
def get_session(current_user: User = Depends(get_current_user)):
    """Is this browser signed in? 200 with the user (and two-factor state) or 401."""
    user = _user_dict(current_user)
    return {
        "authenticated": True,
        "user": user,
        "mfa_enabled": user["mfa_enabled"],
        "mfa_required": user["mfa_required"],
        "mfa_setup_required": user["mfa_setup_required"],
        "recovery_codes_remaining": totp.remaining_recovery_codes(current_user.mfa_recovery_codes)
        if current_user.mfa_enabled else 0,
    }


@router.put("/me")
def update_me(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's profile (name, avatar)."""
    if body.first_name is not None:
        current_user.first_name = body.first_name
    if body.last_name is not None:
        current_user.last_name = body.last_name
    if body.avatar_url is not None:
        current_user.avatar_url = body.avatar_url
    db.commit()
    return _user_dict(current_user)


@router.put("/me/password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the current user's password. Revokes all refresh tokens."""
    if not current_user.password_hash:
        raise HTTPException(status_code=400, detail="Account uses Google sign-in, no password set")

    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    current_user.password_hash = hash_password(body.new_password)

    # Revoke all refresh tokens (force re-login on other devices)
    db.query(RefreshToken).filter(
        RefreshToken.user_id == current_user.user_id,
        RefreshToken.revoked_at.is_(None),
    ).update({"revoked_at": datetime.utcnow()})

    db.commit()
    if wants_cookies(request):
        # The refresh cookie was just revoked with the rest; give this browser a fresh session
        _deliver_tokens(_issue_tokens(current_user, db), request, response)
    return {"message": "Password changed successfully"}


# ── Two-factor authentication (TOTP) ──────────────────────

def _mfa_audit(db: Session, user: User, action: str, entity_type: str = "mfa") -> None:
    """Audit-log a two-factor change (ENABLE_MFA, DISABLE_MFA, RECOVERY_CODE_USED_MFA, ...)."""
    logger.info("Two-factor %s %s for user %s", entity_type, action, user.user_id)
    if not user.tenant_id:
        return  # PLATFORM_ADMIN: no tenant to file the audit row under
    try:
        from app.services.audit_service import AuditService
        AuditService(db).log_action(user.tenant_id, user.user_id, action, entity_type, user.user_id)
    except Exception:
        logger.exception("Could not write the two-factor audit entry")


def _accept_totp(db: Session, user: User, code: str | None) -> bool:
    """Accept a current TOTP code once: records its time step atomically so it can't be replayed."""
    secret = user.mfa_secret
    step = totp.matching_step(secret, code, user.mfa_last_step)
    if step is None:
        return False
    updated = db.query(User).filter(
        User.user_id == user.user_id,
        or_(User.mfa_last_step.is_(None), User.mfa_last_step < step),
    ).update({User.mfa_last_step: step}, synchronize_session=False)
    if updated != 1:
        return False  # the same (or a later) code was used concurrently
    db.expire(user, ["mfa_last_step"])
    return True


def _accept_recovery_code(db: Session, user: User, code: str | None) -> bool:
    """Accept an unused recovery code and burn it (single use, also under concurrency)."""
    stored = user.mfa_recovery_codes
    remaining = totp.consume_recovery_code(stored, code or "")
    if remaining is None:
        return False
    updated = db.query(User).filter(
        User.user_id == user.user_id,
        User.mfa_recovery_codes == stored,
    ).update({User.mfa_recovery_codes: remaining}, synchronize_session=False)
    if updated != 1:
        return False
    db.expire(user, ["mfa_recovery_codes"])
    return True


def _check_second_factor(db: Session, user: User, code: str | None, recovery_code: str | None,
                         mfa_jti: str | None = None, allow_recovery: bool = True) -> str:
    """Verify a TOTP or recovery code with per-user (and per pending sign-in) lockout.
    Returns "totp" or "recovery"; raises 401 on a wrong code, 429 when locked."""
    rate_limit.check_mfa_allowed(user.user_id, mfa_jti)
    used = None
    if code and _accept_totp(db, user, code):
        used = "totp"
    elif allow_recovery and recovery_code and _accept_recovery_code(db, user, recovery_code):
        used = "recovery"
    if not used:
        db.rollback()
        rate_limit.record_mfa_failure(user.user_id, mfa_jti)
        raise HTTPException(status_code=401, detail="Invalid authentication code")
    rate_limit.record_mfa_success(user.user_id)
    if used == "recovery":
        _mfa_audit(db, user, "RECOVERY_CODE_USED")
    return used


@router.post("/mfa/verify", response_model=TokenResponse, response_model_exclude_none=True)
def mfa_verify(body: MfaVerifyRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Second sign-in step: exchange the mfa_token from /login (or magic link / Google) plus a
    6-digit code or a recovery code for a session. Public; the mfa_token is the credential."""
    rate_limit.check(request, "mfa", rate_limit.MFA_ATTEMPTS_PER_IP)
    try:
        payload = decode_mfa_token(body.mfa_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Your sign-in expired. Please sign in again.")
    jti = payload["jti"]
    if rate_limit.limiter.blocked(f"mfa-token-used:{jti}", 1, 600):
        raise HTTPException(status_code=401, detail="Your sign-in expired. Please sign in again.")
    if not (body.code or body.recovery_code):
        raise HTTPException(status_code=400, detail="Enter the code from your authenticator app or a recovery code")

    user = db.query(User).filter(User.user_id == payload["sub"]).first()
    if not user or user.status != "ACTIVE" or not user.mfa_enabled:
        raise HTTPException(status_code=401, detail="Your sign-in expired. Please sign in again.")
    ensure_tenant_active(db, user)

    _check_second_factor(db, user, body.code, body.recovery_code, mfa_jti=jti)
    rate_limit.limiter.hit(f"mfa-token-used:{jti}", 1, 600)  # one session per mfa_token
    return _deliver_tokens(_issue_tokens(user, db, first_login=bool(payload.get("fl"))), request, response)


@router.post("/mfa/setup")
def mfa_setup(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Start enrolment: a new secret (not active until /mfa/enable confirms a code from it)."""
    if current_user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already on. Turn it off first to use a new authenticator.")
    secret = totp.generate_secret()
    current_user.mfa_secret = secret
    current_user.mfa_last_step = None
    db.commit()
    return {
        "secret": secret,
        "otpauth_uri": totp.provisioning_uri(secret, current_user.email),
        "issuer": totp.ISSUER,
        "account": current_user.email,
    }


@router.post("/mfa/enable")
def mfa_enable(body: MfaCodeRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Confirm the authenticator with a code and turn two-factor on. Returns the recovery codes
    (shown once; only their hashes are kept)."""
    if current_user.mfa_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already on.")
    if not current_user.mfa_secret:
        raise HTTPException(status_code=400, detail="Start the setup first.")
    _check_second_factor(db, current_user, body.code, None, allow_recovery=False)
    codes, hashes = totp.generate_recovery_codes()
    current_user.mfa_enabled = True
    current_user.mfa_enabled_at = datetime.utcnow()
    current_user.mfa_recovery_codes = hashes
    _mfa_audit(db, current_user, "ENABLE")
    db.commit()
    return {"mfa_enabled": True, "recovery_codes": codes}


@router.post("/mfa/disable")
def mfa_disable(body: MfaDisableRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Turn two-factor off: needs the password (accounts that have one) and a code or recovery code.
    Not allowed while the workspace requires two-factor."""
    if not current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not on.")
    if current_user.tenant is not None and current_user.tenant.require_mfa:
        raise HTTPException(status_code=403, detail="Your workspace requires two-factor authentication, so it can't be turned off.")
    if current_user.password_hash:
        rate_limit.check_mfa_allowed(current_user.user_id)
        if not body.password or not verify_password(body.password, current_user.password_hash):
            rate_limit.record_mfa_failure(current_user.user_id)
            raise HTTPException(status_code=401, detail="Password is incorrect")
    if not (body.code or body.recovery_code):
        raise HTTPException(status_code=400, detail="Enter a code from your authenticator app or a recovery code")
    _check_second_factor(db, current_user, body.code, body.recovery_code)
    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    current_user.mfa_recovery_codes = None
    current_user.mfa_enabled_at = None
    current_user.mfa_last_step = None
    _mfa_audit(db, current_user, "DISABLE")
    db.commit()
    return {"mfa_enabled": False}


@router.post("/mfa/recovery-codes")
def mfa_regenerate_recovery_codes(body: MfaCodeRequest, current_user: User = Depends(get_current_user),
                                  db: Session = Depends(get_db)):
    """Replace all recovery codes (needs a current authenticator code). Returns the new codes once."""
    if not current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not on.")
    _check_second_factor(db, current_user, body.code, None, allow_recovery=False)
    codes, hashes = totp.generate_recovery_codes()
    current_user.mfa_recovery_codes = hashes
    _mfa_audit(db, current_user, "REGENERATE_RECOVERY_CODES")
    db.commit()
    return {"recovery_codes": codes}


@router.put("/mfa/tenant-policy")
def mfa_tenant_policy(body: MfaTenantPolicyRequest, current_user: User = Depends(require_role("SUPER_ADMIN")),
                      db: Session = Depends(get_db)):
    """SUPER_ADMIN: require two-factor for everyone in this workspace (or stop requiring it).
    Users without it are sent to set it up before they can use anything else."""
    tenant = db.query(Tenant).filter(Tenant.tenant_id == current_user.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if body.require_mfa and not current_user.mfa_enabled:
        raise HTTPException(status_code=400, detail="Turn on two-factor for your own account before requiring it for everyone.")
    if bool(tenant.require_mfa) != body.require_mfa:
        tenant.require_mfa = body.require_mfa
        _mfa_audit(db, current_user, "REQUIRE" if body.require_mfa else "UNREQUIRE", "tenant_mfa_policy")
        db.commit()
    return {"require_mfa": bool(tenant.require_mfa)}
