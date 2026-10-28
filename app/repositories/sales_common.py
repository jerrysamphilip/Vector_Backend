"""Shared helpers of the sales repositories: owner-scope SQL and the session-bound base class."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import Query, Session

from app.core.concurrency import claim_version
from app.domain.sales import OwnerScope


def owner_condition(column: Any, scope: OwnerScope) -> Optional[ColumnElement[bool]]:
    """SQL condition limiting ``column`` (an owner id) to the scope; None = no limit."""
    if scope.owner_ids is None:
        return None
    clause: ColumnElement[bool] = column.in_(scope.owner_ids)
    if scope.include_unowned:
        clause = or_(clause, column.is_(None))  # unassigned records can be picked up
    return clause


def scoped(query: Query[Any], column: Any, scope: OwnerScope) -> Query[Any]:
    clause = owner_condition(column, scope)
    return query if clause is None else query.filter(clause)


class SessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def flush(self) -> None:
        self._session.flush()

    def claim_version(self, entity: Any, expected: Optional[int], record: str) -> int:
        """Optimistic concurrency (T-3): bump row_version if it still equals ``expected``."""
        return claim_version(self._session, entity, expected, record=record)
