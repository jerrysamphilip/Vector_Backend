"""
What the request authentication dependencies (app/core/auth.py) read from the database: token
revocation, the token's user, the user's tenant flags, and the daily activity mark. app.core sits below
the models and services, so app.main installs this store with ``app.core.auth.configure``.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from app.repositories.identity import borrow
from app.services.identity_common import tenant_flags
from app.services.token_revocation import is_revoked
from app.services.user_activity import record_for

__all__ = ["SqlAlchemyAuthStore"]


class SqlAlchemyAuthStore:
    """``app.core.auth.AuthStore`` over the request session."""

    def revoked(self, db: Any, jti: Optional[str]) -> bool:
        return is_revoked(db, jti)

    def user(self, db: Any, user_id: str) -> Any:
        return borrow(db).users.get(user_id)

    def tenant_flags(self, db: Any, user: Any) -> Tuple[Optional[str], bool]:
        """(status, require_mfa) of the user's tenant; (None, False) for PLATFORM_ADMIN / no tenant."""
        flags = tenant_flags(borrow(db), user)
        return (flags.status, flags.require_mfa) if flags else (None, False)

    def record_activity(self, db: Any, user: Any) -> None:
        record_for(db, user)
