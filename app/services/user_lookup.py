"""
One live account per email address, across every workspace (F017).

A person may have old, deactivated accounts in several workspaces, but at most one ACTIVE or INVITED
account in total (users.active_email is unique: migration 0019_unique_active_email). PLATFORM_ADMIN
accounts (no workspace) count like any other: their address cannot also be live in a workspace.

- ``find_user_for_login``: the account sign-in, forgot-password and Google sign-in act on. When the
  address has several rows (old data from before the unique index) the live one wins, the same way
  every time: ACTIVE before INVITED before anything else, then the oldest.
- ``live_account_conflict``: the other live account that stops a new invite, an accepted invitation,
  a reactivation or a sign-up from making a second one.

Email comparison follows the column's case-insensitive collation (utf8mb4_0900_ai_ci).
"""

from typing import Any, Iterable, Optional, Protocol

from app.models.user import User

LIVE_STATUSES = ("ACTIVE", "INVITED")
_STATUS_RANK = {"ACTIVE": 0, "INVITED": 1}

LIVE_ACCOUNT_CONFLICT = ("This email address already has an active account in another workspace. "
                         "Use a different address, or deactivate the other account first.")


class _QuerySession(Protocol):
    def query(self, *entities: Any) -> Any: ...


def _login_rank(user: Any) -> tuple[int, str, str]:
    created = getattr(user, "created_at", None)
    return (_STATUS_RANK.get(getattr(user, "status", None) or "", 2),
            created.isoformat() if created is not None else "9999",
            str(getattr(user, "user_id", "") or ""))


def pick_login_user(users: Iterable[Any]) -> Optional[Any]:
    """The account to sign in among the rows sharing one email (deterministic), or None."""
    candidates = list(users)
    return min(candidates, key=_login_rank) if candidates else None


def find_user_for_login(db: _QuerySession, email: str) -> Optional[User]:
    """The account an email address signs in to: its live account when there is one."""
    if not email:
        return None
    return pick_login_user(db.query(User).filter(User.email == email).all())


def live_account_conflict(db: _QuerySession, email: str, exclude_user_id: Optional[str] = None) -> Optional[User]:
    """Another ACTIVE / INVITED account with this email (in any workspace), or None."""
    if not email:
        return None
    query = db.query(User).filter(User.email == email, User.status.in_(LIVE_STATUSES))
    if exclude_user_id:
        query = query.filter(User.user_id != exclude_user_id)
    return query.first()
