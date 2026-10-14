# Outreach 360 backend architecture

FastAPI + SQLAlchemy 2 (synchronous sessions) + MySQL 8. One code base runs as three processes:

| Process | Entry point | What it does |
|---|---|---|
| API | `uvicorn app.main:app` | HTTP API for the web app, SES/SNS webhooks, tracking pixels and links |
| Worker | `python -m app.worker` | Background loops from `app/jobs.py` (scheduler, IMAP sync, warmup, webhooks, ...) and a small health server on `WORKER_HEALTH_PORT` |
| Migrate | `python -m app.db.migrate` | `alembic upgrade head` under a MySQL named lock, then idempotent reference data |

Decisions behind this layout are recorded in [docs/adr/](adr/). For testing, see [docs/TESTING.md](TESTING.md).

## 1. Layers

```
app/routers/   HTTP: request parsing, auth dependencies, response shaping        (thin)
app/services/  business rules, permissions on records, all DB reads and writes   (owns logic)
app/models/    SQLAlchemy ORM tables                                             (no logic beyond helpers)
app/schemas/   Pydantic request/response models                                  (API contract)
app/core/      cross-cutting: config, DB engine/session, auth, errors, rate limit, observability
app/utils/     pure helpers with no request context (e-mail parsing, business calendar, retries)
app/jobs.py    background loops; app/worker.py runs them
app/db/        migration runner, legacy schema patches used by the baseline revision, seed scripts
```

Rules:

- **Routers stay thin.** A router function declares its dependencies (`get_db`, `get_current_user`,
  `require_role`, `require_permission`), validates input through a schema, calls one or a few service
  functions, commits, and maps the result to a response. Logic that is reused by another router, the
  worker or a test belongs in a service.
- **Services own the logic and the database.** They take a `Session` (and usually the current `User`)
  as arguments, query through the visibility helpers (section 2), and raise domain errors (section 5),
  never `fastapi.HTTPException`. Services do not import from `app.routers`.
- **Models** declare tables and relationships. Import `app.models` once before the first query (the
  API, worker and jobs do) so every relationship resolves.
- **Schemas** are the public API contract. The OpenAPI document generated from routers + schemas is
  compared against a baseline in reviews; a change to it is an API change.
- **core/utils** never import services or routers.

## 2. Multi-tenancy and the sales hierarchy

Every business row carries `tenant_id` (a workspace). There is no row-level security in MySQL, so
isolation is enforced in code at these points:

| Enforcement point | Where | Rule |
|---|---|---|
| Authentication | `app/core/auth.get_current_user` | JWT (Bearer header or httpOnly cookie + CSRF header). Loads the `User`; checks the workspace status and, where the workspace requires MFA, limits a user without it to the MFA setup routes. `PLATFORM_ADMIN` has `tenant_id = NULL` and may only use platform routes. |
| Role / permission | `require_role(...)`, `require_permission(...)` in `app/core/auth.py` | Route-level gates. `require_permission` uses `custom_permissions` or the role defaults (`_DEFAULT_PERMS`). |
| Tenant filter | every service query | `Model.tenant_id == user.tenant_id`; a record from another tenant is reported as not found (404), never as forbidden, so ids do not leak. |
| Sales hierarchy (BR-SH-02) | `app/services/contact_service.py`: `visible_user_ids`, `owner_clause`, `scope`, `can_see_owner`, `can_access_contact` | `sees_everything`: SUPER_ADMIN/ADMIN, sales level 1, and users outside the hierarchy with `manage_prospects` see the whole workspace; everyone else sees their own records and those of the people below them. List queries go through `scope(query, db, user, Model.owner_id)`; single records through `get_contact` / `sales.get_lead` / `sales.get_opp`. |
| Money fields | `app/services/sales_settings.masked_for` | Amounts are blanked for roles that may not see them. |
| Sending domains | `app/services/domain_tenancy.py` | A domain row belongs to one tenant; e-mail has no `tenant_id`, so per-domain figures are derived from the mailbox (`inbox_id`) or campaign. |

New code that reads tenant data must use these helpers rather than hand-written filters.

## 3. Background work

`app/jobs.py` defines the loops (`all_jobs()`): `scheduler` (30 s), `imap_sync`, `deliverability`,
`warmup`, `crm_purge`, `send_safety`, `sales_jobs`, `contact_scoring`, `webhooks` (15 s).

- Each iteration first takes a **MySQL named lock** `GET_LOCK('vector:job:<name>:<database>', 0)` on its
  own connection (NullPool engine). If another process holds it, the iteration is skipped. The API
  (`RUN_BACKGROUND_JOBS=true`, the local default) and any number of worker replicas can therefore run
  the loops without double work. Deployments set `RUN_BACKGROUND_JOBS=false` on the API.
- The e-mail scheduler additionally claims each message with a unique `send_key` before sending, a
  second guard against double sends.
- Heartbeats (`HEARTBEATS`) feed the worker's `/health/ready` (a loop is stale after 3x its interval)
  and Prometheus metrics.
- Blocking work (DB, SMTP, IMAP, HTTP) runs in a thread (`_run_in_thread`) so the event loop stays free.

See [ADR 0004](adr/0004-background-jobs-named-locks.md) and, for outbound webhooks,
[ADR 0005](adr/0005-webhook-outbox.md).

