"""D1: the shared retry of a whole unit of work on a MySQL deadlock (1213) / lock wait timeout (1205).

No database: the driver errors are built by hand.
"""
import pymysql
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.repositories.lock_retry import LOCK_RETRY_ATTEMPTS, is_lock_conflict, retry_on_lock_conflict


def _mysql_error(code, cls=OperationalError):
    return cls("UPDATE prospects ...", {}, pymysql.err.OperationalError(code, "message"))


class Work:
    """Fails with the given errors, one per call, then returns ``done``."""

    def __init__(self, *errors):
        self.errors, self.calls = list(errors), 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "done"


@pytest.mark.parametrize("code", [1213, 1205])
def test_lock_conflicts_are_recognised(code):
    assert is_lock_conflict(_mysql_error(code))


@pytest.mark.parametrize("exc", [
    _mysql_error(1054), _mysql_error(1062, IntegrityError), ValueError("x"), OperationalError("s", {}, None),
])
def test_other_errors_are_not(exc):
    assert not is_lock_conflict(exc)


@pytest.mark.parametrize("code", [1213, 1205])
def test_a_conflict_runs_the_whole_work_again(code):
    pauses = []
    work = Work(_mysql_error(code))
    assert retry_on_lock_conflict(work, sleep=pauses.append) == "done"
    assert work.calls == 2 and len(pauses) == 1 and 0 < pauses[0] <= 1


def test_gives_up_after_the_last_attempt():
    errors = [_mysql_error(1213) for _ in range(LOCK_RETRY_ATTEMPTS)]
    work = Work(*errors)
    with pytest.raises(OperationalError) as caught:
        retry_on_lock_conflict(work, sleep=lambda s: None)
    assert caught.value is errors[-1]
    assert work.calls == LOCK_RETRY_ATTEMPTS


def test_other_errors_are_raised_at_once():
    work = Work(_mysql_error(1054))
    with pytest.raises(OperationalError):
        retry_on_lock_conflict(work, sleep=lambda s: pytest.fail("no pause expected"))
    assert work.calls == 1


def test_backoff_grows_with_the_attempt():
    pauses = []
    retry_on_lock_conflict(Work(_mysql_error(1213), _mysql_error(1213)), sleep=pauses.append, attempts=3)
    assert len(pauses) == 2 and pauses[1] > 0
