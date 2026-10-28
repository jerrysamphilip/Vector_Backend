"""In-memory sliding-window limiter and client-IP resolution (app/core/rate_limit.py, BR-DF-09).

The limits asserted here are also the constraints the system/UI test runner plans around
(scripts/test.py: 60 logins per 5 min and 10 sign-ups per hour per client IP).
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import rate_limit as rl
from app.core.config import settings


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rl, "time", SimpleNamespace(monotonic=c.monotonic))
    return c


def test_documented_limits():
    assert rl.LOGIN_ATTEMPTS_PER_IP == (60, 300)
    assert rl.REGISTER_PER_IP == (10, 3600)
    assert rl.LOGIN_FAILURES_PER_EMAIL_IP == (10, 900)
    assert rl.LOGIN_FAILURES_PER_EMAIL == (50, 900)
    assert rl.EMAIL_REQUESTS_PER_EMAIL == (5, 900)


def test_hits_under_the_limit_pass_then_wait_is_reported(clock):
    lim = rl.SlidingWindowLimiter()
    assert [lim.hit("k", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert lim.hit("k", 3, 60) == 61


def test_window_slides(clock):
    lim = rl.SlidingWindowLimiter()
    lim.hit("k", 2, 60)
    clock.now += 30
    lim.hit("k", 2, 60)
    assert lim.hit("k", 2, 60) == 31            # oldest hit leaves the window in 30 s
    clock.now += 30                             # first hit is now exactly one window old
    assert lim.hit("k", 2, 60) == 0


def test_blocked_does_not_record_hits(clock):
    lim = rl.SlidingWindowLimiter()
    lim.hit("k", 2, 60)
    assert lim.blocked("k", 2, 60) == 0
    assert lim.blocked("k", 2, 60) == 0
    lim.hit("k", 2, 60)
    assert lim.blocked("k", 2, 60) == 61


def test_keys_are_independent_and_reset_clears(clock):
    lim = rl.SlidingWindowLimiter()
    lim.hit("a", 1, 60)
    assert lim.hit("b", 1, 60) == 0
    assert lim.hit("a", 1, 60) > 0
    lim.reset("a")
    assert lim.hit("a", 1, 60) == 0
    lim.reset("never-used")                     # no error


def _request(xff=None, host="9.9.9.9"):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host) if host else None)


@pytest.mark.parametrize("hops,expected", [(1, "2.2.2.2"), (2, "1.1.1.1"), (5, "1.1.1.1"), (0, "2.2.2.2")])
def test_client_ip_takes_the_trusted_hop_from_the_right(monkeypatch, hops, expected):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", hops)
    assert rl.client_ip(_request("1.1.1.1, 2.2.2.2")) == expected


def test_client_ip_without_forwarding_header(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    assert rl.client_ip(_request()) == "9.9.9.9"
    assert rl.client_ip(_request(host=None)) == "unknown"
    assert rl.client_ip(_request(" , ")) == "9.9.9.9"


def test_check_raises_429_with_retry_after(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    req = _request("7.7.7.7")
    rl.check(req, "unit", (1, 60))
    with pytest.raises(HTTPException) as exc:
        rl.check(req, "unit", (1, 60))
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "61"


def test_check_limits_per_email_case_insensitively(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    rl.check(_request(host="1.0.0.1"), "unit", (100, 60), "Pat@X.example", (1, 60))
    with pytest.raises(HTTPException):
        rl.check(_request(host="1.0.0.2"), "unit", (100, 60), " pat@x.example ", (1, 60))


# ── Sign-in lockout keyed on (email, IP) with a per-email backstop ──

def _fail(n, host, email="victim@x.example"):
    for _ in range(n):
        rl.check_login_allowed(_request(host=host), email)
        rl.record_login_failure(_request(host=host), email)


def test_wrong_passwords_lock_only_that_email_ip_pair(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    _fail(10, "6.6.6.6")
    with pytest.raises(HTTPException) as exc:
        rl.check_login_allowed(_request(host="6.6.6.6"), "Victim@X.example")
    assert exc.value.status_code == 429
    rl.check_login_allowed(_request(host="1.2.3.4"), "victim@x.example")   # the real user elsewhere is not locked
    rl.check_login_allowed(_request(host="6.6.6.6"), "other@x.example")    # nor another email from that IP


def test_per_email_backstop_across_ips(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    for i in range(5):
        _fail(10, f"10.0.0.{i}")
    with pytest.raises(HTTPException):
        rl.check_login_allowed(_request(host="10.9.9.9"), "victim@x.example")
    clock.now += 901
    rl.check_login_allowed(_request(host="10.9.9.9"), "victim@x.example")


def test_success_clears_the_pair_only(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    _fail(9, "6.6.6.6")
    rl.record_login_success(_request(host="6.6.6.6"), "victim@x.example")
    _fail(9, "6.6.6.6")                                                     # pair counter started again
    rl.check_login_allowed(_request(host="6.6.6.6"), "victim@x.example")
    assert rl.limiter.blocked("login-fail:victim@x.example", 18, 900) > 0   # backstop kept all 18


# ── RATE_LIMIT_TEST_MULTIPLIER (test stacks only) ──

def test_multiplier_defaults_to_one():
    assert settings.RATE_LIMIT_TEST_MULTIPLIER == 1
    assert rl.ip_limit_multiplier() == 1


def test_multiplier_relaxes_per_ip_limits(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", 3)
    req = _request(host="5.5.5.5")
    for _ in range(3):
        rl.check(req, "unit", (1, 60))
    with pytest.raises(HTTPException) as exc:
        rl.check(req, "unit", (1, 60))
    assert exc.value.status_code == 429


def test_multiplier_never_relaxes_per_email_limits(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", 100)
    rl.check(_request(host="1.0.0.1"), "unit", (1, 60), "pat@x.example", (1, 60))
    with pytest.raises(HTTPException):
        rl.check(_request(host="1.0.0.2"), "unit", (1, 60), "pat@x.example", (1, 60))


def test_multiplier_keeps_the_sign_in_lockout(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", 100)
    _fail(10, "6.6.6.6")
    with pytest.raises(HTTPException) as exc:
        rl.check_login_allowed(_request(host="6.6.6.6"), "victim@x.example")
    assert exc.value.status_code == 429
    assert 800 <= int(exc.value.headers["Retry-After"]) <= 901


def test_relaxed_login_ip_limit_and_default_limits_header(monkeypatch, clock):
    """Relaxed: 61 attempts pass. With the header the real 60 / 5 min applies, in a bucket of its own."""
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", 100)
    plain = _request(host="8.8.8.8")
    for i in range(61):
        rl.check_login_allowed(plain, f"n{i}@x.example")
    strict = SimpleNamespace(headers={rl.DEFAULT_LIMITS_HEADER: "1"}, client=SimpleNamespace(host="8.8.8.8"))
    for i in range(60):
        rl.check_login_allowed(strict, f"s{i}@x.example")
    with pytest.raises(HTTPException) as exc:
        rl.check_login_allowed(strict, "s-last@x.example")
    assert exc.value.status_code == 429
    rl.check_login_allowed(plain, "after@x.example")   # the relaxed bucket is separate


def test_default_limits_header_changes_nothing_without_the_multiplier(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    strict = SimpleNamespace(headers={rl.DEFAULT_LIMITS_HEADER: "1"}, client=SimpleNamespace(host="4.4.4.4"))
    rl.check(strict, "unit", (1, 60))
    with pytest.raises(HTTPException):
        rl.check(_request(host="4.4.4.4"), "unit", (1, 60))   # same bucket as a request without the header


@pytest.mark.parametrize("environment", ["production", "prod"])
def test_multiplier_is_ignored_in_production(monkeypatch, clock, environment):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", 100)
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    assert rl.ip_limit_multiplier() == 1
    rl.check(_request(host="3.3.3.3"), "unit", (1, 60))
    with pytest.raises(HTTPException):
        rl.check(_request(host="3.3.3.3"), "unit", (1, 60))


@pytest.mark.parametrize("value", [0, -5, None])
def test_multiplier_below_one_counts_as_one(monkeypatch, value):
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", value)
    assert rl.ip_limit_multiplier() == 1


def test_global_ceiling_is_relaxed_too(monkeypatch, clock):
    monkeypatch.setattr(rl, "limiter", rl.SlidingWindowLimiter())
    monkeypatch.setattr(settings, "RATE_LIMIT_TEST_MULTIPLIER", 2)
    req = _request(host="2.2.2.2")
    key, limit = rl._ip_bucket(req, "global:2.2.2.2", rl.GLOBAL_PER_IP)
    assert (key, limit) == ("global:2.2.2.2", (rl.GLOBAL_PER_IP[0] * 2, rl.GLOBAL_PER_IP[1]))
