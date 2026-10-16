# ADR 0005: Outbound webhooks through a transactional outbox

Status: Accepted (2026-10)

## Context

Other systems subscribe to events (contact/lead/deal changes, bounces, unsubscribes, replies, campaign
pauses; BR-SF-20). Calling their URLs inline would slow requests, lose events when the receiver is down,
and could announce changes that are then rolled back.

## Decision

- `app/services/webhooks.emit_event(db, tenant_id, type, data)` is the single entry point. It inserts one
  `webhook_deliveries` row per active, subscribed endpoint **in the caller's transaction**: the event
  exists if and only if the change commits.
- The worker's `webhooks` loop (every 15 s, named lock, ADR 0004) posts due rows: body signed with the
  endpoint secret (`X-Outreach-Signature: t=<ts>,v1=<HMAC-SHA256 of "<ts>.<body>">`), exponential
  backoff on failure, and the endpoint is switched off after too many consecutive failures with an
  in-app notification to the workspace admins. Old rows are purged after the retention period.
- Every request is SSRF-checked at send time: the host is resolved, all addresses must be public
  (development/test also allow private ranges and http), the connection goes to the checked address
  (no second DNS lookup, no redirects), 10 s timeout.

## Consequences

- At-least-once delivery: receivers must de-duplicate by event id. Ordering across events is not
  guaranteed.
- Delivery latency is up to one loop interval plus backoff.
- Emitting an event is a cheap insert, so services can emit freely; they must do so inside the
  transaction that makes the change.
