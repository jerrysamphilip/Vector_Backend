# ADR 0007: Session timeouts and refresh-token reuse detection

Status: Accepted (2026-10)

## Context

Browser sessions are an httpOnly access cookie (JWT, `ACCESS_TOKEN_EXPIRE_MINUTES`, 30) plus a rotating
refresh cookie (`REFRESH_TOKEN_EXPIRE_DAYS`, 7). Every refresh issued a fresh 7-day token, and the SPA
refreshes silently on any 401, so background polling (inbox sync, `refetchInterval` queries) kept an
unattended browser signed in forever. Replaying an already-rotated refresh token only got a 401.

## Decision

- A **session** is one sign-in. All refresh tokens it issues share `refresh_tokens.session_id` (the
  rotation family) and carry `session_started_at` and `last_activity_at` through rotation
  (alembic `0015_session_limits`). The logic lives in `app/services/session_limits.py`.
- **Absolute cap**: POST /api/auth/refresh is refused once `now - session_started_at >=
  SESSION_ABSOLUTE_HOURS` (default 12). A refresh token's `expires_at` never passes that point.
- **Idle timeout**: refused once `now - last_activity_at > SESSION_IDLE_MINUTES` (default 30).
  Only the user counts: the SPA (`src/api/activity.js`) records pointer / keyboard / scroll / tab-visible
  events (throttled, shared between tabs via localStorage) and sends `X-User-Activity: <seconds since
  the last interaction>` on refresh only when that is within the idle window. The server moves
  `last_activity_at` forward to `now - seconds` (never backwards, never into the future); a refresh
  without the header leaves it unchanged. Signing in sets it to now.
- **Reuse detection**: presenting a refresh token that is already revoked (rotated, signed out, ...)
  revokes every active token of its session (`revoked_reason = reuse`) and is logged as a warning.
  Exception: a token rotated less than `REFRESH_REUSE_GRACE_SECONDS` (30) ago is a benign race (two
  tabs refreshing at once); it is refused with `REFRESH_TOKEN_SUPERSEDED`, nothing is revoked and the
  cookies are left alone, and the SPA retries the request with the cookies the other tab received.
  The token row is locked (`SELECT ... FOR UPDATE`) during the exchange.
- Refusals are 401 with the session cookies cleared and `detail = {code, message}`:
  `SESSION_IDLE_TIMEOUT` ("Signed out after 30 minutes of inactivity"), `SESSION_EXPIRED` ("Session
  expired, please sign in again"), `REFRESH_TOKEN_REUSED`. Invalid / expired tokens keep the plain
  "Invalid or expired refresh token" text. The SPA redirects to `/login?reason=idle` or
  `?reason=expired`, and the login page explains why.

## Consequences

- **Granularity** (amended 2026-11): every access token is issued to expire at the earliest of
  `ACCESS_TOKEN_EXPIRE_MINUTES`, the absolute cap (`session_started_at + SESSION_ABSOLUTE_HOURS`) and the
  idle deadline as known at issue (`last_activity_at + SESSION_IDLE_MINUTES`)
  (`session_limits.access_expiry`, applied in `auth_session.issue_session`). So an idle or capped session
  gets 401 on its next API call at the limit itself, not up to one access-token lifetime later (before:
  30-60 min idle, cap + 30 min). An active user is not affected: the SPA already refreshes on any 401 and
  reports its activity then, which moves the deadline and issues a new token. Cost: no per-request work
  (the check is the token's `exp`); one extra refresh when an active user's token reaches the idle
  deadline before its normal lifetime, which with the defaults (30 / 30) happens only for refreshes made
  by background polling. Tests: `tests/test_session_limits.py::test_idle_session_gets_401_on_the_next_api_call_not_only_at_refresh`,
  `tests/unit/test_session_limits.py` (`access_expiry`).
- Activity is self-reported by the client. That is the point (it separates a person from polling),
  but a modified client can keep its own session alive up to the absolute cap, which is the hard bound.
- Reuse detection revokes refresh tokens, not the access token currently in use by the attacker or
  the victim; that one stays valid until it expires (sign-out revokes it via `revoked_access_tokens`).
- Script clients that refresh with the token in the JSON body are subject to the same limits; a
  long-running script must send `X-User-Activity: 0` on refresh or sign in again. The system test
  suites refresh right after signing in, well inside both limits.
- Rows that existed before 0015 are backfilled as one-token sessions started at their `created_at`
  and last active at migration time, so nobody is signed out by the deploy itself.
