"""Agent follow: the import unit of work retries exactly the lock conflicts app/repositories/lock_retry.py names."""
from sqlalchemy.exc import IntegrityError, OperationalError

from app.repositories import lock_retry
from app.repositories.imports import SqlAlchemyImportsUnitOfWork


class _Orig(Exception):
    pass


def _err(cls, code):
    return cls("INSERT ...", {}, _Orig(code, "message"))


def test_import_retries_the_shared_lock_conflicts():
    uow = SqlAlchemyImportsUnitOfWork(lambda: None)
    assert [uow.is_retryable(_err(OperationalError, c)) for c in (1213, 1205, 1305)] == [True, True, True]
    assert not uow.is_retryable(_err(OperationalError, 2013))  # lost connection: not a lock conflict
    assert not uow.is_retryable(_err(IntegrityError, 1213))
    assert not uow.is_retryable(ValueError("x"))
    assert not uow.is_retryable(OperationalError("x", {}, _Orig()))  # no error code


def test_import_uses_the_one_list(monkeypatch):
    monkeypatch.setattr(lock_retry, "LOCK_CONFLICT_ERRORS", (1213,))
    uow = SqlAlchemyImportsUnitOfWork(lambda: None)
    assert uow.is_retryable(_err(OperationalError, 1213)) and not uow.is_retryable(_err(OperationalError, 1205))
