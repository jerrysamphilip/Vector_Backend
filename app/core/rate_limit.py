# app/core/rate_limit.py
"""
Rate limiting (BR-DF-09): a small sliding-window limiter with no dependencies (slowapi isn't available on
every package index this project builds from).

- Sign-in: failed passwords are counted per (email, client IP) pair, which locks only that pair,
  plus a higher per-email backstop across all IPs; all attempts are also counted per client IP.
  Password-reset and register requests are limited per IP / per email.
- Everything else: a generous per-client-IP ceiling (RateLimitMiddleware).
- Test stacks only: RATE_LIMIT_TEST_MULTIPLIER > 1 raises the per-client-IP limits (login attempts,
  sign-ups, reset/magic-link requests, two-factor attempts, the global ceiling) so the live test suites
  can share one client IP without restarting the API. Per-email and per-(email, IP) limits (the
  account lockouts) never change. A request carrying ``X-Test-Default-Rate-Limits: 1`` is counted
  against the real limits in a separate bucket, so the rate-limit tests still see them. The setting is
  refused at start-up in production (app/core/config.py) and ignored there as well.

Where the counters live (DEC-09, ADR 0002): the API runs several replicas, so every limit above is kept in
MySQL (``rate_limit_counters``; app/services/rate_limit_store.py, installed by app.main with ``configure``)
and is shared by all processes: the 10-wrong-password lockout is 10 in total, not 10 per replica. The table
holds one row per bucket and time slot (a window is cut into about 15 slots, see ``slot_seconds``), so a
hit is one ``INSERT ... ON DUPLICATE KEY UPDATE`` and a short read of the bucket's live slots. A hit is
counted while its slot overlaps the window, i.e. up to one slot (1/15 of the window) longer than an exact
sliding window: never shorter. When the database cannot be reached, the limiter falls back to the
in-memory counters of this process (the pre-DEC-09 behaviour: limits per process) and logs an ERROR at
most once a minute; it never lets requests through unlimited.

Not shared on purpose: the global per-IP ceiling (RateLimitMiddleware) stays in process memory. It is a
coarse flood guard on every request; a database write per request would turn a flood into database load.
Its effective value is GLOBAL_PER_IP x API replicas (docs/OPERATIONS.md section 6).

Processes that never call ``configure`` (unit tests, scripts) use the in-memory limiter.
"""

import logging
import math
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, NoReturn, Optional, Protocol, Sequence, Tuple

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.core.config import settings

logger = logging.getLogger("app.rate_limit")


class RateLimitStore(Protocol):
    """Counters behind the limits. ``hit`` records one hit unless the key is at its limit and returns the
    seconds to wait (0 = allowed); ``blocked`` only reads; ``reset`` forgets the key; ``clear`` forgets all."""

    def hit(self, key: str, limit: int, window: int) -> int: ...

    def blocked(self, key: str, limit: int, window: int) -> int: ...

    def reset(self, key: str) -> None: ...

    def clear(self) -> None: ...


