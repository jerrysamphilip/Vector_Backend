# ADR 0002: Single API process with an in-memory rate limiter

Status: Accepted (2026-10)

## Context

Sign-in, password reset, magic links, registration and MFA need brute-force protection (BR-DF-09), and
the API needs a per-client ceiling. slowapi is not available on every package index the project builds
from, and the current deployment runs one API replica next to a separate worker.

## Decision

- `app/core/rate_limit.py` implements a dependency-free sliding-window limiter held in the API process:
  per IP for every request (`RateLimitMiddleware`, `GLOBAL_PER_IP`), and per e-mail / per IP / per
  pending MFA sign-in for the authentication endpoints. Over the limit the API returns 429 with
  `Retry-After`.
- The client IP is the `TRUSTED_PROXY_HOPS`-th `X-Forwarded-For` entry from the right (left-most
  entries are client-controlled).
- Health checks, the SES webhook and tracking pixels/links are exempt (mail providers fetch them from
  few IPs).
- slowapi is wired in `app/main.py` only when it happens to be installed.

## Consequences

- Limits are exact for one API replica and reset when the process restarts (acceptable for
  brute-force throttling).
- With N replicas behind a load balancer each replica counts separately, so the effective limit becomes
  up to N times higher. **Before running more than one API replica**, move the counters to Redis: keep
  the `SlidingWindowLimiter` interface (`hit`, `blocked`, `reset`) and back it with a sorted set per key
  (`ZADD now`, `ZREMRANGEBYSCORE` older than the window, `ZCARD`, `EXPIRE window`) in one
  `MULTI`/Lua call, selected by a `RATE_LIMIT_BACKEND=redis` setting. No caller has to change.
- Background work is already safe across replicas (ADR 0004); the rate limiter is the only in-process
  state that blocks horizontal scaling of the API.
