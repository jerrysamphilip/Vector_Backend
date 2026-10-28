"""
Identity aggregate (ADR 0009, chunk A5): roles, feature permissions, user / invitation / tenant states and
the pure rules of team management (who may invite, change, deactivate, reactivate or delete whom).

No I/O. Rules take plain values or any object with ``user_id`` / ``role`` / ``status`` (an ORM ``User``
or a test double) and raise ``IdentityRuleError`` (a ``ValueError``) carrying the HTTP status the API
has always answered with; the services turn it into an ``app.core.errors`` domain error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

# ── Roles ──────────────────────────────────────────────────────────

PLATFORM_ADMIN = "PLATFORM_ADMIN"
SUPER_ADMIN = "SUPER_ADMIN"
ADMIN = "ADMIN"
MANAGER = "MANAGER"
AGENT = "AGENT"

# Roles a workspace member can hold (PLATFORM_ADMIN lives outside every workspace)
TENANT_ROLES = frozenset({SUPER_ADMIN, ADMIN, MANAGER, AGENT})

# Default feature permissions per role: the single source of truth. app.core.auth.require_permission
# enforces them, /auth/me and the user admin API report them (SUPER_ADMIN always passes the check).
ROLE_DEFAULT_PERMISSIONS: dict[str, tuple[str, ...]] = {
    PLATFORM_ADMIN: ("manage_platform",),          # platform-only
    SUPER_ADMIN:    ("manage_campaigns", "manage_templates", "manage_inboxes",
                     "manage_prospects", "view_analytics", "export_data", "manage_team"),
    ADMIN:          ("manage_campaigns", "manage_templates", "manage_inboxes",
                     "manage_prospects", "view_analytics", "export_data", "manage_team"),
    MANAGER:        ("manage_campaigns", "manage_templates", "view_analytics"),
    AGENT:          ("manage_campaigns",),
}

# Every feature permission a workspace member can be granted
ALL_PERMISSIONS = frozenset({
    "manage_campaigns",   # Create / edit / delete campaigns
    "manage_templates",   # Create / edit email templates
    "manage_inboxes",     # Connect and configure email inboxes
    "manage_prospects",   # Import and manage prospect lists
    "view_analytics",     # View reports and analytics dashboard
    "export_data",        # Export campaigns, contacts and reports
    "manage_team",        # Invite and manage team members
})

# The defaults as lists (the shape the API returns)
DEFAULT_PERMISSIONS: dict[str, list[str]] = {role: list(perms) for role, perms in ROLE_DEFAULT_PERMISSIONS.items()}


def default_permissions(role: Optional[str]) -> list[str]:
    return DEFAULT_PERMISSIONS.get(role, []) if role is not None else []


def effective_permissions(role: Optional[str], custom: Optional[Sequence[str]]) -> list[str]:
    """Custom permissions when set (also an empty list), else the role's defaults."""
    return list(custom) if custom is not None else default_permissions(role)


def unknown_permissions(permissions: Iterable[str]) -> set[str]:
    return set(permissions) - ALL_PERMISSIONS


# ── States ─────────────────────────────────────────────────────────

class UserStatus:
    ACTIVE = "ACTIVE"
    INVITED = "INVITED"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"


# One live (ACTIVE or INVITED) account per email across every workspace (F017)
LIVE_STATUSES = (UserStatus.ACTIVE, UserStatus.INVITED)
# Accounts that may complete a first sign-in factor (an INVITED one only through its emailed link)
SIGN_IN_STATUSES = LIVE_STATUSES


class InvitationStatus:
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


# Invitations the invitee's first sign-in accepts
OPEN_INVITATION_STATUSES = (InvitationStatus.PENDING, InvitationStatus.EXPIRED)


class TenantStatus:
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    INACTIVE = "INACTIVE"
    DELETED = "DELETED"


# Tenants in these states cannot sign in or call the API (PLATFORM_ADMIN has no tenant and is unaffected)
BLOCKED_TENANT_STATUSES = frozenset({TenantStatus.SUSPENDED, TenantStatus.INACTIVE, TenantStatus.DELETED})
# What a platform admin may set a tenant to
SETTABLE_TENANT_STATUSES = (TenantStatus.ACTIVE, TenantStatus.SUSPENDED)


