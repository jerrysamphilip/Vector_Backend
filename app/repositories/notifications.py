"""
In-app notifications repository (ADR 0009, chunk A7; BR-SF-07). Never commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.models.sales_extra import Notification
from app.models.user import User

__all__ = ["NotificationRepository", "Session", "SqlAlchemyNotificationRepository", "notifications_repo"]


class NotificationRepository(Protocol):
    def told_recently(self, user_id: str, dedupe_key: str, since: datetime) -> bool: ...
    def add(self, entity: Any) -> Any: ...
    def manager_of(self, user_id: str) -> Optional[str]: ...
    def unsent(self, since: datetime, limit: int) -> list[tuple[Notification, User]]: ...


class SqlAlchemyNotificationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def told_recently(self, user_id: str, dedupe_key: str, since: datetime) -> bool:
        return self._session.query(Notification.notification_id).filter(
            Notification.user_id == user_id, Notification.dedupe_key == dedupe_key,
            Notification.created_at >= since).first() is not None

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def manager_of(self, user_id: str) -> Optional[str]:
        return self._session.query(User.manager_id).filter(User.user_id == user_id).scalar()

    def unsent(self, since: datetime, limit: int) -> list[tuple[Notification, User]]:
        """Notifications not emailed yet (created since ``since``), oldest first, with their user."""
        rows = self._session.query(Notification, User).join(User, User.user_id == Notification.user_id).filter(
            Notification.emailed_at.is_(None), Notification.created_at >= since
        ).order_by(Notification.created_at).limit(limit).all()
        return [(n, u) for n, u in rows]


def notifications_repo(session: Session) -> SqlAlchemyNotificationRepository:
    return SqlAlchemyNotificationRepository(session)
