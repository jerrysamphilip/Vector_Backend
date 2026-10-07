# app/routers/user_router.py
"""
Team management endpoints: list users, invite, change role, deactivate.
"""

import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role, require_permission
from app.core.config import settings
from app.core.database import get_db
from app.core.security import hash_password, hash_token
from app.models.audit import AuditLog
from app.models.campaign import Campaign
from app.models.campaign_group import CampaignGroup
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.invitation import Invitation
from app.models.magic_login_token import MagicLoginToken
from app.models.password_reset_token import PasswordResetToken
from app.models.prospect_list import ProspectList
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["User Management"])

VALID_ROLES = {"SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"}

# All available feature permission keys
ALL_PERMISSIONS = {
    "manage_campaigns",   # Create / edit / delete campaigns
    "manage_templates",   # Create / edit email templates
    "manage_inboxes",     # Connect and configure email inboxes
    "manage_prospects",   # Import and manage prospect lists
    "view_analytics",     # View reports and analytics dashboard
    "export_data",        # Export campaigns, contacts and reports
    "manage_team",        # Invite and manage team members
}

# Default permissions granted when no custom override is set
DEFAULT_PERMISSIONS: dict[str, list[str]] = {
    "PLATFORM_ADMIN": ["manage_platform"],
    "SUPER_ADMIN":    list(ALL_PERMISSIONS),
    "ADMIN":          ["manage_campaigns", "manage_templates", "manage_inboxes",
                       "manage_prospects", "view_analytics", "export_data", "manage_team"],
    "MANAGER":        ["manage_campaigns", "manage_templates", "view_analytics"],
    "AGENT":          ["manage_campaigns"],
}


def _get_ses_client():
    """Build SES client using configured region/credentials."""
    client_kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", **client_kwargs)


def _build_magic_login_link(token: str) -> str:
    return _build_magic_login_link_for_origin(token, None)


def _build_magic_login_link_for_origin(token: str, request: Request | None) -> str:
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

    magic_login_url = (settings.MAGIC_LOGIN_URL or "").strip()
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
    base = (request_origin and f"{request_origin}/magic-login") or magic_login_url or f"{resolved_base}/magic-login"
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={quote_plus(token)}"


def _derive_name_from_email(email: str) -> tuple[str, str]:
    local_part = (email.split("@", 1)[0] if "@" in email else email).strip()
    parts = [p for p in re.split(r"[._+\-]+", local_part) if p]
    first = (parts[0].title() if parts else "Team")
    last = (parts[1].title() if len(parts) > 1 else "Member")
    return first[:100], last[:100]


def _generate_temporary_password() -> str:
    specials = "!@#$%&*"
    password = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    alphabet = string.ascii_letters + string.digits + specials
    password.extend(secrets.choice(alphabet) for _ in range(8))
    secrets.SystemRandom().shuffle(password)
    return "".join(password)


