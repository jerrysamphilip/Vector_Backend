# ADR 0001: Layering and domain errors

Status: Accepted (2026-10)

## Context

Routers had grown to several thousand lines that mixed HTTP handling, business rules and SQL. Services
raised `fastapi.HTTPException`, so they could not be reused cleanly from the worker or tested without
the web framework, and error handling differed from file to file (silent `except Exception: pass`,
`print()` instead of logging).

## Decision

- Layers: `routers` (HTTP only) -> `services` (business rules, record-level permissions, all DB access)
  -> `models`; `schemas` define the API contract; `core`/`utils` are shared infrastructure that never
  import services or routers.
- Services raise domain errors from `app/core/errors.py`: `DomainError(message, code=..., detail=...)`
  with subclasses per HTTP outcome (`BadRequestError` 400, `PermissionDenied` 403, `NotFoundError` 404,
  `ConflictError` 409, `GoneError` 410, `ValidationFailed` 422, `TooManyRequests` 429,
  `OperationFailed` 500, `ExternalServiceUnavailable` 503).
- One handler, registered in `app/main.py`, maps them to `{"detail": <message or structured payload>}`,
  exactly the body `HTTPException` produced, so the move from `HTTPException` to domain errors is not an
  API change (verified by diffing the OpenAPI document and by the functional tests).
- `handle_route_error` (`app/utils/error_utils.py`) passes domain errors through unchanged.
- Broad exception handlers are allowed only around external calls or optional work, and must log with
  context.

## Consequences

- Services are framework-free and can be called from jobs, scripts and unit tests; tests assert on the
  error class and `code` instead of HTTP plumbing.
- A router that catches `HTTPException` around a service call must also catch `DomainError` (or let it
  propagate); grep for `except HTTPException` when moving code into services.
- `code` is not in the response body (the body stays backward compatible). Exposing it to clients would
  be a deliberate API change.
