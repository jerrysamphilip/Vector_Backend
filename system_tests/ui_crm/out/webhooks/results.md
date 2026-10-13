# ui_crm webhooks results

PASS 9 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIF-WH-01 | Admin sees Workspace > Webhooks in the sidebar; empty state on a new workspace | PASS |  |  |
| UIF-WH-02 | Validation: non-http(s) URL is refused with the server message; no events -> "Choose at least one event"; nothing stored | PASS |  |  |
| UIF-WH-03 | Add endpoint: tick events, save -> secret shown once with Copy; list shows URL, Active and the secret hint; DB row | PASS |  |  |
| UIF-WH-04 | Send test: banner "Test event delivered", receiver got a signed webhook.test, delivery log lists it and View shows the payload | PASS |  |  |
| UIF-WH-05 | Edit endpoint: add an event -> chips and DB event_types updated | PASS |  |  |
| UIF-WH-06 | Rotate secret (confirm) -> a new secret is shown once and differs from the first | PASS |  |  |
| UIF-WH-07 | Turn off -> badge "Off", DB inactive; turn on again -> Active | PASS |  |  |
| UIF-WH-08 | Agent: no Webhooks nav entry; /app/webhooks says only admins can manage webhooks; API is 403 | PASS |  |  |
| UIF-WH-09 | Delete (confirm) -> card gone, empty state back, DB endpoint and its deliveries removed | PASS |  |  |
