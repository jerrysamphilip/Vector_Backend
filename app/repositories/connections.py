"""
User account connections repository and unit of work (ADR 0009; BR-SF-16): a user's own Google /
Microsoft connections and what the calendar/email sync reads and writes (contacts in the user's scope,
timeline activities). Connections are per user (and carry their tenant); contact lookups take the
tenant and the caller's VisibilityScope. The sync job's scan of every connection is cross-tenant by
design. Never commits.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional, Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.contacts import VisibilityScope
from app.models.contact_activity import ContactActivity
from app.models.prospect import Prospect
from app.models.sales_extra import UserConnection
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId
from app.repositories.contacts import apply_scope

__all__ = [
    "ConnectionRepository", "ConnectionsUnitOfWork", "Session", "SqlAlchemyConnectionRepository",
    "SqlAlchemyConnectionsUnitOfWork", "connections_repo",
]


class ConnectionRepository(Protocol):
    def for_user(self, user_id: str) -> list[UserConnection]: ...
    def owned(self, user_id: str, connection_id: str) -> Optional[UserConnection]: ...
    def by_provider(self, user_id: str, provider: str) -> Optional[UserConnection]: ...
    def by_id(self, connection_id: str) -> Optional[UserConnection]: ...
    def calendar_connection(self, user_id: str) -> Optional[UserConnection]: ...
    def every(self) -> list[UserConnection]: ...
    def user(self, user_id: str) -> Optional[User]: ...
    def prospect(self, prospect_id: str) -> Optional[Prospect]: ...
    def contacts_by_emails(self, tenant_id: TenantId, scope: VisibilityScope,
                           emails: Iterable[str]) -> list[Prospect]: ...
    def activity_logged(self, external_id: str) -> bool: ...
    def add(self, entity: Any) -> Any: ...
    def delete(self, entity: Any) -> None: ...


class SqlAlchemyConnectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def for_user(self, user_id: str) -> list[UserConnection]:
        return self._session.query(UserConnection).filter(UserConnection.user_id == user_id).all()

    def owned(self, user_id: str, connection_id: str) -> Optional[UserConnection]:
        return self._session.query(UserConnection).filter(UserConnection.connection_id == connection_id,
                                                          UserConnection.user_id == user_id).first()

    def by_provider(self, user_id: str, provider: str) -> Optional[UserConnection]:
        return self._session.query(UserConnection).filter(UserConnection.user_id == user_id,
                                                          UserConnection.provider == provider).first()

    def by_id(self, connection_id: str) -> Optional[UserConnection]:
        return self._session.query(UserConnection).filter(UserConnection.connection_id == connection_id).first()

    def calendar_connection(self, user_id: str) -> Optional[UserConnection]:
        return self._session.query(UserConnection).filter(UserConnection.user_id == user_id,
                                                          UserConnection.sync_calendar.is_(True)).first()

    def every(self) -> list[UserConnection]:
        return self._session.query(UserConnection).all()

    def user(self, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.user_id == user_id).first()

    def prospect(self, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id).first()

    def contacts_by_emails(self, tenant_id: TenantId, scope: VisibilityScope,
                           emails: Iterable[str]) -> list[Prospect]:
        """Live contacts of the tenant inside ``scope`` whose lower-cased email is in ``emails``."""
        q = self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None),
                                                 func.lower(Prospect.email).in_(list(emails)))
        return apply_scope(q, Prospect.owner_id, scope).all()

    def activity_logged(self, external_id: str) -> bool:
        return self._session.query(ContactActivity.activity_id).filter(
            ContactActivity.external_id == external_id).first() is not None

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def delete(self, entity: Any) -> None:
        self._session.delete(entity)


def connections_repo(session: Session) -> SqlAlchemyConnectionRepository:
    """For the legacy ``fn(db, ...)`` helpers in app/services/account_sync.py."""
    return SqlAlchemyConnectionRepository(session)


class ConnectionsUnitOfWork(Protocol):
    @property
    def connections(self) -> ConnectionRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> ConnectionsUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyConnectionsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyConnectionsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyConnectionsUnitOfWork:
        super().__enter__()
        return self

    @property
    def connections(self) -> SqlAlchemyConnectionRepository:
        return SqlAlchemyConnectionRepository(self.session)
