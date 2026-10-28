"""
A user's own Google / Microsoft account connections (BRD v2.0 BR-SF-16): one method per route of
app/routers/connections_router.py. Each user sees and changes only their own connections (another
user's id is 404). OAuth and sync mechanics stay in app/services/account_sync.py. No sqlalchemy /
fastapi imports.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from app.core.errors import BadRequestError, NotFoundError
from app.models.sales_extra import UserConnection
from app.repositories.connections import ConnectionsUnitOfWork, Session, SqlAlchemyConnectionsUnitOfWork
from app.services import account_sync, oauth_binding

__all__ = ["ConnectionService", "connection_dict"]

PROVIDER = {"google": "GOOGLE", "microsoft": "MICROSOFT"}


def connection_dict(c: UserConnection) -> dict:
    return {"connection_id": c.connection_id, "provider": c.provider, "account_email": c.account_email,
            "sync_email": c.sync_email, "sync_calendar": c.sync_calendar, "last_sync_at": c.last_sync_at,
            "last_error": c.last_error, "created_at": c.created_at}


class ConnectionService:
    def __init__(self, uow: ConnectionsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> ConnectionService:
        return cls(SqlAlchemyConnectionsUnitOfWork.for_session(db))

    def mine(self, user: Any) -> dict:
        with self.uow as uow:
            rows = uow.connections.for_user(user.user_id)
            return {"connections": [connection_dict(c) for c in rows],
                    "available": {p: account_sync.configured(p) for p in account_sync.PROVIDERS}}

    @staticmethod
    def start(provider: str, user: Any, issue_nonce: Callable[[], str]) -> dict:
        """The provider's sign-in URL; ``issue_nonce`` binds it to this browser (cookie)."""
        p = PROVIDER.get(provider)
        if not p:
            raise NotFoundError("Unknown provider")
        if not account_sync.configured(p):
            names = ("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_SYNC_REDIRECT_URI" if p == "GOOGLE"
                     else "MS365_CLIENT_ID, MS365_CLIENT_SECRET and GRAPH_REDIRECT_URI")
            raise BadRequestError(f"{provider.title()} sync is not set up on the server. Set {names}.")
        return {"authorize_url": account_sync.authorize_url(p, user, issue_nonce())}

    def complete(self, provider: str, state: str, code: str, error: str, error_description: str,
                 nonce_matches: Callable[[Optional[str]], bool]) -> dict:
        """Finish the OAuth sign-in; the query parameters to send the browser back with."""
        try:
            data = account_sync.read_state(state)
        except account_sync.SyncError as exc:
            return {"sync": "error", "message": str(exc)}
        if not nonce_matches(data.get("nonce")):
            return {"sync": "error", "message": oauth_binding.MISMATCH}
        if error:
            return {"sync": "error", "message": error_description or error}
        if PROVIDER.get(provider) != data["provider"]:
            return {"sync": "error", "message": "Provider mismatch"}
        with self.uow as uow:
            user = uow.connections.user(data["user_id"])
            if not user:
                return {"sync": "error", "message": "User not found"}
            try:
                conn = account_sync.complete(uow.session, user, data["provider"], code)
            except account_sync.SyncError as exc:
                return {"sync": "error", "message": str(exc)}
            account_sync.sync_connection(uow.session, conn)
            return {"sync": "connected", "provider": provider}

    @staticmethod
    def _owned(uow: ConnectionsUnitOfWork, user: Any, connection_id: str) -> UserConnection:
        c = uow.connections.owned(user.user_id, connection_id)
        if not c:
            raise NotFoundError("Connection not found")
        return c

    def update(self, connection_id: str, changes: dict, user: Any) -> dict:
        with self.uow as uow:
            c = self._owned(uow, user, connection_id)
            for f, v in changes.items():
                setattr(c, f, bool(v))
            uow.commit()
            return connection_dict(c)

    def sync_now(self, connection_id: str, user: Any) -> dict:
        with self.uow as uow:
            c = self._owned(uow, user, connection_id)
            return {**account_sync.sync_connection(uow.session, c), "connection": connection_dict(c)}

    def disconnect(self, connection_id: str, user: Any) -> dict:
        with self.uow as uow:
            uow.connections.delete(self._owned(uow, user, connection_id))
            uow.commit()
            return {"status": "disconnected"}
