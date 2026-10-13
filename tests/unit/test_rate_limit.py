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
    assert rl.LOGIN_FAILURES_PER_EMAIL == (10, 900)
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
