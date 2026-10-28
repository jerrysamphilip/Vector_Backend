"""
CRM import repository and unit of work (ADR 0009, chunk A7).

All SQL of the import: the rows it matches (contacts by e-mail, companies by domain / name), the rows
it inserts under the database's unique keys, import jobs (history, progress, the worker's claim) and
saved column mappings. Never commits; the import service owns the transaction.

Database outcomes the service reacts to are raised as plain exceptions defined here, so the service
needs no SQLAlchemy import:
  ``DuplicateRow``   an insert hit a unique key (another import created the row a moment ago)
  ``RejectedValue``  MySQL refused a value (strict mode DataError); ``reason`` names the field
``ImportsUnitOfWork.is_retryable(exc)`` recognises deadlocks / lock wait timeouts worth a retry.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import func, or_
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.repositories import lists as lists_repo
from app.models.account import Account
from app.models.contact_field import ContactFieldDefinition
from app.models.contact_identity import ContactIdentifier
from app.models.crm import ImportJob
from app.models.import_mapping import ImportMapping
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId
from app.repositories.contacts import SqlAlchemyContactRepository

__all__ = [
    "DuplicateRow", "ImportRepository", "ImportsUnitOfWork", "RejectedValue", "Session",
    "SqlAlchemyImportRepository", "SqlAlchemyImportsUnitOfWork", "imports_uow",
]

# MySQL errors after which the whole chunk is worth running again: 1213 deadlock, 1205 lock wait
# timeout; 1305 "savepoint does not exist" surfaces when the deadlock hit inside a savepoint
# (MySQL dropped the whole transaction)
RETRYABLE_ERRORS = (1213, 1205, 1305)
EMAIL_BATCH = 1000


class DuplicateRow(Exception):
    """An insert collided with a unique key."""


class RejectedValue(Exception):
    """The database refused a value; ``orig`` carries the driver message (column name)."""

    def __init__(self, orig: Any) -> None:
        super().__init__(str(orig))
        self.orig = orig


class ImportRepository(Protocol):
    # reference data
    def field_definitions(self, tenant_id: TenantId) -> list[ContactFieldDefinition]: ...
    def users(self, tenant_id: TenantId) -> list[User]: ...
    def user(self, tenant_id: TenantId, user_id: str) -> Optional[User]: ...
    def user_names(self, ids: Collection[str]) -> dict[str, str]: ...
    def unsubscribed_emails(self, tenant_id: TenantId) -> set[str]: ...
    def static_list(self, tenant_id: TenantId, list_id: str) -> Optional[ProspectList]: ...
    # matching
    def contacts_by_email(self, tenant_id: TenantId, emails: Collection[str]) -> dict[str, Prospect]: ...
    def lock_contact(self, tenant_id: TenantId, email: str) -> Optional[Prospect]: ...
    def live_account(self, tenant_id: TenantId, *, domain: Optional[str] = None,
                     name: Optional[str] = None) -> Optional[Account]: ...
    def deleted_account_holds_domain(self, tenant_id: TenantId, domain: str) -> bool: ...
    def account_name_taken(self, tenant_id: TenantId, name: str) -> bool: ...
    def lock_account(self, tenant_id: TenantId, *, domain: Optional[str], name: str) -> Optional[Account]: ...
    # writes
    def add(self, entity: Any) -> Any: ...
    def insert(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...
    def free_list_name(self, tenant_id: TenantId, name: str) -> str: ...
    def list_member_ids(self, list_id: str, ids: Collection[str]) -> set[str]: ...
    @contextmanager
    def row(self) -> Iterator[None]: ...
    # jobs
    def job(self, tenant_id: TenantId, import_id: str) -> Optional[ImportJob]: ...
    def job_by_id(self, import_id: str) -> Optional[ImportJob]: ...
    def history(self, tenant_id: TenantId, offset: int, limit: int) -> tuple[list[ImportJob], int]: ...
    def claim_next(self, stale_before: datetime, now: datetime, worker: str) -> Optional[str]: ...
    def fail_interrupted(self, cutoff: datetime, now: datetime, message: str) -> int: ...
    # saved mappings
    def mappings(self, tenant_id: TenantId) -> list[ImportMapping]: ...
    def mapping(self, tenant_id: TenantId, mapping_id: str) -> Optional[ImportMapping]: ...
    def mapping_named(self, tenant_id: TenantId, name: str) -> Optional[ImportMapping]: ...
    def delete(self, entity: Any) -> None: ...


class SqlAlchemyImportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # reference data ----------------------------------------------------------
    def field_definitions(self, tenant_id: TenantId) -> list[ContactFieldDefinition]:
        # Active contact properties only: company properties (object_type COMPANY) and deactivated ones
        # are never import targets
        return self._session.query(ContactFieldDefinition).filter(
            ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.object_type == "CONTACT",
            ContactFieldDefinition.is_active.is_(True)).all()

    def users(self, tenant_id: TenantId) -> list[User]:
        return self._session.query(User).filter(User.tenant_id == tenant_id).all()

    def user(self, tenant_id: TenantId, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.user_id == user_id, User.tenant_id == tenant_id).first()

    def user_names(self, ids: Collection[str]) -> dict[str, str]:
        if not ids:
            return {}
        return {u.user_id: f"{u.first_name} {u.last_name}".strip()
                for u in self._session.query(User).filter(User.user_id.in_(set(ids)))}

    def unsubscribed_emails(self, tenant_id: TenantId) -> set[str]:
        return {e.lower() for (e,) in self._session.query(GlobalUnsubscribe.email).filter(
            GlobalUnsubscribe.tenant_id == tenant_id)}

    def static_list(self, tenant_id: TenantId, list_id: str) -> Optional[ProspectList]:
        return self._session.query(ProspectList).filter(
            ProspectList.list_id == list_id, ProspectList.tenant_id == tenant_id,
            ProspectList.list_type == "STATIC").first()

    # matching ----------------------------------------------------------------
    def contacts_by_email(self, tenant_id: TenantId, emails: Collection[str]) -> dict[str, Prospect]:
        """Lower-cased e-mail -> contact (deleted ones included, so the caller can refuse them). An address
        that is no contact's current one but was retired or merged resolves to the contact it now belongs
        to (C-11, the contacts repository's ``resolve_contact_by_email``)."""
        wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
        found: dict[str, Prospect] = {}
        for start in range(0, len(wanted), EMAIL_BATCH):
            batch = wanted[start:start + EMAIL_BATCH]
            for p in self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                          func.lower(Prospect.email).in_(batch)):
                found[p.email.lower()] = p
            self._resolve_history(tenant_id, [e for e in batch if e not in found], found)
        return found

    def _resolve_history(self, tenant_id: TenantId, missing: list[str], found: dict[str, Prospect]) -> None:
        if not missing:
            return
        known = [e for (e,) in self._session.query(ContactIdentifier.email).filter(
            ContactIdentifier.tenant_id == tenant_id, ContactIdentifier.email.in_(missing))]
        contacts = SqlAlchemyContactRepository(self._session)
        for email in known:
            prospect = contacts.resolve_contact_by_email(tenant_id, email)
            if prospect is not None:
                found[email] = prospect

    def lock_contact(self, tenant_id: TenantId, email: str) -> Optional[Prospect]:
        # FOR UPDATE reads the latest committed row; a plain read would use this transaction's
        # snapshot, which predates the other import's commit
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                    Prospect.email == email).with_for_update().first()

    def live_account(self, tenant_id: TenantId, *, domain: Optional[str] = None,
                     name: Optional[str] = None) -> Optional[Account]:
        query = self._session.query(Account).filter(Account.tenant_id == tenant_id, Account.deleted_at.is_(None))
        if domain:
            return query.filter(Account.domain == domain).first()
        if name:
            return query.filter(Account.name == name).first()
        return None

    def deleted_account_holds_domain(self, tenant_id: TenantId, domain: str) -> bool:
        return self._session.query(Account.account_id).filter(
            Account.tenant_id == tenant_id, Account.domain == domain, Account.deleted_at.isnot(None)).first() is not None

    def account_name_taken(self, tenant_id: TenantId, name: str) -> bool:
        return self._session.query(Account.account_id).filter(
            Account.tenant_id == tenant_id, Account.name == name).first() is not None

    def lock_account(self, tenant_id: TenantId, *, domain: Optional[str], name: str) -> Optional[Account]:
        key = (Account.domain == domain) if domain else (Account.name == name)
        return self._session.query(Account).filter(Account.tenant_id == tenant_id, key).with_for_update().first()

    # writes ------------------------------------------------------------------
    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def insert(self, entity: Any) -> Any:
        """Insert now, in a savepoint; a unique-key collision undoes only this row (DuplicateRow)."""
        try:
            with self._session.begin_nested():
                self._session.add(entity)
                self._session.flush()
        except IntegrityError as exc:
            raise DuplicateRow(str(exc.orig)) from exc
        return entity

    def flush(self) -> None:
        self._session.flush()

    def free_list_name(self, tenant_id: TenantId, name: str) -> str:
        """A list name not used in the workspace yet: "Name", else "Name (2)", ... (C-21)."""
        return lists_repo.free_list_name(self._session, tenant_id, name)

    def list_member_ids(self, list_id: str, ids: Collection[str]) -> set[str]:
        if not ids:
            return set()
        return {r[0] for r in self._session.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(list(ids)))}

    @contextmanager
    def row(self) -> Iterator[None]:
        """One row's changes in a savepoint: an exception undoes only them. A value MySQL refuses
        comes out as RejectedValue."""
        try:
            with self._session.begin_nested():
                yield
        except DataError as exc:
            raise RejectedValue(exc.orig) from exc

    # jobs --------------------------------------------------------------------
    def job(self, tenant_id: TenantId, import_id: str) -> Optional[ImportJob]:
        return self._session.query(ImportJob).filter(ImportJob.import_id == import_id,
                                                     ImportJob.tenant_id == tenant_id).first()

    def job_by_id(self, import_id: str) -> Optional[ImportJob]:
        return self._session.get(ImportJob, import_id)

    def history(self, tenant_id: TenantId, offset: int, limit: int) -> tuple[list[ImportJob], int]:
        query = self._session.query(ImportJob).filter(ImportJob.tenant_id == tenant_id)
        total = query.count()
        rows = query.order_by(ImportJob.started_at.desc()).offset(offset).limit(limit).all()
        return rows, total

    def claim_next(self, stale_before: datetime, now: datetime, worker: str) -> Optional[str]:
        """Take the oldest queued background import, or one whose worker stopped beating (crash), and
        mark it RUNNING for ``worker``. The conditional UPDATE makes the claim safe between processes."""
        candidate = self._session.query(ImportJob.import_id).filter(
            ImportJob.file_path.isnot(None),
            or_(ImportJob.status == "QUEUED",
                (ImportJob.status == "RUNNING") & or_(ImportJob.heartbeat_at.is_(None),
                                                      ImportJob.heartbeat_at < stale_before)),
        ).order_by(ImportJob.queued_at, ImportJob.started_at).first()
        if candidate is None:
            return None
        claimed = self._session.query(ImportJob).filter(
            ImportJob.import_id == candidate[0],
            or_(ImportJob.status == "QUEUED",
                (ImportJob.status == "RUNNING") & or_(ImportJob.heartbeat_at.is_(None),
                                                      ImportJob.heartbeat_at < stale_before)),
        ).update({ImportJob.status: "RUNNING", ImportJob.heartbeat_at: now, ImportJob.claimed_by: worker[:100],
                  ImportJob.attempts: ImportJob.attempts + 1}, synchronize_session=False)
        return candidate[0] if claimed == 1 else None

    def fail_interrupted(self, cutoff: datetime, now: datetime, message: str) -> int:
        """Synchronous imports (no stored file) still RUNNING long after they started died with their
        request (process restart); mark them FAILED so the history is truthful."""
        return self._session.query(ImportJob).filter(
            ImportJob.status == "RUNNING", ImportJob.file_path.is_(None), ImportJob.started_at < cutoff,
        ).update({ImportJob.status: "FAILED", ImportJob.message: message, ImportJob.finished_at: now},
                 synchronize_session=False)

    # saved mappings ----------------------------------------------------------
    def mappings(self, tenant_id: TenantId) -> list[ImportMapping]:
        return self._session.query(ImportMapping).filter(ImportMapping.tenant_id == tenant_id) \
            .order_by(ImportMapping.name).all()

    def mapping(self, tenant_id: TenantId, mapping_id: str) -> Optional[ImportMapping]:
        return self._session.query(ImportMapping).filter(ImportMapping.mapping_id == mapping_id,
                                                         ImportMapping.tenant_id == tenant_id).first()

    def mapping_named(self, tenant_id: TenantId, name: str) -> Optional[ImportMapping]:
        return self._session.query(ImportMapping).filter(ImportMapping.tenant_id == tenant_id,
                                                         ImportMapping.name == name).first()

    def delete(self, entity: Any) -> None:
        self._session.delete(entity)


class ImportsUnitOfWork(Protocol):
    @property
    def imports(self) -> ImportRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> ImportsUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def is_retryable(self, exc: BaseException) -> bool: ...


class SqlAlchemyImportsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyImportsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyImportsUnitOfWork:
        super().__enter__()
        return self

    @property
    def imports(self) -> SqlAlchemyImportRepository:
        return SqlAlchemyImportRepository(self.session)

    def is_retryable(self, exc: BaseException) -> bool:
        if not isinstance(exc, OperationalError):
            return False
        return getattr(exc.orig, "args", [None])[0] in RETRYABLE_ERRORS


def imports_uow(session: Session) -> SqlAlchemyImportsUnitOfWork:
    return SqlAlchemyImportsUnitOfWork.for_session(session)
