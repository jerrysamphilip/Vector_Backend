"""Sign-in retried on a MySQL lock conflict (live regression: login 500 on deadlock 1213 while recording
``users.last_login_at``). No database: the fake unit of work fails its commit with a hand-built driver error
and, like a real rollback, drops what the failed attempt staged."""
import pymysql
import pytest
from sqlalchemy.exc import OperationalError

from app.core.security import decode_access_token, hash_password, hash_token
from app.repositories import lock_retry
from app.services import auth_session
from app.services.auth_service import AuthService
from tests.unit.a5_identity_fakes import FakeUow, tenant, user

PASSWORD = "correct-horse-1"


class Throttle:
    def __init__(self):
        self.events = []

    def check(self):
        self.events.append("check")

    def failed(self):
        self.events.append("failed")

    def succeeded(self):
        self.events.append("succeeded")


class ConflictingUow(FakeUow):
    """Commit fails with the given MySQL error codes, one per commit; ``__exit__`` after a failure rolls the
    attempt back (refresh tokens staged since the last good commit are dropped, last_login_at restored)."""

    def __init__(self, codes, **kw):
        super().__init__(**kw)
        self.codes = list(codes)
        self._mark = (0, {})

    def __enter__(self):
        self._mark = (len(self.tokens.refresh), {u.user_id: u.last_login_at for u in self.users.rows})
        return super().__enter__()

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            kept, logins = self._mark
            del self.tokens.refresh[kept:]
            for u in self.users.rows:
                u.last_login_at = logins[u.user_id]
        return super().__exit__(exc_type, exc, tb)

    def commit(self):
        if self.codes:
            code = self.codes.pop(0)
            raise OperationalError("UPDATE users SET last_login_at=...", {}, pymysql.err.OperationalError(code, "x"))
        super().commit()


@pytest.fixture(autouse=True)
def _no_activity_rows(monkeypatch):
    monkeypatch.setattr(auth_session, "record_for", lambda db, u: None)


def _service(codes):
    me = user(email="a@x.com", password_hash=hash_password(PASSWORD))
    uow = ConflictingUow(codes, users=(me,), tenants=(tenant(),))
    return AuthService(uow), uow, me


@pytest.mark.parametrize("code", [1213, 1205])
def test_final_login_u01_lock_conflict_on_first_attempt_signs_in_once(code):
    svc, uow, me = _service([code])
    throttle = Throttle()
    result = svc.login("a@x.com", PASSWORD, throttle)
    assert decode_access_token(result["access_token"])["sub"] == me.user_id
    assert len(uow.tokens.refresh) == 1  # one refresh token: one session row
    assert uow.tokens.refresh[0].token_hash == hash_token(result["refresh_token"])
    assert uow.entered == 2 and uow.rollbacks == 1 and uow.commits == 1
    assert me.last_login_at is not None
    assert throttle.events == ["check", "succeeded", "succeeded"]  # the rate limit is checked once


def test_final_login_u02_persistent_conflict_is_raised_after_the_last_attempt():
    svc, uow, _ = _service([1213] * lock_retry.LOCK_RETRY_ATTEMPTS)
    with pytest.raises(OperationalError):
        svc.login("a@x.com", PASSWORD, Throttle())
    assert uow.entered == lock_retry.LOCK_RETRY_ATTEMPTS and not uow.tokens.refresh and uow.commits == 0


def test_final_login_u03_other_database_errors_are_not_retried():
    svc, uow, _ = _service([1054])
    with pytest.raises(OperationalError):
        svc.login("a@x.com", PASSWORD, Throttle())
    assert uow.entered == 1 and not uow.tokens.refresh
