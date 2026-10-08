# app/routers/platform_admin_router.py
"""
Platform Admin routes — accessible only to PLATFORM_ADMIN users.
Provides cross-tenant management: tenant listing, user overview, platform stats.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.config import settings
from app.core.database import get_db
from app.core.security import hash_password, hash_token
from app.models.campaign import Campaign
from app.models.magic_login_token import MagicLoginToken
from app.models.tenant import Tenant
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["Platform Admin"],
    dependencies=[Depends(require_role("PLATFORM_ADMIN"))],
)


# ── Schemas ──────────────────────────────────────────────────

class TenantSummary(BaseModel):
    tenant_id: str
    tenant_name: str
    status: str
    user_count: int
    campaign_count: int
    created_at: str | None


class TenantDetail(BaseModel):
    tenant_id: str
    tenant_name: str
    status: str
    created_at: str | None
    updated_at: str | None
    users: list[dict]
    campaign_count: int


class PlatformStats(BaseModel):
    total_tenants: int
    active_tenants: int
    suspended_tenants: int
    total_users: int
    active_users: int
    total_campaigns: int


class UpdateTenantStatusRequest(BaseModel):
    status: str  # ACTIVE | SUSPENDED


class UserSummary(BaseModel):
    user_id: str
    email: str
    first_name: str
    last_name: str
    role: str
    status: str
    tenant_id: str | None
    tenant_name: str | None
    last_login_at: str | None
    created_at: str | None


class CreateTenantRequest(BaseModel):
    tenant_name: str
    admin_email: EmailStr
    admin_first_name: str
    admin_last_name: str


# ── Helpers ──────────────────────────────────────────────────

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

    magic_login_url = (getattr(settings, "MAGIC_LOGIN_URL", None) or "").strip()
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
    return f"{base}{separator}token={token}"


def _get_ses_client():
    client_kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", **client_kwargs)


def _send_tenant_invite_email(to_email: str, tenant_name: str, magic_login_link: str) -> dict:
    """Send a tenant onboarding invite email via SES."""
    subject = f"You've been set up on {settings.APP_NAME or 'SalesPro'} — activate your account"
    html_body = f"""
<html><body style="font-family:sans-serif;color:#1e293b;max-width:600px;margin:auto;padding:32px">
<h2 style="color:#2d6bbf">Welcome to {tenant_name}</h2>
<p>Your organization workspace <strong>{tenant_name}</strong> has been created on {settings.APP_NAME or 'SalesPro'}.</p>
<p>You have been assigned the <strong>Super Admin</strong> role. Click below to activate your account:</p>
<p style="margin:28px 0">
  <a href="{magic_login_link}" style="background:linear-gradient(135deg,#0046FF,#73C8D2);color:#fff;padding:12px 28px;border-radius:10px;text-decoration:none;font-weight:600;display:inline-block">
    Activate Account
  </a>
