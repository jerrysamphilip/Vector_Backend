"""
Contact upload repository (ADR 0009): what confirming a validated contact upload reads and writes
(the tenant's suppression list, existing contacts by email, the upload's list and members).

Database refusals are translated into ``UploadRowRefused`` / ``UploadConflict`` so the service can react
without importing SQLAlchemy. Every entry point takes the tenant; ``member_counts`` acts on a list id
checked with ``list_exists`` first. Never commits except through ``commit``, which the service calls
when it owns the end of the transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Optional, TypeVar

from sqlalchemy import case, func
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.repositories.base import TenantId

__all__ = ["Session", "SqlAlchemyUploadRepository", "UploadConflict", "UploadRowRefused"]

T = TypeVar("T")


class UploadRowRefused(Exception):
    """The database refused a row: ``duplicate`` (unique key) or a value too long / invalid."""

    def __init__(self, duplicate: bool, cause: Exception) -> None:
        super().__init__(str(cause))
        self.duplicate = duplicate


class UploadConflict(Exception):
    """The commit clashed with a unique key (e.g. a concurrent confirmation of the same upload)."""


class SqlAlchemyUploadRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def suppression_reasons(self, tenant_id: TenantId) -> list[tuple[str, Optional[str]]]:
        """(email as stored, reason) of every suppression of the tenant."""
        return [(e, r) for e, r in self._session.query(GlobalUnsubscribe.email, GlobalUnsubscribe.reason)
                .filter(GlobalUnsubscribe.tenant_id == tenant_id).all()]

    def list_exists(self, tenant_id: TenantId, list_id: str) -> bool:
        return self._session.query(ProspectList.list_id).filter(
            ProspectList.list_id == list_id, ProspectList.tenant_id == tenant_id).first() is not None

    def list_held_elsewhere(self, tenant_id: TenantId, list_id: str) -> bool:
        """Another tenant already holds this list id (an upload id is the id of the list it creates)."""
        return self._session.query(ProspectList.list_id).filter(
            ProspectList.list_id == list_id, ProspectList.tenant_id != tenant_id).first() is not None

    def member_counts(self, list_id: str) -> tuple[int, int]:
        """(members, members that were new contacts) of the list."""
        total, new = self._session.query(
            func.count(ProspectListMember.id),
            func.coalesce(func.sum(case((ProspectListMember.is_new_prospect.is_(True), 1), else_=0)), 0),
        ).filter(ProspectListMember.list_id == list_id).one()
        return int(total or 0), int(new or 0)

    def contacts_with_emails(self, tenant_id: TenantId, emails: Sequence[str]) -> list[Prospect]:
        if not emails:
            return []
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                    Prospect.email.in_(list(emails))).all()

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def in_savepoint(self, work: Callable[[], T]) -> T:
        """Run ``work`` and flush inside a savepoint; a refused row rolls the savepoint back and raises
        UploadRowRefused."""
        try:
            with self._session.begin_nested():
                result = work()
                self._session.flush()
            return result
        except IntegrityError as exc:
            raise UploadRowRefused(True, exc) from exc
        except DataError as exc:
            raise UploadRowRefused(False, exc) from exc

    def commit(self) -> None:
        """Commit the caller's transaction; UploadConflict (after rolling back) on a unique-key clash."""
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise UploadConflict(str(exc)) from exc