## 4. Database migrations

- Alembic (`alembic/versions/`) is the only source of schema changes. `0001_baseline` builds the full
  schema idempotently on an empty database or on one created by an older build
  (`create_all()` + `app/db/schema_patches.apply_all()`); later revisions are ordinary migrations.
- Migrations run once per deploy via `python -m app.db.migrate` (compose `migrate` service, Kubernetes
  initContainer). A named lock (`vector:migrate`) serialises concurrent runs. `AUTO_MIGRATE=true` runs
  them in the API lifespan for local convenience only.
- Never change the schema from application start-up, and never edit an applied revision: add a new one.
  `app/db/*_schema.py` modules exist only for the baseline revision.
- The functional test layer builds its schema with the same migrations, so a broken migration fails CI.

## 5. Error handling

| Layer | Raises | Becomes |
|---|---|---|
| Services | domain errors from `app/core/errors.py` | the handler registered in `app/main.py` turns them into `{"detail": <message or structured payload>}` with the class's status code, the same body an `HTTPException` produces |
| Routers | `HTTPException` for HTTP-level concerns (auth, malformed input), or let domain errors pass | unchanged FastAPI behaviour |
| Request validation | Pydantic | 422 `{"success": false, "error": "Validation Error", "detail": [...], "code": "VALIDATION_ERROR"}` |
| Database | `IntegrityError` / `DataError` not handled closer | 409 / 422 handlers in `app/main.py` |
| Anything else | | 500 `{"success": false, "error": "An unexpected system error occurred", ...}` (`app/core/exceptions.py`); the traceback goes to the log only |

Domain error classes: `BadRequestError` 400, `PermissionDenied` 403, `NotFoundError` 404,
`ConflictError` 409, `GoneError` 410, `ValidationFailed` 422, `TooManyRequests` 429 (`Retry-After`),
`OperationFailed` 500, `ExternalServiceUnavailable` 503. Each carries a message, a machine-readable
`code` (for logs and tests, not sent to clients) and an optional structured `detail` payload.
`app/utils/error_utils.handle_route_error` re-raises `HTTPException` and `DomainError` unchanged and
maps other known exceptions to status codes.

Broad `except Exception` is allowed only as a deliberate safety net around external calls
(SMTP/IMAP/SES/OpenAI/HTTP feeds) or optional work that must not fail the request; it must log with
context (which mailbox, tenant, provider) and never swallow silently. See
[ADR 0001](adr/0001-layering-and-domain-errors.md).

## 6. OpenAI spend cap

All OpenAI calls go through the client wrapper in `app/services/sender_identity.py`, which calls
`app/services/llm_budget.py`:

- before each call the worst-case cost (pessimistic prompt-token estimate + clamped max output tokens)
  is reserved atomically in `platform_flags` (micro-dollars per UTC day, shared by API and worker);
- if the reservation would exceed `OPENAI_MAX_USD_PER_DAY` (default $0.20) or
  `OPENAI_MAX_CALLS_PER_DAY`, the call is refused with `LLMBudgetExceeded` and the caller falls back to
  the built-in templates exactly as when no API key is configured;
- after the call the reservation is replaced by the real cost from the response's usage.

`OPENAI_MAX_USD_PER_DAY=0` blocks every call. Tests never call OpenAI.

## 7. Cross-cutting

- **Rate limiting**: `app/core/rate_limit.py`, an in-process sliding window (per IP, per e-mail for
  sign-in/reset/MFA). Correct for one API replica; see [ADR 0002](adr/0002-single-process-api-in-memory-rate-limiter.md).
- **Observability**: `app/core/observability.py` adds `X-Request-ID` to every request and log line,
  Prometheus metrics at `/metrics` (API) and on the worker, and optional Sentry (`SENTRY_DSN`) with
  request bodies and secrets scrubbed.
- **Session limits**: refresh is refused after `SESSION_IDLE_MINUTES` (30) without user activity
  (reported by the SPA in `X-User-Activity`; polling does not count) or `SESSION_ABSOLUTE_HOURS` (12)
  after sign-in, and replaying a rotated refresh token revokes the whole session. Enforced at refresh,
  so within one access-token lifetime; see [ADR 0007](adr/0007-session-timeouts.md).
- **Secrets**: `app/core/secrets_guard.py` refuses to start a deployed environment without
  `JWT_SECRET_KEY` and `CREDENTIALS_ENCRYPTION_KEY`; mailbox passwords and OAuth tokens are encrypted
  at rest (`app/core/encrypted_type.py`).
- **Logging**: module loggers (`logging.getLogger(__name__)`), never `print()`. CLI scripts under
  `app/db/` configure logging in their `__main__` block.
- **Contact search**: see [ADR 0003](adr/0003-contact-search-strategy.md).
- **Static typing**: `mypy.ini` checks `app/core`, `app/services` and `app/utils` in CI;
  `app/core` must be fully annotated. ORM models are treated as `Any` until they move to `Mapped[...]`.

## 8. Known debt

- ORM models use classic `Column(...)` declarations; moving them to `Mapped[...]` would let mypy check
  attribute types in services.
- Some routers still contain business logic and DB queries that predate the layering rule; move it into
  services when the router is next changed.
