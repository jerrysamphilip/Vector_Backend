# app/services/invitation_service.py
"""
Workspace invitations: invite, resend, accept, list and revoke.

An invitation creates the INVITED account up front WITHOUT a usable password and emails a
one-time set-password (sign-in) link; the invitee chooses their own password on first use.
No password is ever generated, emailed or returned in an API response (rule I-13). The
invitation stays PENDING until the invitee first signs in or accepts. Revoking it
deactivates the invited account. Errors are domain errors.
"""

import logging
import re
import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Request
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, GoneError, NotFoundError, TooManyRequests
from app.core.secrets_guard import is_deployed
from app.core.security import hash_password, hash_token
from app.models.invitation import Invitation
from app.models.magic_login_token import MagicLoginToken
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.user_admin_schema import AcceptInviteRequest
from app.services import invitation_email
from app.services.user_admin_service import (
    require_can_grant_role,
    revoke_pending_invitations,
    spend_magic_links,
    validate_role,
)

logger = logging.getLogger(__name__)

RESEND_COOLDOWN_SECONDS = 60  # one resend per invited user per minute
INVITATION_VALID_DAYS = 7
MIN_PASSWORD_LENGTH = 8
_LOCAL_ENVIRONMENTS = ("development", "dev", "local")


# ── Pure helpers ───────────────────────────────────────────────────

def resend_wait_seconds(seconds_since_last: int | None, cooldown: int = RESEND_COOLDOWN_SECONDS) -> int:
    """Seconds to wait before the invitation can be sent again (0 = now). None = never sent."""
    if seconds_since_last is None:
        return 0
    if seconds_since_last < 0:  # created "in the future" (clock skew): treat as just sent
        return cooldown
    return max(0, cooldown - int(seconds_since_last))


def derive_name_from_email(email: str) -> tuple[str, str]:
    """Placeholder (first, last) name from the address until the invitee sets their own."""
    local_part = (email.split("@", 1)[0] if "@" in email else email).strip()
    parts = [p for p in re.split(r"[._+\-]+", local_part) if p]
    first = (parts[0].title() if parts else "Team")
    last = (parts[1].title() if len(parts) > 1 else "Member")
    return first[:100], last[:100]


def may_show_link_once(email_sent: bool, environment: Optional[str], deployed: bool) -> bool:
    """The show-once fallback (I-13, SEC-15): the sign-in link (never a password) goes back to the
    inviting admin only on a local development setup whose email delivery failed. Refused in
    production and on any deployed environment."""
    return not email_sent and (environment or "").strip().lower() in _LOCAL_ENVIRONMENTS and not deployed


def show_once_link(email: str, magic_login_link: str, email_sent: bool) -> Optional[Dict[str, str]]:
    """The show-once payload for a response, or None (always None in production / deployed)."""
    if settings.is_production or not may_show_link_once(email_sent, settings.ENVIRONMENT, is_deployed()):
        return None
    return {"email": email, "magic_login_link": magic_login_link}


# ── Database helpers ───────────────────────────────────────────────

def reinvitable(db: Session, user: User) -> bool:
    """
    An account that never joined can be invited again: still INVITED (reissue), or
    deactivated while invited (revoked invitation). Anyone who joined keeps the 409.
    """
    if user.status == "INVITED":
        return True
    if user.status != "INACTIVE" or user.last_login_at is not None:
        return False
    joined = db.query(Invitation.invitation_id).filter(
        Invitation.tenant_id == user.tenant_id, Invitation.email == user.email,
        Invitation.status == "ACCEPTED").first()
    invited = db.query(Invitation.invitation_id).filter(
        Invitation.tenant_id == user.tenant_id, Invitation.email == user.email).first()
    return joined is None and invited is not None