def tenant_blocked(status: Optional[str]) -> bool:
    """A missing status counts as ACTIVE."""
    return (status or TenantStatus.ACTIVE).upper() in BLOCKED_TENANT_STATUSES


# ── Errors ─────────────────────────────────────────────────────────

class IdentityRuleError(ValueError):
    """A request the identity rules refuse; ``status`` is the HTTP status the API has always answered with."""

    status = 400

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


def _refuse(message: str, status: int = 400) -> IdentityRuleError:
    return IdentityRuleError(message, status)


# ── Team management rules ──────────────────────────────────────────

class Member(Protocol):
    """What the rules read from a user (an ORM ``User`` or a test double)."""

    @property
    def user_id(self) -> Any: ...

    @property
    def role(self) -> Any: ...

    @property
    def status(self) -> Any: ...


class Actor(Protocol):
    @property
    def user_id(self) -> Any: ...

    @property
    def role(self) -> Any: ...


MIN_PASSWORD_LENGTH = 8


def check_role(role: str) -> None:
    if role not in TENANT_ROLES:
        raise _refuse(f"Invalid role. Must be one of: {', '.join(sorted(TENANT_ROLES))}")


def check_can_grant_role(actor_role: Optional[str], role: Optional[str]) -> None:
    """Only a Super Admin can invite (or re-invite) another Super Admin."""
    if role == SUPER_ADMIN and actor_role != SUPER_ADMIN:
        raise _refuse("Only a Super Admin can invite another Super Admin", 403)


def check_not_self(actor_id: Any, user_id: Any, message: str) -> None:
    """Team actions never apply to the acting user (checked before the target is looked up)."""
    if user_id == actor_id:
        raise _refuse(message)


def check_role_change(target: Member) -> None:
    if target.role == SUPER_ADMIN:
        raise _refuse("Cannot change the Super Admin's role")


def check_deactivate(actor: Actor, target: Member) -> None:
    if target.role == SUPER_ADMIN:
        raise _refuse("Cannot deactivate the workspace Super Admin")
    # ADMIN can only deactivate MANAGERs / AGENTs, not other ADMINs
    if actor.role == ADMIN and target.role == ADMIN:
        raise _refuse("Admins cannot deactivate other admins", 403)
    if target.status == UserStatus.INACTIVE:
        # Already deactivated: a repeated DELETE finds nothing to delete
        raise _refuse("User not found", 404)


def check_reactivate(actor: Actor, target: Member) -> None:
    if target.status == UserStatus.ACTIVE:
        raise _refuse("User is already active")
    if target.status == UserStatus.INVITED:
        # Activating an invited user would bypass the invitation (and leave its links live)
        raise _refuse("User has a pending invitation; resend the invitation instead")
    if target.role == SUPER_ADMIN and actor.role != SUPER_ADMIN:
        raise _refuse("Only a Super Admin can reactivate a Super Admin", 403)
    if actor.role == ADMIN and target.role == ADMIN:
        raise _refuse("Admins cannot reactivate other admins", 403)


def check_permanent_delete(target: Member) -> None:
    if target.role == SUPER_ADMIN:
        raise _refuse("Cannot delete the workspace Super Admin")
    if target.status != UserStatus.INACTIVE:
        raise _refuse("User must be deactivated before permanent deletion")


def check_permission_update(target: Member, permissions: Iterable[str]) -> None:
    if target.role == SUPER_ADMIN:
        raise _refuse("Cannot restrict Super Admin permissions")
    invalid = unknown_permissions(permissions)
    if invalid:
        raise _refuse(f"Unknown permission keys: {', '.join(invalid)}")


# Records that keep a non-nullable reference to their creator: a user who has any of them cannot be
# deleted permanently (the repository reports which kind it found)
BLOCKING_RECORD_MESSAGES = {
    "campaigns": "Cannot permanently delete user: they have created campaigns. Remove or reassign those "
                 "campaigns first.",
    "prospect_lists": "Cannot permanently delete user: they have uploaded prospect lists. Remove those lists first.",
    "campaign_groups": "Cannot permanently delete user: they own campaign groups. Remove those groups first.",
    "template_versions": "Cannot permanently delete user: they have created email templates. Remove those "
                         "templates first.",
}
BLOCKING_RECORD_KINDS = tuple(BLOCKING_RECORD_MESSAGES)


