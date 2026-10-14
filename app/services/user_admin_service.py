# app/services/user_admin_service.py
"""
Team management: roles, feature permissions, deactivate / reactivate and permanent delete.

Role defaults come from app/core/auth.py (ROLE_DEFAULT_PERMISSIONS), which enforces them. Errors are domain errors (app/core/errors.py).
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.auth import ROLE_DEFAULT_PERMISSIONS
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.models.audit import AuditLog
from app.models.campaign import Campaign
from app.models.campaign_group import CampaignGroup
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.invitation import Invitation
from app.models.magic_login_token import MagicLoginToken
from app.models.password_reset_token import PasswordResetToken
from app.models.prospect_list import ProspectList
from app.models.refresh_token import RefreshToken
from app.models.user import User

logger = logging.getLogger(__name__)

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
    role: list(perms) for role, perms in ROLE_DEFAULT_PERMISSIONS.items()
}

# Records that keep a non-nullable reference to their creator: a user who has any of them
# cannot be deleted permanently (model, owner column, message)
_BLOCKING_RECORDS = (
    (Campaign, Campaign.created_by,
     "Cannot permanently delete user: they have created campaigns. Remove or reassign those campaigns first."),
    (ProspectList, ProspectList.uploaded_by,
     "Cannot permanently delete user: they have uploaded prospect lists. Remove those lists first."),
    (CampaignGroup, CampaignGroup.owner_id,
     "Cannot permanently delete user: they own campaign groups. Remove those groups first."),
    (EmailTemplateVersion, EmailTemplateVersion.created_by,
     "Cannot permanently delete user: they have created email templates. Remove those templates first."),
)


# ── Pure rules ─────────────────────────────────────────────────────

def default_permissions(role: Optional[str]) -> List[str]:
    return DEFAULT_PERMISSIONS.get(role, []) if role is not None else []


def effective_permissions(user: User) -> List[str]:
    """Custom permissions when set, else the role's defaults."""
    return user.custom_permissions if user.custom_permissions is not None else default_permissions(user.role)


def validate_role(role: str) -> None:
    if role not in VALID_ROLES:
        raise BadRequestError(f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}")


def require_can_grant_role(actor: User, role: Optional[str]) -> None:
    """Only a Super Admin can invite (or re-invite) another Super Admin."""
    if role == "SUPER_ADMIN" and actor.role != "SUPER_ADMIN":
        raise PermissionDenied("Only a Super Admin can invite another Super Admin")


def unknown_permissions(permissions: List[str]) -> set:
    return set(permissions) - ALL_PERMISSIONS


# ── Lookups ────────────────────────────────────────────────────────

def get_member(db: Session, actor: User, user_id: str) -> User:
    """A user of the actor's tenant, else NotFoundError."""
    target = db.query(User).filter(User.user_id == user_id, User.tenant_id == actor.tenant_id).first()
    if not target:
        raise NotFoundError("User not found")
    return target


def list_members(db: Session, actor: User) -> List[User]:
    """Team members of the tenant (not platform admins), newest first."""
    return db.query(User).filter(
        User.tenant_id == actor.tenant_id,
        User.role != "PLATFORM_ADMIN",
    ).order_by(User.created_at.desc()).all()


def spend_magic_links(db: Session, user_id: str) -> None:
    """Mark the user's unused one-click sign-in links as used."""
    db.query(MagicLoginToken).filter(
        MagicLoginToken.user_id == user_id, MagicLoginToken.used_at.is_(None)
    ).update({MagicLoginToken.used_at: datetime.utcnow()}, synchronize_session=False)


def revoke_pending_invitations(db: Session, user: User) -> None:
    """The invited account is deactivated: close its open invitation and unused sign-in links."""
    db.query(Invitation).filter(
        Invitation.tenant_id == user.tenant_id,
        Invitation.email == user.email,
        Invitation.status == "PENDING",
    ).update({Invitation.status: "REVOKED"}, synchronize_session=False)
    spend_magic_links(db, user.user_id)


# ── Endpoint operations ────────────────────────────────────────────

def change_role(db: Session, actor: User, user_id: str, role: str) -> None:
    validate_role(role)
    if user_id == actor.user_id:
        raise BadRequestError("Cannot change your own role")
    target = get_member(db, actor, user_id)
    if target.role == "SUPER_ADMIN":
        raise BadRequestError("Cannot change the Super Admin's role")
    old_role = target.role
    target.role = role
    db.commit()
    logger.info("Role changed: %s %s → %s by %s", target.email, old_role, role, actor.email)


