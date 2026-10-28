"""
One live account per email address, across every workspace (F017).

A person may have old, deactivated accounts in several workspaces, but at most one ACTIVE or INVITED
account in total (users.active_email is unique: migration 0019_unique_active_email). PLATFORM_ADMIN
accounts (no workspace) count like any other: their address cannot also be live in a workspace.

- ``login_account`` / ``find_user_for_login``: the account sign-in, forgot-password and Google sign-in act
  on. When the address has several rows (old data from before the unique index) the live one wins, the
  same way every time (``app.domain.identity.pick_login_user``).
- ``other_live_account`` / ``live_account_conflict``: the other live account that stops a new invite, an
  accepted invitation, a reactivation or a sign-up from making a second one.

The ``*(users, ...)`` forms take a ``UserRepository`` (services, inside their unit of work); the
``*(db, ...)`` forms are the legacy wrappers over a session. Email comparison follows the column's
case-insensitive collation (utf8mb4_0900_ai_ci).
"""

from typing import Any, Optional, Protocol

from app.domain.identity import LIVE_STATUSES, pick_login_user
from app.models.user import User
from app.repositories.identity import SqlAlchemyUserRepository, UserRepository

__all__ = ["LIVE_ACCOUNT_CONFLICT", "LIVE_STATUSES", "find_user_for_login", "live_account_conflict",
           "login_account", "other_live_account", "pick_login_user"]

LIVE_ACCOUNT_CONFLICT = ("This email address already has an active account in another workspace. "
                         "Use a different address, or deactivate the other account first.")


class _QuerySession(Protocol):
    def query(self, *entities: Any) -> Any: ...


def login_account(users: UserRepository, email: str) -> Optional[User]:
    """The account an email address signs in to: its live account when there is one."""
    if not email:
        return None
    return pick_login_user(users.with_email(email))


def other_live_account(users: UserRepository, email: str, exclude_user_id: Optional[str] = None) -> Optional[User]:
    """Another ACTIVE / INVITED account with this email (in any workspace), or None."""
    if not email:
        return None
    return users.live_with_email(email, exclude_user_id)


def find_user_for_login(db: _QuerySession, email: str) -> Optional[User]:
    return login_account(SqlAlchemyUserRepository(db), email)


def live_account_conflict(db: _QuerySession, email: str, exclude_user_id: Optional[str] = None) -> Optional[User]:
    return other_live_account(SqlAlchemyUserRepository(db), email, exclude_user_id)
