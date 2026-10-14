# ADR 0004: Background jobs coordinated by MySQL named locks

Status: Accepted (2026-10)

## Context

Sending scheduled e-mail, IMAP reply sync, warmup, deliverability sync, CRM purges, sales jobs, contact
scoring and webhook delivery run periodically. They used to start inside the API process, so every API
replica (and every uvicorn worker) ran them, risking double sends. Adding a queue system (Celery,
Redis/RQ) would add infrastructure the deployment does not otherwise need.

## Decision

- The loops live in `app/jobs.py` and run in a dedicated worker (`python -m app.worker`). The API runs
  them too only when `RUN_BACKGROUND_JOBS=true` (local default; deployments set it to false).
- Each iteration takes `GET_LOCK('vector:job:<name>:<database>', 0)` on its own NullPool connection;
  if the lock is held elsewhere the iteration is skipped and retried next interval. The lock is released
  (or dropped with the connection if the process dies) after the iteration.
- The scheduler's per-message `send_key` claim remains as a second guard against double sends.
- Heartbeats per loop drive the worker's `/health/ready` (stale after 3x the interval) and metrics.
- Migrations use the same mechanism (`vector:migrate`, waiting up to 600 s).

## Consequences

- No extra infrastructure; any number of workers (and an API with jobs on) are safe. A crashed holder
  frees the lock as soon as MySQL drops its connection.
- One iteration of a loop runs at a time across the fleet: throughput per loop is that of one process.
  Long iterations must bound their batch size (the scheduler and webhook loops return a shorter sleep
  when work remains).
- Lock names are scoped to the database name, so environments sharing a MySQL server do not block each
  other. Names are capped at 64 characters (MySQL limit).
