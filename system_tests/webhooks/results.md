# webhooks results

PASS 23 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| WH-001 | RP-106b: outbound webhook subscription API is documented (OpenAPI) | PASS |  |  |
| WH-002 | Agents cannot list or register webhooks (403) | PASS |  |  |
| WH-003 | Admin registers an endpoint; the secret is returned once, reads show a hint only | PASS |  |  |
| WH-004 | Secret is encrypted at rest | PASS |  |  |
| WH-005 | SSRF / input checks: metadata, link-local, unspecified, non-http(s), credentials, bad events -> 422 | PASS |  |  |
| WH-006 | Send test: delivered synchronously, signed with the secret, logged as a test delivery | PASS |  |  |
| WH-007 | contact.created reaches the receiver via the worker, signed, with event headers | PASS |  |  |
| WH-008 | contact.updated carries the changed field (from / to) | PASS |  |  |
| WH-009 | lead.created and lead.qualified (SQL) are delivered | PASS |  |  |
| WH-010 | deal.created, deal.stage_changed and deal.won are delivered | PASS |  |  |
| WH-011 | Events the endpoint is not subscribed to are not sent (contact.deleted) | PASS |  |  |
| WH-012 | A 500 answer is recorded and retried later (attempt 1 of 6, next attempt ~1 min) | PASS |  |  |
| WH-013 | The retry succeeds once the receiver recovers (same event id) | PASS |  |  |
| WH-014 | Redeliver queues the same event again and the worker sends it | PASS |  |  |
| WH-015 | After 20 consecutive failures the endpoint is switched off and admins are notified | PASS |  |  |
| WH-016 | A disabled endpoint receives nothing | PASS |  |  |
| WH-017 | Redirects are not followed (302 = failed delivery, target never called) | PASS |  |  |
| WH-018 | A receiver slower than 10 s times out (failed, request bounded) | PASS |  |  |
| WH-019 | Rotated secret: deliveries are signed with the new secret, not the old one | PASS |  |  |
| WH-020 | Another workspace gets 404 on every endpoint operation | PASS |  |  |
| WH-021 | Events of another workspace never reach this workspace's endpoint | PASS |  |  |
| WH-022 | Delivery log: newest first, bounded (limit<=100), status filter, payload on detail | PASS |  |  |
| WH-023 | Deleting an endpoint removes it and its delivery log | PASS |  |  |
