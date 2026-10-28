"""
Google sign-in (POST /api/auth/google/callback): verifies the Google ID token the front end received and
returns the identity it proves. The google-auth package is optional: without it Google sign-in answers
501 like an unconfigured deployment.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from app.core.config import settings
from app.services.identity_common import AuthenticationFailed, NotConfigured

try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    GOOGLE_AUTH_INSTALLED = True
except ImportError:  # pragma: no cover - google-auth is in requirements.txt
    GOOGLE_AUTH_INSTALLED = False

__all__ = ["GOOGLE_NOT_CONFIGURED", "GoogleIdentity", "consent_url", "verify_credential"]

logger = logging.getLogger(__name__)

GOOGLE_NOT_CONFIGURED = "Google OAuth not configured"


@dataclass(frozen=True)
class GoogleIdentity:
    google_id: str
    email: str
    first_name: str
    last_name: str
    avatar: str


def consent_url() -> dict:
    """The Google OAuth consent URL for redirect-based sign-in, with a fresh state value."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_REDIRECT_URI:
        raise NotConfigured(GOOGLE_NOT_CONFIGURED)
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


def verify_credential(credential: str, client_id: str) -> GoogleIdentity:
    """The identity a Google ID token proves; 401 when the token is invalid or the email unverified."""
    if not GOOGLE_AUTH_INSTALLED:
        logger.error("Google sign-in requested but the google-auth package is not installed")
        raise NotConfigured(GOOGLE_NOT_CONFIGURED)
    try:
        idinfo = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except ValueError:
        raise AuthenticationFailed("Invalid Google token")
    identity = GoogleIdentity(
        google_id=idinfo["sub"],
        email=idinfo.get("email", ""),
        first_name=idinfo.get("given_name", ""),
        last_name=idinfo.get("family_name", ""),
        avatar=idinfo.get("picture", ""),
    )
    if not identity.email or idinfo.get("email_verified") not in (True, "true"):
        raise AuthenticationFailed("Google account email is not verified")
    return identity
