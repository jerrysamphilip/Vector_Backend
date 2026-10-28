"""
Platform administration (PLATFORM_ADMIN only; chunk A5): platform statistics, tenants (list, detail,
suspend / reactivate, create with its first Super Admin) and the cross-tenant user list.

``PlatformAdminService`` serves app/routers/platform_admin_router.py, one unit of work per call; the SQL is in
app/repositories/identity_platform.py (cross-tenant on purpose) and app/repositories/identity.py.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.clock import iso_utc, utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.security import hash_token
from app.domain.identity import SETTABLE_TENANT_STATUSES, SUPER_ADMIN, TenantStatus, UserStatus
from app.repositories.identity import DuplicateAccount, IdentityUnitOfWork, Session, identity_uow
from app.schemas.platform_admin_schema import PlatformStats, TenantDetail, TenantSummary, UserSummary
from app.services.invitation_email import HeaderSource, build_magic_login_link
from app.services.invitation_service import show_once_link

__all__ = ["PlatformAdminService", "send_tenant_invite_email"]

logger = logging.getLogger(__name__)

USER_EXISTS = "A user with this email already exists"


def _iso(value: Any) -> Optional[str]:
    return iso_utc(value) if value else None


def _get_ses_client() -> Any:
    client_kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", **client_kwargs)


def send_tenant_invite_email(to_email: str, tenant_name: str, magic_login_link: str) -> dict:
    """Send a tenant onboarding invite email via SES."""
    subject = f"You've been set up on {settings.APP_NAME or 'SalesPro'} — activate your account"
    html_body = f"""
<html><body style="font-family:sans-serif;color:#1e293b;max-width:600px;margin:auto;padding:32px">
<h2 style="color:#2d6bbf">Welcome to {tenant_name}</h2>
<p>Your organization workspace <strong>{tenant_name}</strong> has been created on {settings.APP_NAME or 'SalesPro'}.</p>
<p>You have been assigned the <strong>Super Admin</strong> role. Click below to activate your account and choose your password:</p>
<p style="margin:28px 0">
  <a href="{magic_login_link}" style="background:linear-gradient(135deg,#0046FF,#73C8D2);color:#fff;padding:12px 28px;border-radius:10px;text-decoration:none;font-weight:600;display:inline-block">
    Activate Account
  </a>
