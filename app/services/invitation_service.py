# app/services/invitation_service.py
"""
Workspace invitations: invite, resend, accept, list and revoke.

An invitation creates the INVITED account up front WITHOUT a usable password and emails a
one-time set-password (sign-in) link; the invitee chooses their own password on first use.
No password is ever generated, emailed or returned in an API response (rule I-13). The
invitation stays PENDING until the invitee first signs in or accepts. Revoking it
deactivates the invited account. Errors are domain errors.

``InvitationService`` serves the invitation routes of app/routers/user_router.py: one unit of work per
call over the identity repositories (app/repositories/identity*.py); the pure rules are in
app/domain/identity.py.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

from app.core.clock import iso_utc, utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, GoneError, NotFoundError, TooManyRequests
from app.core.secrets_guard import is_deployed
from app.core.security import hash_password, hash_token
from app.domain import identity as rules
from app.domain.identity import (
    INVITATION_VALID_DAYS,
    MIN_PASSWORD_LENGTH,
    RESEND_COOLDOWN_SECONDS,
    InvitationStatus,
    UserStatus,
    derive_name_from_email,
    may_show_link_once,
    password_too_short,
    resend_wait_seconds,
)
from app.models.invitation import Invitation
from app.models.user import User
from app.repositories.identity import DuplicateAccount, IdentityAccess, IdentityUnitOfWork, Session, identity_uow
from app.services import invitation_email
from app.services.identity_common import identity_rules
from app.services.invitation_email import HeaderSource
from app.services.user_admin_service import require_can_grant_role, revoke_pending_invitations, validate_role
from app.services.user_lookup import LIVE_ACCOUNT_CONFLICT, live_account_conflict, other_live_account

__all__ = ["INVITATION_VALID_DAYS", "InvitationService", "MIN_PASSWORD_LENGTH", "RESEND_COOLDOWN_SECONDS",
           "derive_name_from_email", "may_show_link_once", "require_no_other_live_account", "resend_wait_seconds",
           "show_once_link"]

logger = logging.getLogger(__name__)


def show_once_link(email: str, magic_login_link: str, email_sent: bool) -> Optional[Dict[str, str]]:
    """The show-once payload for a response, or None (always None in production / deployed)."""
    if settings.is_production or not may_show_link_once(email_sent, settings.ENVIRONMENT, is_deployed()):
        return None
    return {"email": email, "magic_login_link": magic_login_link}


def require_no_other_live_account(db: Any, email: str, exclude_user_id: Optional[str] = None) -> None:
    """409 when the address already has an ACTIVE / INVITED account elsewhere (F017). Legacy session form."""
    if live_account_conflict(db, email, exclude_user_id):
        raise ConflictError(LIVE_ACCOUNT_CONFLICT)


def _no_other_live_account(access: IdentityAccess, email: str, exclude_user_id: Optional[str] = None) -> None:
    if other_live_account(access.users, email, exclude_user_id):
        raise ConflictError(LIVE_ACCOUNT_CONFLICT)


def _flush_live_account(access: IdentityAccess) -> None:
    """Write an account that becomes ACTIVE / INVITED. The unique users.active_email index (one live
    account per email across workspaces, F017) or a concurrent invite turns into a 409, not a 500."""
    try:
        access.flush_account()
    except DuplicateAccount as exc:
        raise ConflictError(LIVE_ACCOUNT_CONFLICT) from exc


def _reinvitable(access: IdentityAccess, user: User) -> bool:
    return rules.reinvitable(user.status, user.last_login_at,
                             lambda: access.invitations.history(user.tenant_id, user.email))


def _deliver(email: str, role: str, magic_login_link: str, tenant_name: str, inviter: Any) -> Dict[str, Any]:
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


def _invited_account(access: IdentityAccess, actor: Any, email: str, role: str, existing: Optional[User],
                     keep_permissions: bool) -> User:
    """The INVITED account with no usable password (I-13): reissued on ``existing`` or created."""
    if existing:
        existing.password_hash = None  # no password until the invitee sets one (I-13)
        existing.role = role
        if not keep_permissions:
            existing.custom_permissions = None
        existing.status = UserStatus.INVITED
        existing.invited_by = actor.user_id
        user = existing
    else:
        first_name, last_name = derive_name_from_email(email)
        user = access.users.add(
            tenant_id=actor.tenant_id, first_name=first_name, last_name=last_name, email=email,
            password_hash=None, role=role, auth_provider="local", email_verified=True,
            status=UserStatus.INVITED, invited_by=actor.user_id,
        )
    _flush_live_account(access)
    return user


def issue_invite(uow: IdentityUnitOfWork, request: Optional[HeaderSource], actor: Any, email: str, role: str,
                 existing: Optional[User], keep_permissions: bool = False) -> Dict[str, Any]:
    """
    Create (or reissue on `existing`) the invited account with no usable password, a new
    set-password sign-in link and a PENDING invitation, commit, then email the link. Callers have
    already checked the role and revoked the previous pending invitation / links. Used by
    invite and resend.
    """
    user = _invited_account(uow, actor, email, role, existing, keep_permissions)

    raw_magic_token = secrets.token_urlsafe(48)
    magic_login_link = invitation_email.build_magic_login_link(raw_magic_token, request)
    uow.tokens.add_magic_link(user.user_id, hash_token(raw_magic_token),
                              utcnow() + timedelta(hours=settings.MAGIC_LOGIN_EXPIRE_HOURS))

    # The invitation stays PENDING until the invitee first signs in (or uses
    # /accept-invite with the emailed link's token); revoking it while pending
    # deactivates the invited account.
    invitation = uow.invitations.add(
        tenant_id=actor.tenant_id,
        invited_by=actor.user_id,
        email=email,
        role=role,
        token=hash_token(raw_magic_token),
        status=InvitationStatus.PENDING,
        expires_at=utcnow() + timedelta(days=INVITATION_VALID_DAYS),
    )
    uow.commit()

    tenant = uow.tenants.get(actor.tenant_id)
    tenant_name = tenant.tenant_name if tenant and tenant.tenant_name else "your workspace"
    email_delivery = _deliver(email, role, magic_login_link, tenant_name, actor)
    return {
        "invitation_id": invitation.invitation_id,
        "user_id": user.user_id,
        "email": email,
        "role": role,
        "invite_token": None,
        "expires_at": iso_utc(invitation.expires_at),
        "email_delivery": email_delivery,
        # Local development without email only: the sign-in link, shown once (never a password)
        "show_once_link": show_once_link(email, magic_login_link, bool(email_delivery.get("sent"))),
    }


def _revocable(invitation: Optional[Invitation]) -> Invitation:
    """The invitation if it can be revoked (PENDING), else the refusal."""
    with identity_rules():
        rules.check_revocable(invitation is not None, invitation.status if invitation else None)
    if invitation is None:  # check_revocable has refused it already
        raise NotFoundError("Invitation not found")
    return invitation


def _joined(user: User) -> Dict[str, Any]:
    return {
        "message": "Account created successfully",
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
    }


def _invitation_out(inv: Invitation) -> Dict[str, Any]:
    return {
        "invitation_id": inv.invitation_id,
        "email": inv.email,
        "role": inv.role,
        "status": inv.status,
        "expires_at": iso_utc(inv.expires_at),
        "created_at": iso_utc(inv.created_at),
    }


class InvitationService:
    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> InvitationService:
        return cls(identity_uow(db))

    def invite(self, actor: Any, email: str, role: str, request: Optional[HeaderSource]) -> Dict[str, Any]:
        """Send an invitation to join the tenant workspace."""
        validate_role(role)
        require_can_grant_role(actor, role)
        with self.uow as uow:
            existing = uow.users.in_tenant_with_email(actor.tenant_id, email)
            if existing and not _reinvitable(uow, existing):
                raise ConflictError("User with this email already exists in your workspace")
            _no_other_live_account(uow, email, existing.user_id if existing else None)
            if existing:
                # Invitation revoked / invited account deactivated before it was ever used:
                # reissue it on the same account instead of refusing (old links stop working)
                revoke_pending_invitations(uow, existing)
            pending = uow.invitations.pending_for(actor.tenant_id, email)
            if pending:
                pending.status = InvitationStatus.REVOKED
            return issue_invite(uow, request, actor, email, role, existing)

    def resend(self, actor: Any, user_id: str, request: Optional[HeaderSource]) -> Dict[str, Any]:
        """New set-password sign-in link for a still-INVITED account (old links revoked), once a minute."""
        with self.uow as uow:
            user = uow.users.team_member(actor.tenant_id, user_id)
            if not user:
                raise NotFoundError("User not found")
            with identity_rules():
                rules.check_resend(actor.role, user)
            # Measured on the database clock (created_at is set by the database)
            wait = resend_wait_seconds(uow.invitations.seconds_since_last(user.tenant_id, user.email))
            if wait:
                raise TooManyRequests(f"The invitation was just sent. Try again in {wait} seconds.", retry_after=wait)
            revoke_pending_invitations(uow, user)
            result = issue_invite(uow, request, actor, user.email, user.role, user, keep_permissions=True)
        result["resent"] = True
        return result

    def accept(self, token: str, first_name: str, last_name: str, password: str) -> Dict[str, Any]:
        """Accept an invitation: activate the invited account (or create it) with the chosen password."""
        with self.uow as uow:
            invitation = uow.invitations.pending_by_token(hash_token(token))
            if not invitation:
                raise NotFoundError("Invalid or expired invitation")
            if invitation.expires_at < utcnow():
                invitation.status = InvitationStatus.EXPIRED
                uow.commit()
                raise GoneError("Invitation has expired")
            if password_too_short(password):
                raise BadRequestError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

            existing = uow.users.in_tenant_with_email(invitation.tenant_id, invitation.email)
            if existing and existing.status == UserStatus.INVITED:
                return self._activate_invited(uow, invitation, existing, first_name, last_name, password)
            if existing:
                invitation.status = InvitationStatus.ACCEPTED
                uow.commit()
                raise ConflictError("User already exists in this workspace")
            _no_other_live_account(uow, invitation.email)
            user = uow.users.add(
                tenant_id=invitation.tenant_id, first_name=first_name, last_name=last_name, email=invitation.email,
                password_hash=hash_password(password), role=invitation.role, auth_provider="local",
                email_verified=True, invited_by=invitation.invited_by,
            )
            invitation.status = InvitationStatus.ACCEPTED
            _flush_live_account(uow)
            uow.commit()
            logger.info("Invitation accepted: %s joined as %s", invitation.email, invitation.role)
            return _joined(user)

    @staticmethod
    def _activate_invited(uow: IdentityUnitOfWork, invitation: Invitation, existing: User, first_name: str,
                          last_name: str, password: str) -> Dict[str, Any]:
        """The account was created with the invitation: set the chosen name and password."""
        # Older data may already hold a live account elsewhere: never make it a second ACTIVE one
        _no_other_live_account(uow, existing.email, existing.user_id)
        existing.first_name = first_name
        existing.last_name = last_name
        existing.password_hash = hash_password(password)
        existing.status = UserStatus.ACTIVE
        uow.tokens.spend_magic_links(existing.user_id, utcnow())
        invitation.status = InvitationStatus.ACCEPTED
        uow.commit()
        logger.info("Invitation accepted: %s joined as %s", invitation.email, existing.role)
        return _joined(existing)

    def list(self, actor: Any) -> List[Dict[str, Any]]:
        """All invitations of the tenant, newest first; overdue PENDING ones are marked EXPIRED."""
        with self.uow as uow:
            invitations = uow.invitations.for_tenant(actor.tenant_id)
            now = utcnow()
            for inv in invitations:
                if rules.is_overdue(inv.status, inv.expires_at, now):
                    inv.status = InvitationStatus.EXPIRED
            uow.commit()
            return [_invitation_out(inv) for inv in invitations]

    def revoke(self, actor: Any, invitation_id: str) -> None:
        """Revoke a pending invitation; the invited account becomes INACTIVE."""
        with self.uow as uow:
            invitation = _revocable(uow.invitations.get(actor.tenant_id, invitation_id))
            invitation.status = InvitationStatus.REVOKED
            # The invited account was created up front: it must not be usable once the invite is revoked
            invited = uow.users.in_tenant_with_email(invitation.tenant_id, invitation.email, UserStatus.INVITED)
            if invited:
                invited.status = UserStatus.INACTIVE
                revoke_pending_invitations(uow, invited)
            uow.commit()
            logger.info("Invitation revoked: %s by %s", invitation.email, actor.email)