def deactivate(db: Session, actor: User, user_id: str) -> None:
    """Set a member INACTIVE; an invited member's pending invitation and links are revoked."""
    if user_id == actor.user_id:
        raise BadRequestError("Cannot deactivate yourself")
    target = get_member(db, actor, user_id)
    if target.role == "SUPER_ADMIN":
        raise BadRequestError("Cannot deactivate the workspace Super Admin")
    # ADMIN can only deactivate MANAGERs, not other ADMINs
    if actor.role == "ADMIN" and target.role == "ADMIN":
        raise PermissionDenied("Admins cannot deactivate other admins")
    if target.status == "INACTIVE":
        # Already deactivated: a repeated DELETE finds nothing to delete
        raise NotFoundError("User not found")

    prev_status = target.status
    target.status = "INACTIVE"
    if prev_status == "INVITED":
        revoke_pending_invitations(db, target)
    db.commit()
    action = "revoked invite for" if prev_status == "INVITED" else "deactivated"
    logger.info("User %s: %s by %s", action, target.email, actor.email)


def reactivate(db: Session, actor: User, user_id: str) -> None:
    if user_id == actor.user_id:
        raise BadRequestError("Cannot reactivate yourself")
    target = get_member(db, actor, user_id)
    if target.status == "ACTIVE":
        raise BadRequestError("User is already active")
    if target.status == "INVITED":
        # Activating an invited user would bypass the invitation (and leave its links live)
        raise BadRequestError("User has a pending invitation; resend the invitation instead")
    if target.role == "SUPER_ADMIN" and actor.role != "SUPER_ADMIN":
        raise PermissionDenied("Only a Super Admin can reactivate a Super Admin")
    if actor.role == "ADMIN" and target.role == "ADMIN":
        raise PermissionDenied("Admins cannot reactivate other admins")
    target.status = "ACTIVE"
    db.commit()
    logger.info("User reactivated: %s by %s", target.email, actor.email)


def delete_permanently(db: Session, actor: User, user_id: str) -> None:
    """Delete a deactivated member who owns no business data; the audit trail is kept."""
    if user_id == actor.user_id:
        raise BadRequestError("Cannot delete yourself")
    target = get_member(db, actor, user_id)
    if target.role == "SUPER_ADMIN":
        raise BadRequestError("Cannot delete the workspace Super Admin")
    if target.status != "INACTIVE":
        raise BadRequestError("User must be deactivated before permanent deletion")

    # Block deletion if user has created business data (non-nullable FK constraints)
    for model, owner_column, message in _BLOCKING_RECORDS:
        if db.query(model).filter(owner_column == user_id).first():
            raise ConflictError(message)

    email = target.email
    # Token/session records first (safe to always delete)
    for model in (MagicLoginToken, RefreshToken, PasswordResetToken):
        db.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
    # Invitations this user sent (invited_by is NOT NULL so can't null it out)
    db.query(Invitation).filter(Invitation.invited_by == user_id).delete(synchronize_session=False)
    # Keep the audit trail (SEC-16): detach it from the deleted user instead of deleting it
    db.query(AuditLog).filter(AuditLog.user_id == user_id).update(
        {AuditLog.user_id: None}, synchronize_session=False)
    # Nullable references: template approvals and users this person invited
    db.query(EmailTemplate).filter(EmailTemplate.approved_by == user_id).update(
        {EmailTemplate.approved_by: None}, synchronize_session=False)
    db.query(User).filter(User.invited_by == user_id).update(
        {User.invited_by: None}, synchronize_session=False)

    db.delete(target)
    db.commit()
    logger.info("User permanently deleted: %s by %s", email, actor.email)


def permissions_view(db: Session, actor: User, user_id: str) -> Dict[str, Any]:
    target = get_member(db, actor, user_id)
    return {
        "user_id": user_id,
        "role": target.role,
        "permissions": effective_permissions(target),
        "is_custom": target.custom_permissions is not None,
        "defaults": default_permissions(target.role),
    }


def update_permissions(db: Session, actor: User, user_id: str, permissions: List[str]) -> List[str]:
    """Set custom permissions; returns what was stored."""
    if user_id == actor.user_id:
        raise BadRequestError("Cannot change your own permissions")
    target = get_member(db, actor, user_id)
    if target.role == "SUPER_ADMIN":
        raise BadRequestError("Cannot restrict Super Admin permissions")
    invalid = unknown_permissions(permissions)
    if invalid:
        raise BadRequestError(f"Unknown permission keys: {', '.join(invalid)}")
    target.custom_permissions = list(set(permissions))
    db.commit()
    logger.info("Permissions updated for %s by %s: %s", target.email, actor.email, target.custom_permissions)
    return target.custom_permissions


def reset_permissions(db: Session, actor: User, user_id: str) -> List[str]:
    """Back to the role's defaults; returns them."""
    target = get_member(db, actor, user_id)
    target.custom_permissions = None
    db.commit()
    logger.info("Permissions reset to defaults for %s by %s", target.email, actor.email)
    return default_permissions(target.role)