def _deliver(email: str, role: str, magic_login_link: str, tenant_name: str, inviter: User) -> Dict[str, Any]:
    """Send the invitation email; a delivery failure never fails the invitation itself."""
    inviter_name = f"{inviter.first_name or ''} {inviter.last_name or ''}".strip() or inviter.email
    delivery: Dict[str, Any] = {"attempted": True, "sent": False, "ses_message_id": None, "error": None}
    try:
        delivery.update(invitation_email.send_invitation_email(
            to_email=email,
            magic_login_link=magic_login_link,
            role=role,
            tenant_name=tenant_name,
            inviter_name=inviter_name,
        ))
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        delivery["error"] = "Invitation email could not be sent"
        logger.error("Invitation email failed for %s: %s", email, exc)
    except Exception:  # noqa: BLE001 - delivery boundary: the invite is committed, report and carry on
        delivery["error"] = "Invitation email could not be sent"
        logger.exception("Unexpected invitation email failure for %s", email)

    if delivery.get("sent"):
        logger.info("Invitation email sent: %s as %s by %s", email, role, inviter.email)
    else:
        logger.warning("Invitation created but email not sent: %s as %s by %s. Reason: %s",
                       email, role, inviter.email, delivery.get("error"))
    return delivery


def issue_invite(db: Session, request: Request | None, current_user: User, email: str, role: str,
                 existing: User | None, keep_permissions: bool = False) -> Dict[str, Any]:
    """
    Create (or reissue on `existing`) the invited account with no usable password, a new
    set-password sign-in link and a PENDING invitation, then email the link. Callers have
    already checked the role and revoked the previous pending invitation / links. Used by
    invite and resend.
    """
    first_name, last_name = derive_name_from_email(email)

    if existing:
        user = existing
        user.password_hash = None  # no password until the invitee sets one (I-13)
        user.role = role
        if not keep_permissions:
            user.custom_permissions = None
        user.status = "INVITED"
        user.invited_by = current_user.user_id
    else:
        user = User(
            tenant_id=current_user.tenant_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            password_hash=None,
            role=role,
            auth_provider="local",
            email_verified=True,
            status="INVITED",
            invited_by=current_user.user_id,
        )
        db.add(user)
    db.flush()

    raw_magic_token = secrets.token_urlsafe(48)
    magic_login_link = invitation_email.build_magic_login_link(raw_magic_token, request)
    db.add(MagicLoginToken(
        user_id=user.user_id,
        token_hash=hash_token(raw_magic_token),
        expires_at=utcnow() + timedelta(hours=settings.MAGIC_LOGIN_EXPIRE_HOURS),
    ))

    # The invitation stays PENDING until the invitee first signs in (or uses
    # /accept-invite with the emailed link's token); revoking it while pending
    # deactivates the invited account.
    invitation = Invitation(
        tenant_id=current_user.tenant_id,
        invited_by=current_user.user_id,
        email=email,
        role=role,
        token=hash_token(raw_magic_token),
        status="PENDING",
        expires_at=utcnow() + timedelta(days=INVITATION_VALID_DAYS),
    )
    db.add(invitation)
    db.commit()

    tenant = db.query(Tenant).filter(Tenant.tenant_id == current_user.tenant_id).first()
    tenant_name = tenant.tenant_name if tenant and tenant.tenant_name else "your workspace"
    email_delivery = _deliver(email, role, magic_login_link, tenant_name, current_user)
    return {
        "invitation_id": invitation.invitation_id,
        "user_id": user.user_id,
        "email": email,
        "role": role,
        "invite_token": None,
        "expires_at": invitation.expires_at.isoformat(),
        "email_delivery": email_delivery,
        # Local development without email only: the sign-in link, shown once (never a password)
        "show_once_link": show_once_link(email, magic_login_link, bool(email_delivery.get("sent"))),
    }


# ── Endpoint operations ────────────────────────────────────────────

def invite_user(db: Session, request: Request, current_user: User, email: str, role: str) -> Dict[str, Any]:
    """Send an invitation to join the tenant workspace."""
    validate_role(role)
    require_can_grant_role(current_user, role)

    existing = db.query(User).filter(User.tenant_id == current_user.tenant_id, User.email == email).first()
    if existing and not reinvitable(db, existing):
        raise ConflictError("User with this email already exists in your workspace")
    if existing:
        # Invitation revoked / invited account deactivated before it was ever used:
        # reissue it on the same account instead of refusing (old links stop working)
        revoke_pending_invitations(db, existing)

    pending = db.query(Invitation).filter(
        Invitation.tenant_id == current_user.tenant_id,
        Invitation.email == email,
        Invitation.status == "PENDING",
    ).first()
    if pending:
        pending.status = "REVOKED"

    return issue_invite(db, request, current_user, email, role, existing)