class SlidingWindowLimiter:
    """In-memory store (per process): exact sliding window over monotonic timestamps."""

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, window: float, now: float) -> Deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        return hits

    def hit(self, key: str, limit: int, window: int) -> int:
        """Record a hit; return seconds to wait if over the limit, else 0."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, window, now)
            if len(hits) >= limit:
                return int(hits[0] + window - now) + 1
            hits.append(now)
            if len(self._hits) > 50_000:  # bound memory under a key-spraying attack
                for stale in [k for k, v in self._hits.items() if not v][:10_000]:
                    del self._hits[stale]
            return 0

    def blocked(self, key: str, limit: int, window: int) -> int:
        """Seconds to wait if the key is already at its limit (doesn't record a hit)."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, window, now)
            return int(hits[0] + window - now) + 1 if len(hits) >= limit else 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


# ── Slot math of the shared (database) store: pure, unit tested in tests/unit/test_rate_limit.py ──

SLOTS_PER_WINDOW = 15


def slot_seconds(window: int) -> int:
    """Length of one counter slot for a window (at least one second)."""
    return max(1, int(window) // SLOTS_PER_WINDOW)


def slot_start(now: float, window: int) -> int:
    """Start (epoch seconds) of the slot ``now`` falls in."""
    size = slot_seconds(window)
    return int(now // size) * size


def live_after(now: float, window: int) -> float:
    """A slot still counts while it starts after this: part of it is inside the window."""
    return now - window - slot_seconds(window)


def slot_expiry(start: int, window: int) -> int:
    """When a slot stops counting (and its row may be deleted)."""
    return start + slot_seconds(window) + int(window)


def retry_after(slots: Sequence[Tuple[int, int]], limit: int, window: int, now: float) -> int:
    """Seconds until fewer than ``limit`` hits are live, given (slot start, hits) pairs; 0 when already
    below the limit. Slots leave oldest first, each at ``slot_expiry``."""
    live = sorted((start, hits) for start, hits in slots if slot_expiry(start, window) > now and hits > 0)
    excess = sum(hits for _, hits in live) - limit + 1
    if excess <= 0:
        return 0
    for start, hits in live:
        excess -= hits
        if excess <= 0:
            return max(1, math.ceil(slot_expiry(start, window) - now))
    return max(1, math.ceil(slot_expiry(live[-1][0], window) - now)) if live else 1  # limit < 1


class FallbackLimiter:
    """The shared store, with this process's in-memory counters when the store fails (logged, never silent)."""

    LOG_EVERY_SECONDS = 60.0

    def __init__(self, primary: RateLimitStore, fallback: Optional[SlidingWindowLimiter] = None) -> None:
        self.primary = primary
        self.fallback = fallback or SlidingWindowLimiter()
        self._last_log = -math.inf

    def _degraded(self, exc: Exception) -> SlidingWindowLimiter:
        now = time.monotonic()
        if now - self._last_log >= self.LOG_EVERY_SECONDS:
            self._last_log = now
            logger.error("Rate-limit store unavailable (%s): using this process's in-memory counters "
                         "(limits per process until the database is back)", type(exc).__name__)
        return self.fallback

    def hit(self, key: str, limit: int, window: int) -> int:
        try:
            return self.primary.hit(key, limit, window)
        except Exception as exc:  # noqa: BLE001 - any store failure degrades to per-process limits
            return self._degraded(exc).hit(key, limit, window)

    def blocked(self, key: str, limit: int, window: int) -> int:
        try:
            return self.primary.blocked(key, limit, window)
        except Exception as exc:  # noqa: BLE001
            return self._degraded(exc).blocked(key, limit, window)

    def reset(self, key: str) -> None:
        self.fallback.reset(key)
        try:
            self.primary.reset(key)
        except Exception as exc:  # noqa: BLE001
            self._degraded(exc)

    def clear(self) -> None:
        self.fallback.clear()
        self.primary.clear()


limiter: RateLimitStore = SlidingWindowLimiter()
# The global per-IP ceiling: per process on purpose (see the module docstring)
ceiling_limiter = SlidingWindowLimiter()


def configure(store: RateLimitStore) -> None:
    """Install the shared store (app.main: the MySQL counters) behind the sign-in, reset, register and
    two-factor limits, with the in-memory counters as the fallback."""
    global limiter
    limiter = FallbackLimiter(store)


def clear_all() -> None:
    """Forget every counter (functional tests and the system-test runner between suites)."""
    limiter.clear()
    ceiling_limiter.clear()

LOGIN_FAILURES_PER_EMAIL_IP = (10, 15 * 60)   # 10 wrong passwords per 15 min for one email from one IP
LOGIN_FAILURES_PER_EMAIL = (50, 15 * 60)      # backstop: 50 per 15 min for one email across all IPs
LOGIN_ATTEMPTS_PER_IP = (60, 5 * 60)
EMAIL_REQUESTS_PER_EMAIL = (5, 15 * 60)    # reset links / magic links
EMAIL_REQUESTS_PER_IP = (30, 15 * 60)
REGISTER_PER_IP = (10, 60 * 60)
GLOBAL_PER_IP = (1200, 60)
# Two-factor codes: attempts per IP; wrong codes per pending sign-in (then sign in again) and per user
MFA_ATTEMPTS_PER_IP = (30, 5 * 60)
MFA_FAILURES_PER_TOKEN = (5, 10 * 60)
MFA_FAILURES_PER_USER = (10, 15 * 60)


def client_ip(request: Request) -> str:
    """Client IP for rate limiting. The left-most X-Forwarded-For entries are client-controlled,
    so take the hop appended by our own proxy: the TRUSTED_PROXY_HOPS-th entry from the right."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        trusted = max(1, int(settings.TRUSTED_PROXY_HOPS or 1))
        if hops:
            return hops[-trusted] if len(hops) >= trusted else hops[0]
    return request.client.host if request.client else "unknown"


DEFAULT_LIMITS_HEADER = "x-test-default-rate-limits"


def ip_limit_multiplier() -> int:
    """Factor applied to the per-client-IP limits: RATE_LIMIT_TEST_MULTIPLIER on a test stack, 1 in production."""
    if settings.is_production:
        return 1
    try:
        return max(1, int(settings.RATE_LIMIT_TEST_MULTIPLIER or 1))
    except (TypeError, ValueError):
        return 1


def _ip_bucket(request: Request, key: str, limit: tuple) -> tuple[str, tuple]:
    """(key, limit) for a per-client-IP counter. Relaxed on a test stack unless the request asks for the
    real limits (DEFAULT_LIMITS_HEADER), which are then counted in their own bucket."""
    factor = ip_limit_multiplier()
    if factor == 1:
        return key, limit
    if request.headers.get(DEFAULT_LIMITS_HEADER) == "1":
        return f"default-limits:{key}", limit
    return key, (limit[0] * factor, limit[1])


def _too_many(retry_after: int, message: str) -> NoReturn:
    raise HTTPException(status_code=429, detail=message, headers={"Retry-After": str(retry_after)})


def check(request: Request, scope: str, ip_limit: tuple, email: str | None = None, email_limit: tuple | None = None) -> None:
    """Count one request for this scope; raise 429 when the IP or email is over its limit."""
    key, limit = _ip_bucket(request, f"{scope}:ip:{client_ip(request)}", ip_limit)
    wait = limiter.hit(key, *limit)
    if wait:
        _too_many(wait, "Too many requests. Please wait a few minutes and try again.")
    if email and email_limit:
        wait = limiter.hit(f"{scope}:email:{email.strip().lower()}", *email_limit)
        if wait:
            _too_many(wait, "Too many requests for this email. Please wait a few minutes and try again.")


def _login_fail_keys(request: Request, email: str) -> tuple[str, str]:
    norm = email.strip().lower()
    return f"login-fail:{norm}:ip:{client_ip(request)}", f"login-fail:{norm}"


def check_login_allowed(request: Request, email: str) -> None:
    """429 when this client IP is over its attempt limit, this (email, IP) pair has had too many wrong
    passwords, or the email has had very many from all IPs together (backstop against distributed guessing).
    Keying the lock on the pair means a stranger cannot lock a user out from another network."""
    check(request, "login", LOGIN_ATTEMPTS_PER_IP)
    pair_key, email_key = _login_fail_keys(request, email)
    wait = max(limiter.blocked(pair_key, *LOGIN_FAILURES_PER_EMAIL_IP), limiter.blocked(email_key, *LOGIN_FAILURES_PER_EMAIL))
    if wait:
        _too_many(wait, "Too many failed sign-in attempts. Please wait 15 minutes or reset your password.")


def record_login_failure(request: Request, email: str) -> None:
    pair_key, email_key = _login_fail_keys(request, email)
    limiter.hit(pair_key, *LOGIN_FAILURES_PER_EMAIL_IP)
    limiter.hit(email_key, *LOGIN_FAILURES_PER_EMAIL)


def record_login_success(request: Request, email: str) -> None:
    """Clears this (email, IP) pair; the cross-IP backstop counter keeps running out its window."""
    limiter.reset(_login_fail_keys(request, email)[0])


def check_mfa_allowed(user_id: str, mfa_jti: str | None = None) -> None:
    """429 when this user (or this pending sign-in) has had too many wrong two-factor codes."""
    if mfa_jti:
        wait = limiter.blocked(f"mfa-token-fail:{mfa_jti}", *MFA_FAILURES_PER_TOKEN)
        if wait:
            _too_many(wait, "Too many incorrect codes. Please sign in again.")
    wait = limiter.blocked(f"mfa-fail:{user_id}", *MFA_FAILURES_PER_USER)
    if wait:
        _too_many(wait, "Too many incorrect codes. Please wait 15 minutes and try again.")


def record_mfa_failure(user_id: str, mfa_jti: str | None = None) -> None:
    if mfa_jti:
        limiter.hit(f"mfa-token-fail:{mfa_jti}", *MFA_FAILURES_PER_TOKEN)
    limiter.hit(f"mfa-fail:{user_id}", *MFA_FAILURES_PER_USER)


def record_mfa_success(user_id: str) -> None:
    limiter.reset(f"mfa-fail:{user_id}")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP ceiling on all API traffic. Exempt: health checks, the SES webhook and email tracking
    (mail providers such as Gmail fetch open pixels for many recipients from a few IPs)."""

    EXEMPT = ("/health", "/webhooks/ses", "/tracking", "/api/tracking")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith(self.EXEMPT):
            key, limit = _ip_bucket(request, f"global:{client_ip(request)}", GLOBAL_PER_IP)
            wait = ceiling_limiter.hit(key, *limit)
            if wait:
                return JSONResponse({"detail": "Too many requests. Please slow down."},
                                    status_code=429, headers={"Retry-After": str(wait)})
        return await call_next(request)