# ── Invitations ────────────────────────────────────────────────────

RESEND_COOLDOWN_SECONDS = 60  # one resend per invited user per minute
INVITATION_VALID_DAYS = 7
_LOCAL_ENVIRONMENTS = ("development", "dev", "local")


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


@dataclass(frozen=True)
class InvitationHistory:
    """What the workspace's invitations say about one address."""

    invited: bool    # any invitation was ever sent
    accepted: bool   # one was accepted (the person joined)


def reinvitable(status: Optional[str], last_login_at: Optional[datetime],
                history: Callable[[], InvitationHistory]) -> bool:
    """An account that never joined can be invited again: still INVITED (reissue), or deactivated while
    invited (revoked invitation). Anyone who joined keeps the 409. ``history`` is only asked for a
    deactivated account that never signed in."""
    if status == UserStatus.INVITED:
        return True
    if status != UserStatus.INACTIVE or last_login_at is not None:
        return False
    past = history()
    return past.invited and not past.accepted


def check_resend(actor_role: Optional[str], target: Member) -> None:
    if target.status != UserStatus.INVITED:
        raise _refuse("Only a pending invitation can be resent", 409)
    check_can_grant_role(actor_role, target.role)


def check_revocable(found: bool, status: Optional[str]) -> None:
    """``found``: the invitation exists in the workspace; ``status``: its status."""
    if not found or status == InvitationStatus.REVOKED:
        # A revoked invitation is gone: a repeated DELETE is a 404
        raise _refuse("Invitation not found", 404)
    if status != InvitationStatus.PENDING:
        raise _refuse(f"Cannot revoke invitation with status: {status}")


def is_overdue(status: Optional[str], expires_at: Optional[datetime], now: datetime) -> bool:
    """A PENDING invitation past its expiry (listed as EXPIRED from then on)."""
    return status == InvitationStatus.PENDING and expires_at is not None and expires_at < now


# ── Sign-in ────────────────────────────────────────────────────────

SUSPENDED_ACCOUNT = "Account is suspended or inactive"
FINISH_JOINING = ("Finish joining with the sign-in link in your invitation email. "
                  "If it has expired, ask your administrator to resend it.")


def check_password_sign_in(status: Optional[str]) -> None:
    """A password sign-in for an account whose password matched."""
    if status not in SIGN_IN_STATUSES:
        raise _refuse(SUSPENDED_ACCOUNT, 403)
    if status == UserStatus.INVITED:
        # I-13: an invited account has no password of its own yet; it joins only through the
        # emailed set-password link (/magic-login), never with a password.
        raise _refuse(FINISH_JOINING, 403)


def password_too_short(password: str) -> bool:
    return len(password) < MIN_PASSWORD_LENGTH


_STATUS_RANK = {UserStatus.ACTIVE: 0, UserStatus.INVITED: 1}


def _login_rank(user: Any) -> tuple[int, str, str]:
    created = getattr(user, "created_at", None)
    return (_STATUS_RANK.get(getattr(user, "status", None) or "", 2),
            created.isoformat() if created is not None else "9999",
            str(getattr(user, "user_id", "") or ""))


def pick_login_user(users: Iterable[Any]) -> Optional[Any]:
    """The account to sign in among the rows sharing one email (deterministic), or None: ACTIVE before
    INVITED before anything else, then the oldest, then the lowest id."""
    candidates = list(users)
    return min(candidates, key=_login_rank) if candidates else None


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


def may_link_google(current_google_id: Optional[str], email: str, allowed_domains: Iterable[str]) -> bool:
    """A Google identity may be attached to an existing account found by its (Google-verified) email only
    for listed domains, and never to an account already linked to a different Google identity (I-12)."""
    return not current_google_id and email_domain(email) in set(allowed_domains)
