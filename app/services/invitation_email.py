# app/services/invitation_email.py
"""
The workspace invitation email: sign-in link, HTML and plain-text bodies, and delivery
through Amazon SES. ``send_invitation_email`` raises on failure; the caller decides the
fallback (app/services/invitation_service.py).
"""

import os
import re
from typing import Any, Dict
from urllib.parse import quote_plus, urlparse

import boto3
from fastapi import Request

from app.core.config import settings

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})
# Front-end origins a sign-in link may point back to (taken from the inviting request)
_KNOWN_ORIGINS = {
    "sales.neutrino-ai.com": "https://sales.neutrino-ai.com",
    "outreach360.neutrinoaistudio.com": "https://outreach360.neutrinoaistudio.com",
}
# Deployed front ends per APP_PROFILE, used when the configured URLs are all local
_PROFILE_BASES = {
    "dev": "https://sales.neutrino-ai.com",
    "stage": "https://outreach360.neutrinoaistudio.com",
}


def _get_ses_client() -> Any:
    """Build SES client using configured region/credentials."""
    client_kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", **client_kwargs)


def normalized_origin(raw_url: str | None) -> str:
    """A known front-end origin (or a local one) for a header URL; "" for anything else."""
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
    except ValueError:  # malformed URL (e.g. a bad IPv6 netloc)
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    if host in _KNOWN_ORIGINS:
        return _KNOWN_ORIGINS[host]
    if host in _LOCAL_HOSTS:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def is_local_url(url: str) -> bool:
    """True for an empty URL or one on a local host."""
    if not url:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def resolve_base_url(backend_base: str, frontend_base: str, profile: str) -> str:
    """Where /magic-login lives when the request gives no usable origin: the profile's deployed
    front end (when every configured URL is local), else the first non-local configured URL."""
    profile_base = ""
    if is_local_url(backend_base) and is_local_url(frontend_base):
        profile_base = _PROFILE_BASES.get(profile, "")
    frontend_non_local = bool(frontend_base) and not is_local_url(frontend_base)
    backend_non_local = bool(backend_base) and not is_local_url(backend_base)
    return (
        profile_base
        or (frontend_base if frontend_non_local else "")
        or (backend_base if backend_non_local else "")
        or frontend_base
        or backend_base
    )


def build_magic_login_link(token: str, request: Request | None = None) -> str:
    """The one-click sign-in link for ``token``, on the front end the invite came from if known."""
    request_origin = ""
    if request is not None:
        request_origin = normalized_origin(request.headers.get("origin"))
        if not request_origin:
            request_origin = normalized_origin(request.headers.get("referer"))

    magic_login_url = (settings.MAGIC_LOGIN_URL or "").strip()
    backend_base = settings.BASE_URL.rstrip("/")
    frontend_base = (settings.FRONTEND_URL or "").split(",")[0].strip().rstrip("/")
    profile = (os.getenv("APP_PROFILE", "") or "").strip().lower()
    resolved_base = resolve_base_url(backend_base, frontend_base, profile)

    base = (request_origin and f"{request_origin}/magic-login") or magic_login_url or f"{resolved_base}/magic-login"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={quote_plus(token)}"


def recipient_name_from_email(email: str) -> str:
    """'jane.doe@x.com' -> 'Jane Doe' (greeting in the email)."""
    local_part = (email.split("@", 1)[0] if "@" in email else email).strip()
    return re.sub(r"[._+\-]+", " ", local_part).strip().title() or "there"


