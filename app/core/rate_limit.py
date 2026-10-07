# app/core/rate_limit.py
"""
In-memory rate limiting (BR-DF-09). slowapi isn't available on every package index this
project builds from, so this is a small sliding-window limiter with no dependencies.

- Sign-in: failed passwords are counted per email (brute force on one account) and all
  attempts per client IP; password-reset and register requests are limited the same way.
- Everything else: a generous per-client-IP ceiling (RateLimitMiddleware).

Counters live in the API process, which is enough for the single-replica deployment. With
several replicas, move them to Redis so limits are shared.
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class SlidingWindowLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, window, now):
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

    def reset(self, key: str):
        with self._lock:
            self._hits.pop(key, None)


limiter = SlidingWindowLimiter()

LOGIN_FAILURES_PER_EMAIL = (10, 15 * 60)   # 10 wrong passwords per 15 min
LOGIN_ATTEMPTS_PER_IP = (60, 5 * 60)
EMAIL_REQUESTS_PER_EMAIL = (5, 15 * 60)    # reset links / magic links
EMAIL_REQUESTS_PER_IP = (30, 15 * 60)
REGISTER_PER_IP = (10, 60 * 60)
GLOBAL_PER_IP = (1200, 60)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _too_many(retry_after: int, message: str):
    raise HTTPException(status_code=429, detail=message, headers={"Retry-After": str(retry_after)})


def check(request: Request, scope: str, ip_limit: tuple, email: str = None, email_limit: tuple = None):
    """Count one request for this scope; raise 429 when the IP or email is over its limit."""
    wait = limiter.hit(f"{scope}:ip:{client_ip(request)}", *ip_limit)
    if wait:
        _too_many(wait, "Too many requests. Please wait a few minutes and try again.")
    if email and email_limit:
        wait = limiter.hit(f"{scope}:email:{email.strip().lower()}", *email_limit)
        if wait:
            _too_many(wait, "Too many requests for this email. Please wait a few minutes and try again.")


def check_login_allowed(request: Request, email: str):
    check(request, "login", LOGIN_ATTEMPTS_PER_IP)
    wait = limiter.blocked(f"login-fail:{email.strip().lower()}", *LOGIN_FAILURES_PER_EMAIL)
    if wait:
        _too_many(wait, "Too many failed sign-in attempts. Please wait 15 minutes or reset your password.")


def record_login_failure(email: str):
    limiter.hit(f"login-fail:{email.strip().lower()}", *LOGIN_FAILURES_PER_EMAIL)


def record_login_success(email: str):
    limiter.reset(f"login-fail:{email.strip().lower()}")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP ceiling on all API traffic. Exempt: health checks, the SES webhook and email tracking
    (mail providers such as Gmail fetch open pixels for many recipients from a few IPs)."""

    EXEMPT = ("/health", "/webhooks/ses", "/tracking", "/api/tracking")

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith(self.EXEMPT):
            wait = limiter.hit(f"global:{client_ip(request)}", *GLOBAL_PER_IP)
            if wait:
                return JSONResponse({"detail": "Too many requests. Please slow down."},
                                    status_code=429, headers={"Retry-After": str(wait)})
        return await call_next(request)
