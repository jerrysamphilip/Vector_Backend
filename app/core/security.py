# app/core/security.py
"""
Password hashing and JWT token management.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings


# ── Password Hashing ──────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a plain-text password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a plain-text password against a bcrypt hash."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


# ── JWT Access Tokens ─────────────────────────────────────

def create_access_token(
    user_id: str,
    tenant_id: str,
    role: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a short-lived JWT access token."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.
    Raises JWTError on invalid/expired tokens.
    """
    payload = jwt.decode(
        token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
    )
    if payload.get("type") != "access":
        raise JWTError("Invalid token type")
    return payload


# ── Two-factor pending sign-in token ───────────────────

MFA_TOKEN_MINUTES = 5


def create_mfa_token(user_id: str, first_login: bool = False) -> str:
    """Short-lived token proving the password (or magic link / Google) step passed. Grants no API
    access (decode_access_token refuses its type); only POST /api/auth/mfa/verify accepts it."""
    payload = {
        "sub": user_id,
        "type": "mfa",
        "jti": secrets.token_urlsafe(16),
        "fl": bool(first_login),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=MFA_TOKEN_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_mfa_token(token: str) -> dict:
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != "mfa" or not payload.get("sub") or not payload.get("jti"):
        raise JWTError("Invalid token type")
    return payload


# ── Refresh Tokens ────────────────────────────────────────

def generate_refresh_token() -> str:
    """Generate a cryptographically secure random refresh token."""
    return secrets.token_urlsafe(64)


def hash_token(token: str) -> str:
    """SHA-256 hash a refresh/invitation token for storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Tracking-link signatures ──────────────────────────────

def sign_tracking_url(message_id: str, url: str) -> str:
    """HMAC (JWT secret) over message id + destination, so /tracking/click can't be used as an open redirect."""
    import hmac
    digest = hmac.new(
        settings.JWT_SECRET_KEY.encode("utf-8"),
        f"click|{message_id}|{url}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def verify_tracking_url(message_id: str, url: str, signature: Optional[str]) -> bool:
    import hmac
    if not signature:
        return False
    return hmac.compare_digest(sign_tracking_url(message_id, url), signature)
