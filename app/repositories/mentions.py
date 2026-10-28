"""
Note mentions repository (ADR 0009; BR-CM-16): the workspace's mentionable users and the mentions
stored per note. User lookups take the tenant. Never commits.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.domain.mentions import MENTIONABLE_ROLES
from app.models.crm import NoteMention
from app.models.user import User
from app.repositories.base import TenantId
from app.utils.sql_like import like

__all__ = ["Session", "SqlAlchemyMentionRepository", "mentions_repo"]


class SqlAlchemyMentionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _active(self, tenant_id: TenantId) -> Any:
        return self._session.query(User).filter(User.tenant_id == tenant_id, User.status == "ACTIVE",
                                                User.role.in_(MENTIONABLE_ROLES))

    def mentionable(self, tenant_id: TenantId, q: Optional[str], limit: int) -> list[User]:
        """Active colleagues whose name or email contains ``q``, by name."""
        query = self._active(tenant_id)
        if q and q.strip():
            term = q.strip().lower()
            query = query.filter(or_(like(func.lower(func.concat(func.coalesce(User.first_name, ""), " ",
                                                                 func.coalesce(User.last_name, ""))), term),
                                     like(func.lower(User.email), term)))
        return query.order_by(User.first_name, User.last_name).limit(limit).all()

    def active_users(self, tenant_id: TenantId, user_ids: Collection[str]) -> list[User]:
        return self._active(tenant_id).filter(User.user_id.in_(list(user_ids))).all()

    def of_activity(self, activity_id: str) -> list[NoteMention]:
        return self._session.query(NoteMention).filter(NoteMention.activity_id == activity_id).all()

    def with_users(self, activity_ids: Collection[str]) -> list[tuple[NoteMention, User]]:
        rows = self._session.query(NoteMention, User).join(User, User.user_id == NoteMention.user_id).filter(
            NoteMention.activity_id.in_(list(activity_ids))).order_by(NoteMention.created_at)
        return [(m, u) for m, u in rows]

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def delete(self, entity: Any) -> None:
        self._session.delete(entity)


def mentions_repo(session: Session) -> SqlAlchemyMentionRepository:
    return SqlAlchemyMentionRepository(session)
