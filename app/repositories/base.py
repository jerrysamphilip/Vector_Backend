"""
Base repository and unit-of-work contracts (ADR 0009).

Services depend on the Protocols (``Repository``, ``UnitOfWork``) so unit tests can hand them fakes;
production code uses the SQLAlchemy implementations below.

Transaction ownership: one service call = one unit of work. Repositories add, load and delete
objects in the unit of work's session but never commit; the service decides when to ``commit()``.
An exception inside the ``with`` block rolls back; work that was never committed is discarded when the
session closes (the unit of work's own session on exit, a borrowed request session when ``get_db``
closes it at the end of the request).
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Generic, Protocol, TypeVar

from sqlalchemy.orm import Session

ModelT = TypeVar("ModelT")
IdT = TypeVar("IdT", contravariant=True)
UowT = TypeVar("UowT", bound="UnitOfWork")

TenantId = str
"""Tenants are identified by their UUID string (``tenants.tenant_id``)."""


class Repository(Protocol[ModelT, IdT]):
    """Minimal tenant-scoped repository: every call names the tenant it acts for."""

    def get(self, tenant_id: TenantId, entity_id: IdT) -> ModelT | None:
        """The entity, or None when it does not exist or belongs to another tenant."""
        ...

    def add(self, tenant_id: TenantId, entity: ModelT) -> ModelT:
        """Stage a new entity for ``tenant_id`` (persisted when the unit of work commits)."""
        ...

    def delete(self, tenant_id: TenantId, entity_id: IdT) -> bool:
        """Stage deletion; False when there is nothing of this tenant's to delete."""
        ...


class UnitOfWork(Protocol):
    """Transaction boundary owned by a service call."""

    def __enter__(self: UowT) -> UowT: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyRepository(Generic[ModelT, IdT]):
    """``Repository`` over a SQLAlchemy session for a model with a ``tenant_id`` column."""

    def __init__(self, session: Session, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    def get(self, tenant_id: TenantId, entity_id: IdT) -> ModelT | None:
        entity = self._session.get(self._model, entity_id)
        if entity is None or getattr(entity, "tenant_id", None) != tenant_id:
            return None
        return entity

    def add(self, tenant_id: TenantId, entity: ModelT) -> ModelT:
        current = getattr(entity, "tenant_id", None)
        if current is None:
            setattr(entity, "tenant_id", tenant_id)
        elif current != tenant_id:
            raise ValueError("entity belongs to another tenant")
        self._session.add(entity)
        return entity

    def delete(self, tenant_id: TenantId, entity_id: IdT) -> bool:
        entity = self.get(tenant_id, entity_id)
        if entity is None:
            return False
        self._session.delete(entity)
        return True


class SqlAlchemyUnitOfWork:
    """``UnitOfWork`` over a SQLAlchemy session.

    ``SqlAlchemyUnitOfWork(SessionLocal)`` opens (and closes) its own session -- worker and scripts.
    ``SqlAlchemyUnitOfWork.for_session(db)`` wraps the request session from ``get_db``, which the
    FastAPI dependency closes.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._factory = session_factory
        self._session: Session | None = None
        self._owns_session = True

    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work used outside its 'with' block")
        return self._session

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exc_type is not None:
                session.rollback()
        finally:
            if self._owns_session:
                session.close()
            self._session = None

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