def resend_invite(db: Session, request: Request, current_user: User, user_id: str) -> Dict[str, Any]:
    """New set-password sign-in link for a still-INVITED account (old links revoked), once a minute."""
    user = db.query(User).filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id,
                                 User.role != "PLATFORM_ADMIN").first()
    if not user:
        raise NotFoundError("User not found")
    if user.status != "INVITED":
        raise ConflictError("Only a pending invitation can be resent")
    require_can_grant_role(current_user, user.role)
    # Measured on the database clock (created_at is set by the database)
    since_last = db.query(func.min(func.timestampdiff(text("SECOND"), Invitation.created_at, func.now()))).filter(
        Invitation.tenant_id == user.tenant_id, Invitation.email == user.email).scalar()
    wait = resend_wait_seconds(None if since_last is None else int(since_last))
    if wait:
        raise TooManyRequests(f"The invitation was just sent. Try again in {wait} seconds.", retry_after=wait)
    revoke_pending_invitations(db, user)
    result = issue_invite(db, request, current_user, user.email, user.role, user, keep_permissions=True)
    result["resent"] = True
    return result


def _joined(user: User) -> Dict[str, Any]:
    return {
        "message": "Account created successfully",
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
    }


def accept_invite(db: Session, body: AcceptInviteRequest) -> Dict[str, Any]:
    """Accept an invitation: activate the invited account (or create it) with the chosen password."""
    invitation = db.query(Invitation).filter(
        Invitation.token == hash_token(body.token), Invitation.status == "PENDING").first()
    if not invitation:
        raise NotFoundError("Invalid or expired invitation")

    if invitation.expires_at < utcnow():
        invitation.status = "EXPIRED"
        db.commit()
        raise GoneError("Invitation has expired")

    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise BadRequestError("Password must be at least 8 characters")

    existing = db.query(User).filter(
        User.tenant_id == invitation.tenant_id, User.email == invitation.email).first()
    if existing and existing.status == "INVITED":
        # The account was created with the invitation: set the chosen name and password
        existing.first_name = body.first_name
        existing.last_name = body.last_name
        existing.password_hash = hash_password(body.password)
        existing.status = "ACTIVE"
        spend_magic_links(db, existing.user_id)
        invitation.status = "ACCEPTED"
        db.commit()
        logger.info("Invitation accepted: %s joined as %s", invitation.email, existing.role)
        return _joined(existing)
    if existing:
        invitation.status = "ACCEPTED"
        db.commit()
        raise ConflictError("User already exists in this workspace")

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
    logger.info("Invitation accepted: %s joined as %s", invitation.email, invitation.role)
    return _joined(user)


def list_invitations(db: Session, current_user: User) -> List[Dict[str, Any]]:
    """All invitations of the tenant, newest first; overdue PENDING ones are marked EXPIRED."""
    invitations = db.query(Invitation).filter(
        Invitation.tenant_id == current_user.tenant_id).order_by(Invitation.created_at.desc()).all()
    now = utcnow()
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


def revoke_invitation(db: Session, current_user: User, invitation_id: str) -> None:
    """Revoke a pending invitation; the invited account becomes INACTIVE."""
    invitation = db.query(Invitation).filter(
        Invitation.invitation_id == invitation_id,
        Invitation.tenant_id == current_user.tenant_id,
    ).first()
    if not invitation or invitation.status == "REVOKED":
        # A revoked invitation is gone: a repeated DELETE is a 404
        raise NotFoundError("Invitation not found")
    if invitation.status != "PENDING":
        raise BadRequestError(f"Cannot revoke invitation with status: {invitation.status}")

    invitation.status = "REVOKED"
    # The invited account was created up front: it must not be usable once the invite is revoked
    invited = db.query(User).filter(
        User.tenant_id == invitation.tenant_id, User.email == invitation.email, User.status == "INVITED"
    ).first()
    if invited:
        invited.status = "INACTIVE"
        revoke_pending_invitations(db, invited)
    db.commit()
    logger.info("Invitation revoked: %s by %s", invitation.email, current_user.email)