def build_invite_html(
    *,
    recipient_name: str,
    to_email: str,
    magic_login_link: str,
    tenant_name: str,
    support_email: str,
) -> str:
    """Build the HTML body for the invitation email: a set-password link, never a password (I-13)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>You're invited to {tenant_name}</title>
</head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:32px 16px;">
    <tr>
      <td align="center">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">

          <!-- Logo / Header -->
          <tr>
            <td style="padding-bottom:20px;" align="center">
              <span style="font-size:20px;font-weight:700;color:#1e293b;letter-spacing:-0.5px;">
                &#9679; Outreach360.AI
              </span>
            </td>
          </tr>

          <!-- Card -->
          <tr>
            <td style="background:#ffffff;border-radius:12px;border:1px solid #e2e8f0;padding:36px 40px;">

              <!-- Subject line -->
              <p style="margin:0 0 20px;font-size:18px;font-weight:700;color:#0f172a;line-height:1.3;">
                You&#39;re invited to {tenant_name}
              </p>

              <!-- Greeting -->
              <p style="margin:0 0 8px;font-size:15px;color:#334155;">Hi {recipient_name},</p>
              <p style="margin:0 0 20px;font-size:15px;color:#334155;">Hope you&#39;re doing great!</p>

              <p style="margin:0 0 16px;font-size:15px;color:#334155;">
                You have been invited to join <strong>{tenant_name}</strong> on Outreach360.AI as
                <a href="mailto:{to_email}" style="color:#2563eb;text-decoration:none;font-weight:500;">{to_email}</a>.
              </p>

              <!-- Set-password button -->
              <p style="margin:0 0 12px;font-size:14px;color:#334155;">
                Click the button below to sign in and choose your password:
              </p>
              <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
                <tr>
                  <td style="background:#2563eb;border-radius:8px;">
                    <a href="{magic_login_link}"
                      style="display:inline-block;padding:12px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;">
                      Set your password
                    </a>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 24px;font-size:13px;color:#94a3b8;">
                This link works once and expires in {settings.MAGIC_LOGIN_EXPIRE_HOURS} hours. If it has expired, ask
                your administrator to resend the invitation. We will never email you a password.
              </p>

              <hr style="border:none;border-top:1px solid #e2e8f0;margin:0 0 24px;" />

              <!-- Support -->
              <p style="margin:0 0 24px;font-size:14px;color:#475569;">
                Should you face any challenges, please feel free to reach out to us at
                <a href="mailto:{support_email}" style="color:#2563eb;text-decoration:none;">{support_email}</a>.
              </p>

              <!-- Disclaimer -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="border-left:3px solid #e2e8f0;padding:10px 16px;background:#f8fafc;border-radius:0 6px 6px 0;">
                    <p style="margin:0;font-size:12px;color:#64748b;font-style:italic;line-height:1.6;">
                      <strong style="font-style:normal;">Disclaimer:</strong>
                      This email contains a personal sign-in link. The information contained in this communication
                      and any attachments is confidential and intended solely for the use of the individual or entity to
                      whom it is addressed. Access to Outreach360.AI is subject to our Terms of Use. Please do not
                      forward this email or share the link with unauthorized parties. Unauthorized
                      sharing of access or data is strictly prohibited to ensure system integrity.
                    </p>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 0;text-align:center;">
              <p style="margin:0;font-size:12px;color:#94a3b8;">
                &copy; Outreach360.AI &bull; This is an automated email, please do not reply directly.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def build_invite_text(
    *,
    recipient_name: str,
    to_email: str,
    magic_login_link: str,
    tenant_name: str,
    support_email: str,
) -> str:
    """Plain-text fallback for email clients that don't render HTML (no password, I-13)."""
    return "\n".join([
        f"Hi {recipient_name},",
        "",
        "Hope you're doing great!",
        "",
        f"You have been invited to join {tenant_name} on Outreach360.AI as {to_email}.",
        "",
        "Sign in and choose your password with this link (works once, expires in "
        f"{settings.MAGIC_LOGIN_EXPIRE_HOURS} hours):",
        magic_login_link,
        "",
        "If the link has expired, ask your administrator to resend the invitation.",
        "We will never email you a password.",
        "",
        f"Should you face any challenges, reach out at {support_email}.",
        "",
        "Disclaimer:",
        "This email contains a personal sign-in link intended only for the",
        "recipient. Do not forward it or share the link with others.",
    ])


def send_invitation_email(
    *,
    to_email: str,
    magic_login_link: str,
    role: str,
    tenant_name: str,
    inviter_name: str,
) -> Dict[str, Any]:
    """
    Send a workspace invitation email (HTML + plain-text).
    Raises on failure so caller can decide fallback behavior.
    """
    sender_email = (settings.SENDER_EMAIL or "").strip()
    if not sender_email:
        raise RuntimeError("SENDER_EMAIL is not configured")

    support_email = getattr(settings, "SUPPORT_EMAIL", None) or sender_email
    content = {
        "recipient_name": recipient_name_from_email(to_email),
        "to_email": to_email,
        "magic_login_link": magic_login_link,
        "tenant_name": tenant_name,
        "support_email": support_email,
    }
    response = _get_ses_client().send_email(
        Source=f"{settings.SENDER_NAME or 'Outreach360.AI'} <{sender_email}>",
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": f"You're invited to {tenant_name}: set your password", "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": build_invite_text(**content), "Charset": "UTF-8"},
                "Html": {"Data": build_invite_html(**content), "Charset": "UTF-8"},
            },
        },
    )
    return {
        "sent": True,
        "ses_message_id": response.get("MessageId"),
    }