def _build_invite_html(
    *,
    recipient_name: str,
    to_email: str,
    temporary_password: str,
    magic_login_link: str,
    tenant_name: str,
    support_email: str,
) -> str:
    """Build the HTML body for the invitation email."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Your Secure Login Credentials</title>
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
                Your Secure Login Credentials to {tenant_name}
              </p>

              <!-- Greeting -->
              <p style="margin:0 0 8px;font-size:15px;color:#334155;">Hi {recipient_name},</p>
              <p style="margin:0 0 20px;font-size:15px;color:#334155;">Hope you&#39;re doing great!</p>

              <p style="margin:0 0 16px;font-size:15px;color:#334155;">
                Below are your secure login credentials to access <strong>{tenant_name}</strong>:
              </p>

              <!-- Credentials box -->
              <table width="100%" cellpadding="0" cellspacing="0"
                style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:20px;">
                <tr>
                  <td style="padding:16px 20px;border-bottom:1px solid #e2e8f0;">
                    <span style="font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Email</span><br/>
                    <a href="mailto:{to_email}" style="font-size:15px;color:#2563eb;text-decoration:none;font-weight:500;margin-top:4px;display:inline-block;">
                      {to_email}
                    </a>
                  </td>
                </tr>
                <tr>
                  <td style="padding:16px 20px;">
                    <span style="font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Password</span><br/>
                    <span style="font-size:15px;font-weight:700;color:#0f172a;font-family:monospace;margin-top:4px;display:inline-block;background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:4px 10px;">
                      {temporary_password}
                    </span>
                  </td>
                </tr>
              </table>

              <!-- Magic login button -->
              <p style="margin:0 0 12px;font-size:14px;color:#334155;">
                Or click the button below for instant one-click access:
              </p>
              <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
                <tr>
                  <td style="background:#2563eb;border-radius:8px;">
                    <a href="{magic_login_link}"
                      style="display:inline-block;padding:12px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;">
                      &#x1F517; Login to {tenant_name}
                    </a>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 24px;font-size:13px;color:#94a3b8;">
                This link expires in {settings.MAGIC_LOGIN_EXPIRE_HOURS} hours. After that, use your email and password to sign in.
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
                      This email contains sensitive login information. The information contained in this communication
                      and any attachments is confidential and intended solely for the use of the individual or entity to
                      whom it is addressed. Access to Outreach360.AI is subject to our Terms of Use. Please ensure
                      your login credentials are kept secure and are not shared with unauthorized parties. Unauthorized
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


def _build_invite_text(
    *,
    recipient_name: str,
    to_email: str,
    temporary_password: str,
    magic_login_link: str,
    tenant_name: str,
    support_email: str,
) -> str:
    """Plain-text fallback for email clients that don't render HTML."""
    return "\n".join([
        f"Hi {recipient_name},",
        "",
        "Hope you're doing great!",
        "",
        f"Below are your secure login credentials to access {tenant_name}:",
        "",
        f"  Email:    {to_email}",
        f"  Password: {temporary_password}",
        "",
        "One-click login link (expires in "
        f"{settings.MAGIC_LOGIN_EXPIRE_HOURS} hours):",
        magic_login_link,
        "",
        f"Should you face any challenges, reach out at {support_email}.",
        "",
        "Disclaimer:",
        "This email contains sensitive login information intended only for the",
        "recipient. Keep your credentials secure and do not share with others.",
    ])


def _send_invitation_email(
    *,
    to_email: str,
    temporary_password: str,
    magic_login_link: str,
    role: str,
    tenant_name: str,
    inviter_name: str,
) -> dict:
    """
    Send a workspace invitation email (HTML + plain-text).
    Raises on failure so caller can decide fallback behavior.
    """
    sender_email = (settings.SENDER_EMAIL or "").strip()
    if not sender_email:
        raise RuntimeError("SENDER_EMAIL is not configured")

    support_email = getattr(settings, "SUPPORT_EMAIL", None) or sender_email
    local_part = (to_email.split("@", 1)[0] if "@" in to_email else to_email).strip()
    recipient_name = re.sub(r"[._+\-]+", " ", local_part).strip().title() or "there"

    subject = f"Your Secure Login Credentials to {tenant_name}"

    html_body = _build_invite_html(
        recipient_name=recipient_name,
        to_email=to_email,
        temporary_password=temporary_password,
        magic_login_link=magic_login_link,
        tenant_name=tenant_name,
        support_email=support_email,
    )

    text_body = _build_invite_text(
        recipient_name=recipient_name,
        to_email=to_email,
        temporary_password=temporary_password,
        magic_login_link=magic_login_link,
        tenant_name=tenant_name,
        support_email=support_email,
    )

    ses_client = _get_ses_client()
    response = ses_client.send_email(
        Source=f"{settings.SENDER_NAME or 'Outreach360.AI'} <{sender_email}>",
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html": {"Data": html_body, "Charset": "UTF-8"},
            },
        },
    )

    return {
        "sent": True,
        "magic_login_link": magic_login_link,
        "ses_message_id": response.get("MessageId"),
    }


