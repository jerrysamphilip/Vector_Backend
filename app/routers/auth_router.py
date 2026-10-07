# app/routers/auth_router.py
"""
Authentication endpoints: register, login, Google OAuth, token refresh, profile.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlencode, urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core import rate_limit
from app.core.security import (
    create_access_token,
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
    refresh_token: str


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
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict
    first_login: bool = False


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

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """
    Register the first SUPER_ADMIN user and create a new tenant workspace.
    Subsequent users should be invited via /api/users/invite.
    """
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
    return _issue_tokens(user, db)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Authenticate with email + password and receive JWT tokens."""
    rate_limit.check_login_allowed(request, body.email)
    user = db.query(User).filter(User.email == body.email).first()

    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        rate_limit.record_login_failure(body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    rate_limit.record_login_success(body.email)

    if user.status not in ("ACTIVE", "INVITED"):
        raise HTTPException(status_code=403, detail="Account is suspended or inactive")

    # Activate invited user on first login
    is_first_login = user.status == "INVITED"
    if is_first_login:
        user.status = "ACTIVE"

    return _issue_tokens(user, db, first_login=is_first_login)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(body: RefreshRequest, db: Session = Depends(get_db)):
    """Exchange a valid refresh token for a new access + refresh token pair."""
    token_hash = hash_token(body.refresh_token)
    rt = db.query(RefreshToken).filter(
        RefreshToken.token_hash == token_hash,
        RefreshToken.revoked_at.is_(None),
    ).first()

    if not rt or rt.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = db.query(User).filter(User.user_id == rt.user_id).first()
    if not user or user.status != "ACTIVE":
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Revoke old refresh token (rotation)
    rt.revoked_at = datetime.utcnow()

    return _issue_tokens(user, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    body: RefreshRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke the refresh token (logout)."""
    token_hash = hash_token(body.refresh_token)
    rt = db.query(RefreshToken).filter(
        RefreshToken.token_hash == token_hash,
        RefreshToken.user_id == current_user.user_id,
    ).first()
    if rt:
        rt.revoked_at = datetime.utcnow()
        db.commit()


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


@router.post("/magic-login", response_model=TokenResponse)
def magic_login(body: MagicLoginRequest, request: Request, db: Session = Depends(get_db)):
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

    # Activate invited user on first login via magic link
    is_first_login = user.status == "INVITED"
    if is_first_login:
        user.status = "ACTIVE"

    magic_record.used_at = datetime.utcnow()
    return _issue_tokens(user, db, first_login=is_first_login)


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

@router.post("/google/callback", response_model=TokenResponse)
def google_callback(body: GoogleCallbackRequest, db: Session = Depends(get_db)):
    """
    Verify Google ID token from the frontend, then login or register the user.
    The frontend uses Google Sign-In and sends the credential (ID token) here.
    """
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests

        if not settings.GOOGLE_CLIENT_ID:
            raise HTTPException(status_code=501, detail="Google OAuth not configured")

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

    # Check if user exists by google_id or email
    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()

    if user:
        # Existing user — link Google if not already
        if not user.google_id:
            user.google_id = google_id
            user.auth_provider = "google"
        if avatar and not user.avatar_url:
            user.avatar_url = avatar
        user.email_verified = True
    else:
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

    return _issue_tokens(user, db)


# ── Profile ───────────────────────────────────────────────

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Return the current authenticated user's profile."""
    return _user_dict(current_user)


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
    return {"message": "Password changed successfully"}
