# ADR 0002: Single API process with an in-memory rate limiter

Status: Accepted (2026-10); counters amended 2026-11 (DEC-09, see the end)

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

## Amendment (2026-11, DEC-09): shared counters in MySQL

The manifests run 2 API replicas, so the per-process counters doubled every limit (sign-in lockout 20
wrong passwords instead of 10). Redis would be new infrastructure; MySQL is already shared by every
process, so the counters moved there instead:

- `rate_limit_counters` (alembic `0037_rate_limit_counters`): one row per (bucket, slot), `bucket` =
  SHA-256 of the limiter key (no e-mail or IP stored). A window is cut into 15 slots
  (`rate_limit.slot_seconds`). A hit is one `INSERT ... ON DUPLICATE KEY UPDATE hits = hits + 1` on the
  current slot plus a read of the live slots, in one short READ COMMITTED transaction on its own pooled
  connection; over the limit the hit is taken back in the same transaction. The row lock serialises hits
  on a bucket, so replicas together never admit more than the limit
  (`tests/test_shared_limits.py::test_concurrent_hits_on_two_replicas_never_pass_the_limit`).
- Store behind `app.core.rate_limit.RateLimitStore` (`hit`, `blocked`, `reset`, `clear`): the MySQL
  implementation is `app/services/rate_limit_store.py` (SQL in `app/repositories/rate_limits.py`),
  installed by `app/main.py` with `rate_limit.configure`. Callers, limits, windows, `Retry-After`, the
  (email, IP) lockout keys, `RATE_LIMIT_TEST_MULTIPLIER` and `X-Test-Default-Rate-Limits` are unchanged.
- Window precision: a hit counts while its slot overlaps the window, i.e. up to 1/15 of the window
  longer than an exact sliding window (a 15-minute lockout lasts 15-16 minutes), never shorter.
- Database unreachable: `FallbackLimiter` uses this process's in-memory `SlidingWindowLimiter` (limits
  per process, the pre-amendment behaviour) and logs an ERROR at most once a minute. Not fail-open, and
  not fail-closed either: sign-in needs the database anyway, so refusing every attempt would add nothing.
- Expired rows are deleted by the store itself, at most every 5 minutes per API process.
- Not moved: the global per-IP ceiling (`GLOBAL_PER_IP`, `RateLimitMiddleware`). It runs on every request;
  a database write per request would turn a flood into database load. Its effective value stays
  `GLOBAL_PER_IP x API replicas` ([OPERATIONS.md](../OPERATIONS.md) section 6).
- Counters now survive an API restart (a restart no longer clears a lockout early). The system-test
  runner's `--restart-api-between` also empties `rate_limit_counters`.
