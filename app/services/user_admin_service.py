# app/services/user_admin_service.py
"""
Team management: roles, feature permissions, deactivate / reactivate and permanent delete.

``TeamService`` serves app/routers/user_router.py: one unit of work per call over the identity repositories
(app/repositories/identity.py). The rules (who may change, deactivate, reactivate or delete whom; role
defaults from ROLE_DEFAULT_PERMISSIONS) are pure functions in app/domain/identity.py; this module applies
them and reports refusals as domain errors (app/core/errors.py). The module-level rule helpers below are
kept for existing callers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core.clock import utcnow
from app.core.errors import ConflictError, NotFoundError
from app.domain import identity as rules
from app.domain.identity import (
    ALL_PERMISSIONS,
    BLOCKING_RECORD_MESSAGES,
    DEFAULT_PERMISSIONS,
    TENANT_ROLES,
    UserStatus,
    default_permissions,
    unknown_permissions,
)
from app.models.user import User
from app.repositories.identity import DuplicateAccount, IdentityAccess, IdentityUnitOfWork, Session, identity_uow
from app.services.identity_common import identity_rules
from app.services.user_lookup import LIVE_ACCOUNT_CONFLICT, other_live_account

__all__ = ["ALL_PERMISSIONS", "DEFAULT_PERMISSIONS", "TeamService", "VALID_ROLES", "default_permissions",
           "effective_permissions", "require_can_grant_role", "revoke_pending_invitations", "unknown_permissions",
           "validate_role"]

logger = logging.getLogger(__name__)

VALID_ROLES = set(TENANT_ROLES)


# ── Rule helpers (domain errors) ───────────────────────────────────

def effective_permissions(user: Any) -> List[str]:
    """Custom permissions when set, else the role's defaults."""
    return rules.effective_permissions(user.role, user.custom_permissions)


def validate_role(role: str) -> None:
    with identity_rules():
        rules.check_role(role)


def require_can_grant_role(actor: Any, role: Optional[str]) -> None:
    """Only a Super Admin can invite (or re-invite) another Super Admin."""
    with identity_rules():
        rules.check_can_grant_role(actor.role, role)


def _not_self(actor: Any, user_id: str, message: str) -> None:
    with identity_rules():
        rules.check_not_self(actor.user_id, user_id, message)


def revoke_pending_invitations(access: IdentityAccess, user: Any) -> None:
    """The invited account is deactivated (or re-invited): close its open invitation and unused sign-in links."""
    access.invitations.revoke_pending(user.tenant_id, user.email)
    access.tokens.spend_magic_links(user.user_id, utcnow())


def get_member(access: IdentityAccess, actor: Any, user_id: str) -> User:
    """A user of the actor's tenant, else NotFoundError."""
    target = access.users.member(actor.tenant_id, user_id)
    if not target:
        raise NotFoundError("User not found")
    return target


# ── Endpoint operations ────────────────────────────────────────────

class TeamService:
    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> TeamService:
        return cls(identity_uow(db))

    def list_members(self, actor: Any) -> List[User]:
        """Team members of the tenant (not platform admins), newest first."""
        with self.uow as uow:
            return uow.users.members(actor.tenant_id)

    def change_role(self, actor: Any, user_id: str, role: str) -> None:
        validate_role(role)
        _not_self(actor, user_id, "Cannot change your own role")
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            with identity_rules():
                rules.check_role_change(target)
            old_role = target.role
            target.role = role
            uow.commit()
            logger.info("Role changed: %s %s → %s by %s", target.email, old_role, role, actor.email)

    def deactivate(self, actor: Any, user_id: str) -> None:
        """Set a member INACTIVE; an invited member's pending invitation and links are revoked."""
        _not_self(actor, user_id, "Cannot deactivate yourself")
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            with identity_rules():
                rules.check_deactivate(actor, target)
            prev_status = target.status
            target.status = UserStatus.INACTIVE
            if prev_status == UserStatus.INVITED:
                revoke_pending_invitations(uow, target)
            uow.commit()
            action = "revoked invite for" if prev_status == UserStatus.INVITED else "deactivated"
            logger.info("User %s: %s by %s", action, target.email, actor.email)

    def reactivate(self, actor: Any, user_id: str) -> None:
        _not_self(actor, user_id, "Cannot reactivate yourself")
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            with identity_rules():
                rules.check_reactivate(actor, target)
            # One live account per email across workspaces (F017)
            if other_live_account(uow.users, target.email, target.user_id):
                raise ConflictError(LIVE_ACCOUNT_CONFLICT)
            target.status = UserStatus.ACTIVE
            try:
                uow.commit_account()
            except DuplicateAccount as exc:  # a concurrent invite / reactivation won users.active_email
                raise ConflictError(LIVE_ACCOUNT_CONFLICT) from exc
            logger.info("User reactivated: %s by %s", target.email, actor.email)

    def delete_permanently(self, actor: Any, user_id: str) -> None:
        """Delete a deactivated member who owns no business data; the audit trail is kept."""
        _not_self(actor, user_id, "Cannot delete yourself")
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            with identity_rules():
                rules.check_permanent_delete(target)
            # Block deletion if user has created business data (non-nullable FK constraints)
            blocking = uow.users.blocking_record(user_id)
            if blocking:
                raise ConflictError(BLOCKING_RECORD_MESSAGES[blocking])
            email = target.email
            uow.users.purge(target)
            uow.commit()
            logger.info("User permanently deleted: %s by %s", email, actor.email)

    def permissions_view(self, actor: Any, user_id: str) -> Dict[str, Any]:
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            return {
                "user_id": user_id,
                "role": target.role,
                "permissions": effective_permissions(target),
                "is_custom": target.custom_permissions is not None,
                "defaults": default_permissions(target.role),
            }

    def update_permissions(self, actor: Any, user_id: str, permissions: List[str]) -> List[str]:
        """Set custom permissions; returns what was stored."""
        _not_self(actor, user_id, "Cannot change your own permissions")
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            with identity_rules():
                rules.check_permission_update(target, permissions)
            target.custom_permissions = list(set(permissions))
            uow.commit()
            logger.info("Permissions updated for %s by %s: %s", target.email, actor.email, target.custom_permissions)
            return target.custom_permissions

    def reset_permissions(self, actor: Any, user_id: str) -> List[str]:
        """Back to the role's defaults; returns them."""
        with self.uow as uow:
            target = get_member(uow, actor, user_id)
            target.custom_permissions = None
            uow.commit()
            logger.info("Permissions reset to defaults for %s by %s", target.email, actor.email)
            return default_permissions(target.role)
