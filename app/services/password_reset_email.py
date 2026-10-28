"""
The password reset email (POST /api/auth/forgot-password): the reset link, on the front end the request
came from when that is a known origin (F024), and its delivery through Amazon SES.

``deliver_reset_link`` never raises: the endpoint answers the same generic message whether or not the
email went out (no account enumeration).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import quote_plus

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.services.invitation_email import HeaderSource
from app.services.ses_client import get_ses_client
from app.utils import safe_origin

__all__ = ["build_reset_link", "deliver_reset_link", "send_password_reset_email"]

logger = logging.getLogger(__name__)


def _get_ses_client() -> Any:
    """The shared SES client (one per region/credentials, not one per reset email, T-5)."""
    return get_ses_client()


def build_reset_link(token: str, request: Optional[HeaderSource] = None) -> str:
    """The reset link, on the front end the request came from when that is a known (or, locally, a
    localhost) origin rebuilt from validated parts (F024), else on the configured front end."""
    request_origin = safe_origin.request_origin(request.headers) if request is not None else ""
    reset_url = (settings.RESET_PASSWORD_URL or "").strip()
    backend_base = settings.BASE_URL.rstrip("/")
    frontend_base = (settings.FRONTEND_URL or "").split(",")[0].strip().rstrip("/")
    profile = (os.getenv("APP_PROFILE", "") or "").strip().lower()
    resolved_base = safe_origin.resolve_base_url(backend_base, frontend_base, profile)
    base = request_origin and f"{request_origin}/reset-password" or reset_url or f"{resolved_base}/reset-password"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={quote_plus(token)}"


def send_password_reset_email(to_email: str, reset_link: str) -> None:
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


def deliver_reset_link(to_email: str, raw_token: str, request: Optional[HeaderSource]) -> None:
    """Email the reset link; a failure is logged, never raised."""
    try:
        reset_link = build_reset_link(raw_token, request)
        send_password_reset_email(to_email, reset_link)
        logger.info(f"Password reset email sent to {to_email}")
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        logger.error(f"Password reset email failed for {to_email}: {exc}")
    except Exception as exc:  # noqa: BLE001 - delivery boundary: the reset token is stored, report and carry on
        logger.exception(f"Unexpected password reset error for {to_email}: {exc}")