</p>
<p style="color:#64748b;font-size:13px">This link expires in {getattr(settings, 'MAGIC_LOGIN_EXPIRE_HOURS', 72)} hours and works once. We will never email you a password. If you didn't expect this, you can ignore it.</p>
</body></html>"""
    text_body = (f"Welcome to {tenant_name}. Activate your account and choose your password "
                 f"(the link works once): {magic_login_link}")

    sender_email = (settings.SENDER_EMAIL or "").strip()
    if not sender_email:
        return {"sent": False, "ses_message_id": None, "error": "SENDER_EMAIL is not configured"}
    try:
        ses = _get_ses_client()
        response = ses.send_email(
            Source=f"{settings.SENDER_NAME or settings.APP_NAME} <{sender_email}>",
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        return {"sent": True, "ses_message_id": response["MessageId"], "error": None}
    except (ClientError, BotoCoreError, Exception) as exc:
        return {"sent": False, "ses_message_id": None, "error": str(exc)}


def _member_out(u: Any) -> Dict[str, Any]:
    return {
        "user_id": u.user_id,
        "email": u.email,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "role": u.role,
        "status": u.status,
        "last_login_at": _iso(u.last_login_at),
    }


def _user_summary(u: Any, tenant_name: Optional[str]) -> UserSummary:
    return UserSummary(
        user_id=u.user_id,
        email=u.email,
        first_name=u.first_name or "",
        last_name=u.last_name or "",
        role=u.role,
        status=u.status,
        tenant_id=u.tenant_id,
        tenant_name=tenant_name,
        last_login_at=_iso(u.last_login_at),
        created_at=_iso(u.created_at),
    )


class PlatformAdminService:
    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> PlatformAdminService:
        return cls(identity_uow(db))

    def stats(self) -> PlatformStats:
        """Platform-wide aggregate statistics."""
        with self.uow as uow:
            counts = uow.platform.stats()
        return PlatformStats(
            total_tenants=counts.total_tenants,
            active_tenants=counts.active_tenants,
            suspended_tenants=counts.suspended_tenants,
            total_users=counts.total_users,
            active_users=counts.active_users,
            total_campaigns=counts.total_campaigns,
        )

    def list_tenants(self, search: Optional[str], status: Optional[str]) -> List[TenantSummary]:
        """All tenants with user / campaign counts, newest first."""
        with self.uow as uow:
            rows = uow.platform.tenant_summaries(search, status.upper() if status else None)
        return [
            TenantSummary(
                tenant_id=r.tenant_id,
                tenant_name=r.tenant_name,
                status=r.status or TenantStatus.ACTIVE,
                user_count=r.user_count,
                campaign_count=r.campaign_count,
                created_at=_iso(r.created_at),
            )
            for r in rows
        ]

    def tenant_detail(self, tenant_id: str) -> TenantDetail:
        """One tenant with its users (newest first) and campaign count."""
        with self.uow as uow:
            tenant = uow.tenants.get(tenant_id)
            if not tenant:
                raise NotFoundError("Tenant not found")
            users = uow.platform.tenant_users(tenant_id)
            return TenantDetail(
                tenant_id=tenant.tenant_id,
                tenant_name=tenant.tenant_name,
                status=tenant.status or TenantStatus.ACTIVE,
                created_at=_iso(tenant.created_at),
                updated_at=_iso(tenant.updated_at),
                users=[_member_out(u) for u in users],
                campaign_count=uow.platform.campaign_count(tenant_id),
            )

    def update_tenant_status(self, actor: Any, tenant_id: str, status: str) -> Dict[str, Any]:
        """Suspend or reactivate a tenant (its users are refused at sign-in and on every request)."""
        if status not in SETTABLE_TENANT_STATUSES:
            raise BadRequestError("Status must be ACTIVE or SUSPENDED")
        with self.uow as uow:
            tenant = uow.tenants.get(tenant_id)
            if not tenant:
                raise NotFoundError("Tenant not found")
            old = tenant.status
            tenant.status = status
            uow.commit()
            logger.info(f"[PLATFORM_ADMIN] Tenant '{tenant.tenant_name}' {old} → {status} by {actor.email}")
        return {"message": f"Tenant status updated to {status}", "tenant_id": tenant_id}

    def list_users(self, search: Optional[str], role: Optional[str], tenant_id: Optional[str]) -> List[UserSummary]:
        """Users of every tenant (not platform admins), newest first, at most 200."""
        with self.uow as uow:
            rows = uow.platform.users(search, role.upper() if role else None, tenant_id)
            return [_user_summary(u, tenant_name) for u, tenant_name in rows]

    def create_tenant(self, tenant_name: str, admin_email: str, admin_first_name: str, admin_last_name: str,
                      request: Optional[HeaderSource]) -> Dict[str, Any]:
        """A new tenant and its first Super Admin (INVITED, no password: they choose one through the
        emailed set-password link, I-13); the activation email is sent after the commit."""
        tenant_name = tenant_name.strip()
        if not tenant_name:
            raise BadRequestError("Tenant name cannot be empty")
        with self.uow as uow:
            if uow.tenants.name_taken(tenant_name):
                raise ConflictError("A tenant with this name already exists")
            # Prevent duplicate admin email across ALL tenants
            if uow.users.any_with_email(admin_email):
                raise ConflictError(USER_EXISTS)
            tenant = uow.tenants.add(tenant_name=tenant_name, status=TenantStatus.ACTIVE)
            uow.flush()  # get tenant_id
            admin_user = uow.users.add(
                tenant_id=tenant.tenant_id, first_name=admin_first_name.strip(), last_name=admin_last_name.strip(),
                email=admin_email, password_hash=None, role=SUPER_ADMIN, auth_provider="local",
                email_verified=True, status=UserStatus.INVITED,
            )
            try:
                uow.flush_account()
            except DuplicateAccount as exc:  # one live account per email (F017), or a concurrent create
                raise ConflictError(USER_EXISTS) from exc
            raw_token = secrets.token_urlsafe(48)
            magic_link = build_magic_login_link(raw_token, request)
            expire_hours = getattr(settings, "MAGIC_LOGIN_EXPIRE_HOURS", 72)
            uow.tokens.add_magic_link(admin_user.user_id, hash_token(raw_token),
                                      utcnow() + timedelta(hours=expire_hours))
            uow.commit()
            created = {"tenant_id": tenant.tenant_id, "admin_user_id": admin_user.user_id}

        email_result = send_tenant_invite_email(
            to_email=admin_email,
            tenant_name=tenant_name,
            magic_login_link=magic_link,
        )
        if email_result["sent"]:
            logger.info(f"[PLATFORM_ADMIN] Tenant '{tenant_name}' created, invite sent to {admin_email}")
        else:
            logger.warning(
                f"[PLATFORM_ADMIN] Tenant '{tenant_name}' created but invite email failed: {email_result['error']}"
            )
        return {
            "tenant_id": created["tenant_id"],
            "tenant_name": tenant_name,
            "admin_user_id": created["admin_user_id"],
            "admin_email": admin_email,
            "email_sent": email_result["sent"],
            # Show-once fallback, local development without email only (refused in production / deployed)
            "magic_login_link": (show_once_link(admin_email, magic_link, bool(email_result["sent"])) or {}).get(
                "magic_login_link"),
        }