# ── Schemas ──────────────────────────────────────────────

class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "AGENT"


class AcceptInviteRequest(BaseModel):
    token: str
    first_name: str
    last_name: str
    password: str


class ChangeRoleRequest(BaseModel):
    role: str


class UserOut(BaseModel):
    user_id: str
    tenant_id: str
    first_name: str
    last_name: str
    email: str
    role: str
    status: str
    auth_provider: str
    email_verified: bool
    avatar_url: str | None
    created_at: datetime | None
    last_login_at: datetime | None
    permissions: list[str]  # Effective permissions (custom or role default)


class UpdatePermissionsRequest(BaseModel):
    permissions: list[str]


# ── Endpoints ────────────────────────────────────────────

@router.get("", response_model=list[UserOut])
def list_users(
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """List all team members in the current tenant."""
    users = (
        db.query(User)
        .filter(
            User.tenant_id == current_user.tenant_id,
            User.role != "PLATFORM_ADMIN",
        )
        .order_by(User.created_at.desc())
        .all()
    )
    return [
        UserOut(
            user_id=u.user_id,
            tenant_id=u.tenant_id,
            first_name=u.first_name,
            last_name=u.last_name,
            email=u.email,
            role=u.role,
            status=u.status,
            auth_provider=u.auth_provider or "local",
            email_verified=u.email_verified or False,
            avatar_url=u.avatar_url,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
            permissions=u.custom_permissions if u.custom_permissions is not None
                        else DEFAULT_PERMISSIONS.get(u.role, []),
        )
        for u in users
    ]


@router.post("/invite", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_permission("manage_team"))])
def invite_user(
    body: InviteRequest,
    request: Request,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """Send an invitation to join the tenant workspace."""
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")

    # Only SUPER_ADMIN can invite another SUPER_ADMIN — ADMIN cannot
    if body.role == "SUPER_ADMIN" and current_user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Only a Super Admin can invite another Super Admin")

    # Check if user already exists in this tenant
    existing = (
        db.query(User)
        .filter(User.tenant_id == current_user.tenant_id, User.email == body.email)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists in your workspace")

    # Check for pending invitation
    pending = (
        db.query(Invitation)
        .filter(
            Invitation.tenant_id == current_user.tenant_id,
            Invitation.email == body.email,
            Invitation.status == "PENDING",
        )
        .first()
    )
    if pending:
        pending.status = "REVOKED"

    first_name, last_name = _derive_name_from_email(body.email)
    temporary_password = _generate_temporary_password()

    user = User(
        tenant_id=current_user.tenant_id,
        first_name=first_name,
        last_name=last_name,
        email=body.email,
        password_hash=hash_password(temporary_password),
        role=body.role,
        auth_provider="local",
        email_verified=True,
        status="INVITED",
        invited_by=current_user.user_id,
    )
    db.add(user)
    db.flush()

    raw_magic_token = secrets.token_urlsafe(48)
    magic_login_link = _build_magic_login_link_for_origin(raw_magic_token, request)
    magic_token = MagicLoginToken(
        user_id=user.user_id,
        token_hash=hash_token(raw_magic_token),
        expires_at=datetime.utcnow() + timedelta(hours=settings.MAGIC_LOGIN_EXPIRE_HOURS),
    )
    db.add(magic_token)

    # Keep invitation row for audit/history.
    invitation = Invitation(
        tenant_id=current_user.tenant_id,
        invited_by=current_user.user_id,
        email=body.email,
        role=body.role,
        token=hash_token(secrets.token_urlsafe(48)),
        status="ACCEPTED",
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.add(invitation)
    db.commit()

    tenant = db.query(Tenant).filter(Tenant.tenant_id == current_user.tenant_id).first()
    tenant_name = tenant.tenant_name if tenant and tenant.tenant_name else "your workspace"
    inviter_name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email

    email_delivery = {
        "attempted": True,
        "sent": False,
        "magic_login_link": magic_login_link,
        "ses_message_id": None,
        "error": None,
    }
    try:
        send_result = _send_invitation_email(
            to_email=body.email,
            temporary_password=temporary_password,
            magic_login_link=magic_login_link,
            role=body.role,
            tenant_name=tenant_name,
            inviter_name=inviter_name,
        )
        email_delivery.update(send_result)
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        email_delivery["error"] = str(exc)
        logger.error(f"Invitation email failed for {body.email}: {exc}")
    except Exception as exc:
        email_delivery["error"] = str(exc)
        logger.exception(f"Unexpected invitation email failure for {body.email}: {exc}")

    if email_delivery.get("sent"):
        logger.info(f"Invitation email sent: {body.email} as {body.role} by {current_user.email}")
    else:
        logger.warning(
            f"Invitation created but email not sent: {body.email} as {body.role} by {current_user.email}. "
            f"Reason: {email_delivery.get('error')}"
        )

    return {
        "invitation_id": invitation.invitation_id,
        "user_id": user.user_id,
        "email": body.email,
        "role": body.role,
        "invite_token": None,
        "expires_at": invitation.expires_at.isoformat(),
        "email_delivery": email_delivery,
        "manual_credentials": {
            "email": body.email,
            "password": temporary_password,
            "magic_login_link": magic_login_link,
        } if not email_delivery.get("sent") else None,
    }


@router.post("/accept-invite", status_code=status.HTTP_201_CREATED)
def accept_invite(body: AcceptInviteRequest, db: Session = Depends(get_db)):
    """Accept an invitation and create a user account."""
    token_hash = hash_token(body.token)
    invitation = (
        db.query(Invitation)
        .filter(Invitation.token == token_hash, Invitation.status == "PENDING")
        .first()
    )

    if not invitation:
        raise HTTPException(status_code=404, detail="Invalid or expired invitation")

    if invitation.expires_at < datetime.utcnow():
        invitation.status = "EXPIRED"
        db.commit()
        raise HTTPException(status_code=410, detail="Invitation has expired")

    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Check if email already exists in the tenant
    existing = (
        db.query(User)
        .filter(User.tenant_id == invitation.tenant_id, User.email == invitation.email)
        .first()
    )
    if existing:
        invitation.status = "ACCEPTED"
        db.commit()
        raise HTTPException(status_code=409, detail="User already exists in this workspace")

    user = User(
        tenant_id=invitation.tenant_id,
        first_name=body.first_name,
        last_name=body.last_name,
        email=invitation.email,
        password_hash=hash_password(body.password),
        role=invitation.role,
        auth_provider="local",
        email_verified=True,
        invited_by=invitation.invited_by,
    )
    db.add(user)

    invitation.status = "ACCEPTED"
    db.commit()

    logger.info(f"Invitation accepted: {invitation.email} joined as {invitation.role}")

    return {
        "message": "Account created successfully",
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
    }


@router.put("/{user_id}/role", dependencies=[Depends(require_permission("manage_team"))])
def change_role(
    user_id: str,
    body: ChangeRoleRequest,
    current_user: User = Depends(require_role("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    """Change a team member's role (SUPER_ADMIN only)."""
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")

    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.role == "SUPER_ADMIN":
        raise HTTPException(status_code=400, detail="Cannot change the Super Admin's role")

    old_role = target.role
    target.role = body.role
    db.commit()

    logger.info(f"Role changed: {target.email} {old_role} → {body.role} by {current_user.email}")
    return {"message": f"Role updated to {body.role}", "user_id": user_id}


@router.delete("/{user_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(require_permission("manage_team"))])
def deactivate_user(
    user_id: str,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """Deactivate a team member (SUPER_ADMIN or ADMIN). Sets status to INACTIVE."""
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.role == "SUPER_ADMIN":
        raise HTTPException(status_code=400, detail="Cannot deactivate the workspace Super Admin")

    # ADMIN can only deactivate MANAGERs, not other ADMINs
    if current_user.role == "ADMIN" and target.role == "ADMIN":
        raise HTTPException(status_code=403, detail="Admins cannot deactivate other admins")

    if target.status == "INACTIVE":
        raise HTTPException(status_code=400, detail="User is already inactive")

    prev_status = target.status
    target.status = "INACTIVE"
    db.commit()

    action = "revoked invite for" if prev_status == "INVITED" else "deactivated"
    logger.info(f"User {action}: {target.email} by {current_user.email}")
    return {"message": "User deactivated", "user_id": user_id}


@router.put("/{user_id}/reactivate", status_code=status.HTTP_200_OK, dependencies=[Depends(require_permission("manage_team"))])
def reactivate_user(
    user_id: str,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """Reactivate a previously deactivated team member (SUPER_ADMIN or ADMIN)."""
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot reactivate yourself")

    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.status == "ACTIVE":
        raise HTTPException(status_code=400, detail="User is already active")

    if current_user.role == "ADMIN" and target.role == "ADMIN":
        raise HTTPException(status_code=403, detail="Admins cannot reactivate other admins")

    target.status = "ACTIVE"
    db.commit()

    logger.info(f"User reactivated: {target.email} by {current_user.email}")
    return {"message": "User reactivated", "user_id": user_id}


@router.delete("/{user_id}/permanent", status_code=status.HTTP_200_OK, dependencies=[Depends(require_permission("manage_team"))])
def delete_user_permanent(
    user_id: str,
    current_user: User = Depends(require_role("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    """Permanently delete a deactivated team member (SUPER_ADMIN only)."""
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.role == "SUPER_ADMIN":
        raise HTTPException(status_code=400, detail="Cannot delete the workspace Super Admin")

    if target.status != "INACTIVE":
        raise HTTPException(status_code=400, detail="User must be deactivated before permanent deletion")

    # Block deletion if user has created business data (non-nullable FK constraints)
    has_campaigns = db.query(Campaign).filter(Campaign.created_by == user_id).first()
    if has_campaigns:
        raise HTTPException(
            status_code=409,
            detail="Cannot permanently delete user: they have created campaigns. Remove or reassign those campaigns first.",
        )

    has_prospect_lists = db.query(ProspectList).filter(ProspectList.uploaded_by == user_id).first()
    if has_prospect_lists:
        raise HTTPException(
            status_code=409,
            detail="Cannot permanently delete user: they have uploaded prospect lists. Remove those lists first.",
        )

    has_campaign_groups = db.query(CampaignGroup).filter(CampaignGroup.owner_id == user_id).first()
    if has_campaign_groups:
        raise HTTPException(
            status_code=409,
            detail="Cannot permanently delete user: they own campaign groups. Remove those groups first.",
        )

    has_template_versions = db.query(EmailTemplateVersion).filter(EmailTemplateVersion.created_by == user_id).first()
    if has_template_versions:
        raise HTTPException(
            status_code=409,
            detail="Cannot permanently delete user: they have created email templates. Remove those templates first.",
        )

    email = target.email

    # Delete all token/session records first (safe to always delete)
    db.query(MagicLoginToken).filter(MagicLoginToken.user_id == user_id).delete(synchronize_session=False)
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete(synchronize_session=False)
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user_id).delete(synchronize_session=False)

    # Delete invitations this user sent (invited_by is NOT NULL so can't null it out)
    db.query(Invitation).filter(Invitation.invited_by == user_id).delete(synchronize_session=False)

    # Null out audit logs for this user (user_id is NOT NULL — delete audit trail)
    db.query(AuditLog).filter(AuditLog.user_id == user_id).delete(synchronize_session=False)

    # Null out nullable FK: email_templates approved_by
    db.query(EmailTemplate).filter(EmailTemplate.approved_by == user_id).update(
        {EmailTemplate.approved_by: None}, synchronize_session=False
    )

    # Clear invited_by on users this person invited (self-referential, nullable)
    db.query(User).filter(User.invited_by == user_id).update(
        {User.invited_by: None}, synchronize_session=False
    )

    db.delete(target)
    db.commit()

    logger.info(f"User permanently deleted: {email} by {current_user.email}")
    return {"message": "User permanently deleted", "user_id": user_id}


@router.get("/{user_id}/permissions")
def get_permissions(
    user_id: str,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """Get effective permissions for a team member."""
    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    effective = (target.custom_permissions if target.custom_permissions is not None
                 else DEFAULT_PERMISSIONS.get(target.role, []))
    return {
        "user_id": user_id,
        "role": target.role,
        "permissions": effective,
        "is_custom": target.custom_permissions is not None,
        "defaults": DEFAULT_PERMISSIONS.get(target.role, []),
    }


@router.put("/{user_id}/permissions", status_code=status.HTTP_200_OK)
def update_permissions(
    user_id: str,
    body: UpdatePermissionsRequest,
    current_user: User = Depends(require_role("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    """Set custom permissions for a team member (SUPER_ADMIN only)."""
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own permissions")

    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.role == "SUPER_ADMIN":
        raise HTTPException(status_code=400, detail="Cannot restrict Super Admin permissions")

    # Validate all keys are known
    invalid = set(body.permissions) - ALL_PERMISSIONS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown permission keys: {', '.join(invalid)}")

    # ADMINs cannot be granted manage_team by SUPER_ADMIN either — it's implicit in their role
    target.custom_permissions = list(set(body.permissions))
    db.commit()

    logger.info(f"Permissions updated for {target.email} by {current_user.email}: {target.custom_permissions}")
    return {
        "message": "Permissions updated",
        "user_id": user_id,
        "permissions": target.custom_permissions,
    }


@router.delete("/{user_id}/permissions", status_code=status.HTTP_200_OK)
def reset_permissions(
    user_id: str,
    current_user: User = Depends(require_role("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    """Reset a user's permissions back to their role defaults (SUPER_ADMIN only)."""
    target = (
        db.query(User)
        .filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target.custom_permissions = None
    db.commit()

    logger.info(f"Permissions reset to defaults for {target.email} by {current_user.email}")
    return {
        "message": "Permissions reset to role defaults",
        "user_id": user_id,
        "permissions": DEFAULT_PERMISSIONS.get(target.role, []),
    }


@router.get("/invitations")
def list_invitations(
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """List all invitations for the current tenant."""
    invitations = (
        db.query(Invitation)
        .filter(Invitation.tenant_id == current_user.tenant_id)
        .order_by(Invitation.created_at.desc())
        .all()
    )

    # Auto-expire old invitations
    now = datetime.utcnow()
    for inv in invitations:
        if inv.status == "PENDING" and inv.expires_at < now:
            inv.status = "EXPIRED"
    db.commit()

    return [
        {
            "invitation_id": inv.invitation_id,
            "email": inv.email,
            "role": inv.role,
            "status": inv.status,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        }
        for inv in invitations
    ]


@router.delete("/invitations/{invitation_id}", dependencies=[Depends(require_permission("manage_team"))])
def revoke_invitation(
    invitation_id: str,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
    db: Session = Depends(get_db),
):
    """Revoke a pending invitation."""
    invitation = (
        db.query(Invitation)
        .filter(
            Invitation.invitation_id == invitation_id,
            Invitation.tenant_id == current_user.tenant_id,
        )
        .first()
    )
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    if invitation.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Cannot revoke invitation with status: {invitation.status}")

    invitation.status = "REVOKED"
    db.commit()

    logger.info(f"Invitation revoked: {invitation.email} by {current_user.email}")
    return {"message": "Invitation revoked"}