</p>
<p style="color:#64748b;font-size:13px">This link expires in {getattr(settings, 'MAGIC_LOGIN_EXPIRE_HOURS', 72)} hours. If you didn't expect this, you can ignore it.</p>
</body></html>"""
    text_body = f"Welcome to {tenant_name}. Activate your account: {magic_login_link}"

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


# ── Endpoints ────────────────────────────────────────────────

@router.get("/stats", response_model=PlatformStats)
def get_platform_stats(
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
    db: Session = Depends(get_db),
):
    """Platform-wide aggregate statistics."""
    total_tenants = db.query(func.count(Tenant.tenant_id)).scalar() or 0
    active_tenants = db.query(func.count(Tenant.tenant_id)).filter(Tenant.status == "ACTIVE").scalar() or 0
    suspended_tenants = db.query(func.count(Tenant.tenant_id)).filter(Tenant.status == "SUSPENDED").scalar() or 0

    total_users = db.query(func.count(User.user_id)).filter(User.role != "PLATFORM_ADMIN").scalar() or 0
    active_users = (
        db.query(func.count(User.user_id))
        .filter(User.role != "PLATFORM_ADMIN", User.status == "ACTIVE")
        .scalar() or 0
    )

    total_campaigns = db.query(func.count(Campaign.campaign_id)).scalar() or 0

    return PlatformStats(
        total_tenants=total_tenants,
        active_tenants=active_tenants,
        suspended_tenants=suspended_tenants,
        total_users=total_users,
        active_users=active_users,
        total_campaigns=total_campaigns,
    )


@router.get("/tenants", response_model=list[TenantSummary])
def list_tenants(
    search: str = Query(None, description="Search by tenant name"),
    status_filter: str = Query(None, alias="status", description="Filter by status: ACTIVE, SUSPENDED"),
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
    db: Session = Depends(get_db),
):
    """List all tenants with user/campaign counts."""
    query = (
        db.query(
            Tenant.tenant_id,
            Tenant.tenant_name,
            Tenant.status,
            Tenant.created_at,
            func.count(func.distinct(User.user_id)).label("user_count"),
            func.count(func.distinct(Campaign.campaign_id)).label("campaign_count"),
        )
        .outerjoin(User, User.tenant_id == Tenant.tenant_id)
        .outerjoin(Campaign, Campaign.tenant_id == Tenant.tenant_id)
        .group_by(Tenant.tenant_id)
        .order_by(Tenant.created_at.desc())
    )

    if search:
        query = query.filter(Tenant.tenant_name.ilike(f"%{search}%"))
    if status_filter:
        query = query.filter(Tenant.status == status_filter.upper())
    query = query.filter((User.user_id.is_(None)) | (User.role != "PLATFORM_ADMIN"))

    rows = query.all()
    return [
        TenantSummary(
            tenant_id=r.tenant_id,
            tenant_name=r.tenant_name,
            status=r.status or "ACTIVE",
            user_count=r.user_count,
            campaign_count=r.campaign_count,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@router.get("/tenants/{tenant_id}", response_model=TenantDetail)
def get_tenant_detail(
    tenant_id: str,
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
    db: Session = Depends(get_db),
):
    """Detailed view of a single tenant — includes user list."""
    tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    users = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.role != "PLATFORM_ADMIN")
        .order_by(User.created_at.desc())
        .all()
    )
    campaign_count = db.query(func.count(Campaign.campaign_id)).filter(Campaign.tenant_id == tenant_id).scalar() or 0

    return TenantDetail(
        tenant_id=tenant.tenant_id,
        tenant_name=tenant.tenant_name,
        status=tenant.status or "ACTIVE",
        created_at=tenant.created_at.isoformat() if tenant.created_at else None,
        updated_at=tenant.updated_at.isoformat() if tenant.updated_at else None,
        users=[
            {
                "user_id": u.user_id,
                "email": u.email,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "role": u.role,
                "status": u.status,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            }
            for u in users
        ],
        campaign_count=campaign_count,
    )


@router.put("/tenants/{tenant_id}/status")
def update_tenant_status(
    tenant_id: str,
    body: UpdateTenantStatusRequest,
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
    db: Session = Depends(get_db),
):
    """Suspend or reactivate a tenant."""
    if body.status not in ("ACTIVE", "SUSPENDED"):
        raise HTTPException(status_code=400, detail="Status must be ACTIVE or SUSPENDED")

    tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    old = tenant.status
    tenant.status = body.status
    db.commit()

    logger.info(f"[PLATFORM_ADMIN] Tenant '{tenant.tenant_name}' {old} → {body.status} by {current_user.email}")
    return {"message": f"Tenant status updated to {body.status}", "tenant_id": tenant_id}


@router.get("/users", response_model=list[UserSummary])
def list_all_users(
    search: str = Query(None, description="Search by email or name"),
    role: str = Query(None, description="Filter by role"),
    tenant_id: str = Query(None, description="Filter by tenant"),
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
    db: Session = Depends(get_db),
):
    """List all users across all tenants (excludes PLATFORM_ADMIN users)."""
    query = (
        db.query(User, Tenant.tenant_name)
        .outerjoin(Tenant, Tenant.tenant_id == User.tenant_id)
        .filter(User.role != "PLATFORM_ADMIN")
        .order_by(User.created_at.desc())
    )

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (User.email.ilike(search_term))
            | (User.first_name.ilike(search_term))
            | (User.last_name.ilike(search_term))
        )
    if role:
        query = query.filter(User.role == role.upper())
    if tenant_id:
        query = query.filter(User.tenant_id == tenant_id)

    rows = query.limit(200).all()
    return [
        UserSummary(
            user_id=u.user_id,
            email=u.email,
            first_name=u.first_name or "",
            last_name=u.last_name or "",
            role=u.role,
            status=u.status,
            tenant_id=u.tenant_id,
            tenant_name=tenant_name,
            last_login_at=u.last_login_at.isoformat() if u.last_login_at else None,
            created_at=u.created_at.isoformat() if u.created_at else None,
        )
        for u, tenant_name in rows
    ]


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
    db: Session = Depends(get_db),
):
    """Create a new tenant and its initial Super Admin user, then send an activation invite."""
    tenant_name = body.tenant_name.strip()
    if not tenant_name:
        raise HTTPException(status_code=400, detail="Tenant name cannot be empty")

    # Prevent duplicate tenant names
    existing_tenant = db.query(Tenant).filter(Tenant.tenant_name.ilike(tenant_name)).first()
    if existing_tenant:
        raise HTTPException(status_code=409, detail="A tenant with this name already exists")

    # Prevent duplicate admin email across ALL tenants
    existing_user = db.query(User).filter(User.email == body.admin_email).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    # Create tenant
    tenant = Tenant(tenant_name=tenant_name, status="ACTIVE")
    db.add(tenant)
    db.flush()  # get tenant_id

    # Create SUPER_ADMIN user with INVITED status
    import string as _string
    temp_password = "".join(
        secrets.choice(_string.ascii_letters + _string.digits + "!@#$%") for _ in range(16)
    )
    admin_user = User(
        tenant_id=tenant.tenant_id,
        first_name=body.admin_first_name.strip(),
        last_name=body.admin_last_name.strip(),
        email=body.admin_email,
        password_hash=hash_password(temp_password),
        role="SUPER_ADMIN",
        auth_provider="local",
        email_verified=True,
        status="INVITED",
    )
    db.add(admin_user)
    db.flush()

    # Generate magic login token
    raw_token = secrets.token_urlsafe(48)
    magic_link = _build_magic_login_link_for_origin(raw_token, request)
    expire_hours = getattr(settings, "MAGIC_LOGIN_EXPIRE_HOURS", 72)
    magic_token = MagicLoginToken(
        user_id=admin_user.user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(hours=expire_hours),
    )
    db.add(magic_token)
    db.commit()

    # Try to send invite email
    email_result = _send_tenant_invite_email(
        to_email=body.admin_email,
        tenant_name=tenant_name,
        magic_login_link=magic_link,
    )

    if email_result["sent"]:
        logger.info(f"[PLATFORM_ADMIN] Tenant '{tenant_name}' created, invite sent to {body.admin_email}")
    else:
        logger.warning(
            f"[PLATFORM_ADMIN] Tenant '{tenant_name}' created but invite email failed: {email_result['error']}"
        )

    return {
        "tenant_id": tenant.tenant_id,
        "tenant_name": tenant_name,
        "admin_user_id": admin_user.user_id,
        "admin_email": body.admin_email,
        "email_sent": email_result["sent"],
        "magic_login_link": magic_link if not email_result["sent"] else None,
    }
