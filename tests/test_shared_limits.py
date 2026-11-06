"""Limits shared by every process (DEC-09): the sign-in lockout and auth rate limits in rate_limit_counters,
the daily new-contact limit in platform_flags. Two store / limiter instances, each on its own engine, stand
in for two API replicas (or the worker and a replica): they must share one count."""

import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from app.core import rate_limit as rl
from app.core.clock import utcnow
from app.core.config import settings
from app.services import daily_limit
from app.services.rate_limit_store import DatabaseRateLimitStore, bucket_id
from tests.conftest import RUN, register_tenant, sql


@pytest.fixture
def replicas():
    engines = [create_engine(settings.DATABASE_URL, pool_size=5) for _ in range(2)]
    yield [rl.FallbackLimiter(DatabaseRateLimitStore(engine=e)) for e in engines]
    for e in engines:
        e.dispose()


def _request(host):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=host))


def test_the_app_counts_sign_in_limits_in_the_database():
    assert isinstance(rl.limiter, rl.FallbackLimiter)
    assert isinstance(rl.limiter.primary, DatabaseRateLimitStore)


def test_lockout_triggers_at_ten_wrong_passwords_in_total_across_two_replicas(monkeypatch, replicas):
    email, ip = f"victim-{RUN}-{uuid.uuid4().hex[:6]}@x.example", "203.0.113.7"
    for i in range(10):                       # alternate: 5 wrong passwords on each replica
        monkeypatch.setattr(rl, "limiter", replicas[i % 2])
        rl.check_login_allowed(_request(ip), email)
        rl.record_login_failure(_request(ip), email)
    for replica in replicas:                  # both refuse the 11th attempt
        monkeypatch.setattr(rl, "limiter", replica)
        with pytest.raises(HTTPException) as exc:
            rl.check_login_allowed(_request(ip), email)
        assert exc.value.status_code == 429
        assert 900 <= int(exc.value.headers["Retry-After"]) <= 961
    # The lock is on the (email, IP) pair only; a success on one replica clears it for both
    rl.check_login_allowed(_request("198.51.100.1"), email)
    monkeypatch.setattr(rl, "limiter", replicas[0])
    rl.record_login_success(_request(ip), email)
    monkeypatch.setattr(rl, "limiter", replicas[1])
    rl.check_login_allowed(_request(ip), email)


def test_http_lockout_is_visible_to_another_replica(client, replicas):
    t = register_tenant(client, "shl")
    for _ in range(10):
        r = client.post("/api/auth/login", json={"email": t["email"], "password": "wrong-password"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # A second replica (own store, own engine) sees the same lockout
    pair_key = f"login-fail:{t['email'].lower()}:ip:testclient"
    assert replicas[1].blocked(pair_key, *rl.LOGIN_FAILURES_PER_EMAIL_IP) > 0
    # Rows hold a hash of the key, never the address
    assert sql("SELECT COUNT(*) FROM rate_limit_counters WHERE bucket = :b", b=bucket_id(pair_key)) == 1
    assert sql("SELECT COUNT(*) FROM rate_limit_counters WHERE bucket LIKE :e", e=f"%{t['email']}%") == 0


def test_concurrent_hits_on_two_replicas_never_pass_the_limit(replicas):
    key, admitted, lock = f"unit-concurrent:{uuid.uuid4().hex}", [], threading.Lock()

    def worker(replica):
        wait = replica.hit(key, 10, 60)
        with lock:
            admitted.append(wait == 0)

    threads = [threading.Thread(target=worker, args=(replicas[i % 2],)) for i in range(24)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(admitted) == 10
    # Refused hits were taken back: the stored count is the admitted count
    assert sql("SELECT SUM(hits) FROM rate_limit_counters WHERE bucket = :b", b=bucket_id(key)) == 10


def test_expired_rows_are_purged():
    store = DatabaseRateLimitStore()
    key = f"unit-purge:{uuid.uuid4().hex}"
    store.hit(key, 5, 60)
    assert sql("SELECT COUNT(*) FROM rate_limit_counters WHERE bucket = :b", b=bucket_id(key)) == 1
    store.purge(now=utcnow().timestamp() + 30)          # still counting
    assert sql("SELECT COUNT(*) FROM rate_limit_counters WHERE bucket = :b", b=bucket_id(key)) == 1
    assert store.purge(now=utcnow().timestamp() + 3 * 3600) >= 1
    assert sql("SELECT COUNT(*) FROM rate_limit_counters WHERE bucket = :b", b=bucket_id(key)) == 0


# ── Daily new-contact limit across processes (BR-OV-01) ──

def test_daily_limit_holds_across_two_processes(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 3)
    user = f"u-{uuid.uuid4()}"[:36]
    worker, api = daily_limit.DailyLimiter(), daily_limit.DailyLimiter()   # the worker and one API replica
    db = SimpleNamespace()
    monkeypatch.setattr(daily_limit, "sent_today", lambda _db, _user, _day=None: 0)
    assert worker.reserve(db, user) and api.reserve(db, user) and worker.reserve(db, user)
    assert not api.reserve(db, user) and not worker.reserve(db, user)     # 3 in total, not 3 each
    name = daily_limit.counter_name(user, utcnow().date())
    assert sql("SELECT value FROM platform_flags WHERE name = :n", n=name) == "3"
    api.release(user)                                                      # a send that did not go out
    assert worker.reserve(db, user) and not api.reserve(db, user)


def test_daily_counter_is_seeded_from_todays_sends_and_old_days_are_dropped(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 3)
    user = f"s-{uuid.uuid4()}"[:36]
    old = daily_limit.counter_name(user, utcnow().date() - timedelta(days=3))
    sql("INSERT INTO platform_flags (name, value) VALUES (:n, '7')", n=old)
    monkeypatch.setattr(daily_limit, "sent_today", lambda _db, _user, _day=None: 2)
    a, b = daily_limit.DailyLimiter(), daily_limit.DailyLimiter()
    assert a.reserve(None, user)                  # 2 already sent today + this one
    assert not b.reserve(None, user)
    assert sql("SELECT COUNT(*) FROM platform_flags WHERE name = :n", n=old) == 0


def test_daily_limit_falls_back_to_this_process_when_the_counter_fails(monkeypatch, caplog):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 2)
    monkeypatch.setattr(daily_limit, "sent_today", lambda _db, _user, _day=None: 0)

    def broken():
        raise ConnectionError("database down")

    lim = daily_limit.DailyLimiter(session_factory=broken)
    with caplog.at_level("ERROR"):
        assert lim.reserve(None, "u1") and lim.reserve(None, "u1")
        assert not lim.reserve(None, "u1")        # still limited, per process
        lim.release("u1")
        assert lim.reserve(None, "u1")
    assert any("shared counter unavailable" in r.getMessage() for r in caplog.records)
