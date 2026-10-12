# API sweep: summary (read first)

How to run: `python3 gen_inventory.py` (ENDPOINTS.json from live OpenAPI + guards introspected in the vector-api image), `python3 run_api_sweep.py --fresh` (about 8.5 min, about 6,000 requests, about 700 rows inserted), `python3 make_findings.py` (writes ENDPOINT_COVERAGE.md + FINDINGS_generated.md). Triage labels live in `triage.json` (labels only, never turns a FAIL into a PASS). Raw cases: `sweep_cases.json`, `results.md`.

## Security first
- **Cross-tenant data leakage: none found.** 438 isolation cases pass: every endpoint with a resource id in the path was called with tenant B's id (404/403 every time, B's resources verified intact afterwards); every list/read endpoint was called by both tenants and the other tenant's ids, marker and tenant_id never appeared; query filters (owner, member, user_id, account_id...) with foreign ids leak nothing.
- **Cross-tenant reference accepted (Medium):** `POST /templates` accepts another tenant's `sequence_id` and answers 201 (template created pointing at tenant B's sequence). Suspected: app/routers/template_router.py create_template does not verify the sequence belongs to the campaign/tenant. Other body references to foreign ids are rejected or ignored without effect.
- **Authentication: 883/883 pass.** Every protected endpoint answers 401 for no token, garbage token and wrong scheme; an expired correctly-signed JWT is 401. Forbidden roles always got 403 (never 200/500); no missing authorization on any endpoint (ROLE failures are only the three campaigns/lists 500s).
- **Hierarchy (BR-SH-02): 133/133 pass** for contacts, companies, leads, opportunities, tasks, sales-reports `member=` and reports `user_id=` (manager sees own team only, agent only self, agents in other branches see nothing).
- **Unhandled 500s (the main defect class).** See the groups below: wizard (3 endpoints always 500), GET /campaigns/lists always 500, generations FK 500, DB-overflow 500s (strings longer than the column, free text into enum-like columns, out-of-range integers, negative limits) on about 35 endpoints, NUL bytes accepted and later breaking the docx export for the tenant.
- Guards are partly not declarative: 9 handlers add permission/role checks inline (manage_prospects on contacts/companies/lists delete, restore, deleted, duplicates, backfill; "Agents cannot delete contacts/companies"; export for admins/managers only). They behave correctly, but the dependency tree does not show them (listed in gen_inventory.py INLINE_PERMS/INLINE_ROLES).

## Other observations (not counted as failures)
- `POST /api/users/invite` stores the invitation row as `ACCEPTED` immediately, so `DELETE /api/users/invitations/{id}` on a real invite fails with 400 and `GET /api/users/invitations` never lists it; the endpoint only works on a PENDING row (seeded via SQL in the sweep). The response also returns the temporary password in `manual_credentials`.
- 230 of 313 endpoints declare no response schema in OpenAPI (196 SCHEMA cases BLOCKED for that reason): response shapes cannot be validated or generated into clients.
- `X-Forwarded-For` is trusted as "last hop": when the API port is reachable directly, a client can pick its own rate-limit bucket (the other suites already rely on this for sign-up). Only matters if 8191 is exposed without a proxy.
- MANAGER's default permissions lack `manage_prospects`, so managers cannot delete/restore contacts or companies or use duplicates/deleted views (by design per _DEFAULT_PERMS, but worth confirming).
- Environment limits (classified 'environment', not defects): no OpenAI/AWS/OAuth: Google/Microsoft OAuth start/callback return 400/501, AI generation, template regeneration and IMAP/SMTP tests return clean 4xx/5xx JSON; platform-admin endpoints (/api/admin/*, /scheduler/* mutating, /persona-blueprints POST, /inboxes/ses/quota, /deliverability/statistics/account) were only checked for 401/403 (no operator user).
- Coverage numbers come from /metrics deltas; the metrics middleware does not count routes ending in `/metrics`, so `/campaigns/{id}/metrics` and `/deliverability/integrations/metrics` show N although the sweep got 2xx on them.

# API sweep findings

Run: 5832 sweep calls, 6033 HTTP requests, 503s. Cases: 5836 (PASS 5526, FAIL 114, BLOCKED 196).

## Coverage

- Sweep only: 311/313 endpoints hit (99.4%), 288/313 with a 2xx (92.0%).
- Since API start (all suites): 311/313 hit (99.4%), 289 with 2xx (92.3%).
- Per-endpoint table with reasons: `ENDPOINT_COVERAGE.md`.

## Counts per check type

| Check | PASS | FAIL | BLOCKED |
|---|---|---|---|
| AUTH | 883 | 0 | 0 |
| HAPPY | 304 | 5 | 0 |
| HIER | 133 | 0 | 0 |
| IDEM | 109 | 3 | 0 |
| ISO | 438 | 7 | 0 |
| ROLE | 1856 | 9 | 0 |
| SCHEMA | 46 | 33 | 196 |
| VAL | 1757 | 57 | 0 |

BLOCKED = response schema not declared in OpenAPI (cannot be validated) or external dependency unavailable.

## Failures by group (defects first)

### GET /campaigns/lists always 500 (AttributeError ProspectList.prospect_count)  (12 case(s); severity High; category defect)

Suspected location: app/routers/campaign_wizard_router.py:233 get_available_lists reads ProspectList.prospect_count which the model does not have. The campaign wizard list picker is broken for every role.

- `API-GET-campaigns-lists-HAPPY`: status 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-SUPER_ADMIN`: SUPER_ADMIN expected allowed (not 403), got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-ADMIN`: ADMIN expected allowed (not 403), got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-MANAGER`: MANAGER expected allowed (not 403), got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-AGENT`: AGENT expected allowed (not 403), got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-AGENT_NOPERM`: AGENT_NOPERM expected allowed (not 403), got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-ADMIN-read`: ADMIN reading list endpoint expected 2xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-GET-campaigns-lists-ROLE-MANAGER-read`: MANAGER reading list endpoint expected 2xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- ... and 4 more

### POST /campaigns/wizard-creations (and wizard-executions) always 500: CampaignStateEvent(event_id=...) TypeError  (12 case(s); severity High; category defect)

Suspected location: app/services/campaign_wizard_service.py:402 _log_campaign_event passes an invalid keyword 'event_id' to CampaignStateEvent (called from step_1_create_campaign, line 154). Wizard campaign creation and one-shot wizard execution are unusable.

- `API-POST-campaigns-wizard-creations-HAPPY`: status 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-POST-campaigns-wizard-executions-VAL-body-oversize-strings`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 1 string field(s) set to oversize
- `API-POST-campaigns-wizard-creations-VAL-body-oversize-strings`: failing field(s): ['campaign_name', 'cta_link']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 2 string field(s) set to oversize
- `API-POST-campaigns-wizard-executions-VAL-body-special-strings`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 1 string field(s) set to special
- `API-POST-campaigns-wizard-executions-VAL-body-ints--1`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['cool_off_days'] = -1
- `API-POST-campaigns-wizard-executions-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['cool_off_days'] = 1000000000000000
- `API-POST-campaigns-wizard-creations-VAL-body-special-strings`: failing field(s): ['campaign_name', 'cta_link']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 2 string field(s) set to special
- `API-POST-campaigns-wizard-creations-VAL-body-ints--1`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['send_start_hour', 'send_end_hour'] = -1
- ... and 4 more

### POST /campaigns/{id}/generations 500: FK violation when regenerating templates that have versions/attachments  (2 case(s); severity High; category defect)

Suspected location: app/services/batch_processor_service.py:376 generate_templates_per_persona deletes existing EmailTemplate rows that are still referenced by email_template_versions / email_attachments (IntegrityError 1451). Reached from campaign_wizard_router.py:306 process_list_auto.

- `API-POST-campaigns-campaign-id-generations-VAL-body-oversize-strings`: failing field(s): ['product_name', 'company_name', 'campaign_description']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 3 string field(s) set to oversize
- `API-POST-campaigns-campaign-id-generations-VAL-body-special-strings`: failing field(s): ['product_name', 'company_name', 'campaign_description']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 3 string field(s) set to special

### Unhandled 5xx  (1 case(s); severity High; category defect)

- `API-GET-reports-dashboard-summary-ISO-list-swb-sees-swa`: tenant swb response contains tenant swa's data [] (status 598)

### Strings longer than the DB column -> 500 'Data too long' (no max_length validation) on many create/update endpoints  (21 case(s); severity Medium; category defect)

Affects company-profiles, campaign-drafts, automation-rules, wizard, products, stages, template-library, reports/custom, inboxes, prospects, conversations reply, auth/register, uploads confirmations, campaigns/templates regenerate, revalidate. Pydantic models need max_length / handlers need to map DataError 1406 to 422.

- `API-POST-api-company-profiles-VAL-body-oversize-strings`: failing field(s): ['profile_name', 'company_name', 'company_description', 'default_sender_name', 'default_cta_link', 'postal_address']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"
- `API-PUT-api-company-profiles-profile-id-VAL-body-oversize-strings`: failing field(s): ['profile_name', 'company_name', 'company_description', 'default_sender_name', 'default_cta_link', 'postal_address']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"
- `API-POST-campaign-drafts-VAL-body-oversize-strings`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 1 string field(s) set to oversize
- `API-PUT-automation-rules-rule-id-VAL-body-oversize-strings`: failing field(s): ['name', 'description']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 2 string field(s) set to oversize
- `API-POST-campaign-wizard-update-template-VAL-body-oversize-strings`: failing field(s): ['subject', 'body']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 2 string field(s) set to oversize
- `API-PATCH-campaigns-prospects-prospect-id-revalidate-VAL-body-oversize-strings`: failing field(s): ['new_email']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 2 string field(s) set to oversize
- `API-POST-campaigns-campaign-id-personas-persona-type-generations-VAL-body-oversize-strings`: failing field(s): ['product_name']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 3 string field(s) set to oversize
- `API-POST-campaigns-campaign-id-templates-template-id-regenerate-VAL-body-oversize-strings`: failing field(s): ['tone']  expected no 5xx, got 500: {"detail": "AI generation failed: (pymysql.err.DataError) (1406, \"Data too long for column 'tone' at row 1\")\n[SQL: UPDATE email_templates SET tone=%(tone)s, generated_at=%(generated_at)s WHERE emai... | 
- ... and 13 more

### Free text sent to short/enum-like columns (sending_mode, unsubscribe_mode, provider, status, warmup_status, unit, object_type) -> 500  (8 case(s); severity Medium; category defect)

Values are not validated against the allowed set; the DB rejects them (data too long / enum) and the request ends in 500 instead of 422.

- `API-POST-campaigns-VAL-body-special-strings`: failing field(s): ['sending_mode']  expected no 5xx, got 500: {"detail": "An internal server error occurred."} | req: 5 string field(s) set to special
- `API-PUT-campaigns-campaign-id-VAL-body-special-strings`: failing field(s): ['sending_mode', 'unsubscribe_mode']  expected no 5xx, got 500: {"detail": "An internal server error occurred."} | req: 6 string field(s) set to special
- `API-POST-campaigns-campaign-id-templates-template-id-regenerate-VAL-body-special-strings`: failing field(s): ['tone']  expected no 5xx, got 500: {"detail": "AI generation failed: (pymysql.err.DataError) (1406, \"Data too long for column 'tone' at row 1\")\n[SQL: UPDATE email_templates SET tone=%(tone)s, generated_at=%(generated_at)s WHERE emai... | 
- `API-POST-inboxes-VAL-body-special-strings`: failing field(s): []  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 9 string field(s) set to special
- `API-PUT-inboxes-inbox-id-VAL-body-special-strings`: failing field(s): ['provider', 'status', 'warmup_status']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 10 string field(s) set to special
- `API-POST-products-VAL-body-special-strings`: failing field(s): ['unit']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 4 string field(s) set to special
- `API-PATCH-products-product-id-VAL-body-special-strings`: failing field(s): ['unit']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 4 string field(s) set to special
- `API-POST-views-VAL-body-special-strings`: failing field(s): ['object_type']  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: 2 string field(s) set to special

### Out-of-range integers (-1 / 10^15) -> 500 (DB out of range / negative hour)  (7 case(s); severity Medium; category defect)

campaign-drafts current_step, campaigns min_gap_minutes/batch_size, wizard hours/cool_off_days, contacts/fields sort_order, opportunity activities duration_minutes. Add ge/le bounds.

- `API-POST-campaign-drafts-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['current_step'] = 1000000000000000
- `API-POST-campaigns-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"detail": "An internal server error occurred."} | req: ['min_gap_minutes', 'batch_size', 'batch_gap_minutes', 'daily_batch_size'] = 1000000000000000
- `API-PUT-campaigns-campaign-id-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"detail": "An internal server error occurred."} | req: ['min_gap_minutes', 'batch_size', 'batch_gap_minutes', 'daily_batch_size'] = 1000000000000000
- `API-POST-campaigns-campaign-id-enrollments-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['cool_off_days'] = 1000000000000000
- `API-POST-contacts-fields-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['sort_order'] = 1000000000000000
- `API-PATCH-contacts-fields-field-id-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['sort_order'] = 1000000000000000
- `API-POST-opportunities-opportunity-id-activities-VAL-body-ints-1000000000000000`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: ['duration_minutes'] = 1000000000000000

### POST /campaigns/{id}/activations on an already ACTIVE campaign -> uncaught ValueError -> 500  (2 case(s); severity Medium; category defect)

Suspected location: app/services/campaign_email_service.py:373 launch() raises ValueError('Cannot launch campaign with status: ACTIVE'); router app/routers/campaign_wizard_router.py:554 (activate_campaign) does not translate it to 409/400.

- `API-POST-campaigns-campaign-id-activations-HAPPY`: status 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
- `API-POST-campaigns-campaign-id-activations-HAPPY~2`: status 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}

### Unreachable external dependency reported as bare HTTP 500 (POST /inboxes/{id}/sync)  (1 case(s); severity Medium; category defect)

app/routers/inbox_router.py sync_inbox_history raises HTTPException(500) when IMAP is unreachable; should be 502/504.

- `API-POST-inboxes-inbox-id-sync-HAPPY-imap-configured`: status 500: {"detail": "IMAP sync failed for sw1@swswa1a58a304d.example.com. Check that imap_host=imap.invalid is reachable and credentials are correct."}

### POST /uploads/validations: 2.4 MB / 600k-row CSV is processed synchronously >40 s (no row/size cap)  (1 case(s); severity Medium; category defect)

Client timed out after 40 s; availability/DoS risk.

- `API-POST-uploads-validations-VAL-multipart-bigfile`:  expected no 5xx, got 598: client timeout after 40s | req: big.csv 2400017 bytes

### Cross-tenant reference accepted: POST /templates with another tenant's sequence_id -> 201  (1 case(s); severity Medium; category defect)

POST /templates (app/routers/template_router.py) does not check that sequence_id belongs to the caller's campaign/tenant; the template is created pointing at tenant B's sequence.

- `API-POST-templates-ISO-body-foreign-sequence_id`: sequence_id=<tenant B resource> -> 201 (expected 4xx or foreign id ignored/not echoed) LEAK/LINKED: ['6db1e23f-fcff-4dae-aaba-edf49655e1e5']: {"template_id": "64ba7f8e-8330-4d37-8d58-b758bcd5c383", "campaign_id": "323a5313-07e6-440e-9787-5ce5f52d5304", "sequen

### GET /reports/export/global-analytics/docx 500 when stored data contains control characters (NUL bytes accepted on input)  (1 case(s); severity Medium; category defect)

Server: app/services/reports_service.py:1182 _add_data_table -> python-docx ValueError 'All strings must be XML compatible'. Control characters (\u0000) sent by the special-string probes are accepted and stored by some endpoints, then break the export for the whole tenant. Fix: reject/strip control chars on input and sanitise on export.

- `API-GET-reports-export-global-analytics-docx-ISO-list-swa-sees-swb`: tenant swa response contains tenant swb's data [] (status 500)

### OpenAPI response models declare non-nullable fields that are returned as null  (33 case(s); severity Low; category defect)

- `API-POST-api-company-profiles-SCHEMA`: $.company_description: null not allowed (declared string); $.default_cta_link: null not allowed (declared string)
- `API-GET-api-company-profiles-SCHEMA`: $[0].company_description: null not allowed (declared string); $[0].default_sender_name: null not allowed (declared string); $[0].default_cta_link: null not allowed (declared string); $[1].company_description: null not allowed (declared string); $[1].default_ct
- `API-GET-api-company-profiles-profile-id-SCHEMA`: $.company_description: null not allowed (declared string); $.default_cta_link: null not allowed (declared string)
- `API-PUT-api-company-profiles-profile-id-SCHEMA`: $.default_cta_link: null not allowed (declared string)
- `API-GET-api-users-SCHEMA`: $[0].avatar_url: null not allowed (declared string); $[0].last_login_at: null not allowed (declared string); $[1].avatar_url: null not allowed (declared string); $[2].avatar_url: null not allowed (declared string); $[3].avatar_url: null not allowed (declared s
- `API-GET-prospect-lists-list-id-prospects-SCHEMA`: $.prospects[0].poc_city: null not allowed (declared string); $.prospects[0].poc_state: null not allowed (declared string); $.prospects[0].poc_country: null not allowed (declared string); $.prospects[0].notes: null not allowed (declared string); $.prospects[1].
- `API-POST-inboxes-SCHEMA`: $.last_sent_at: null not allowed (declared string); $.imap_host: null not allowed (declared string); $.imap_username: null not allowed (declared string); $.last_sync_at: null not allowed (declared string); $.imap_last_error: null not allowed (declared string);
- `API-GET-inboxes-SCHEMA`: $[0].last_sent_at: null not allowed (declared string); $[0].imap_host: null not allowed (declared string); $[0].imap_username: null not allowed (declared string); $[0].last_sync_at: null not allowed (declared string); $[0].imap_last_error: null not allowed (de
- ... and 25 more

### Negative / huge integer query parameters -> 500 (limit, fy)  (6 case(s); severity Low; category defect)

deliverability_router.py:307 (list_external_events/metrics) passes negative limit into SQL LIMIT -> MySQL syntax error; sales_reports_router.py:346 -> services/sales.py:119 fiscal_year_bounds ValueError/OverflowError for fy=-1 or 10^30. Add ge/le bounds.

- `API-GET-deliverability-integrations-events-VAL-query-int-negative`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['limit']
- `API-GET-deliverability-integrations-events-VAL-query-int-huge`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['limit']
- `API-GET-deliverability-integrations-metrics-VAL-query-int-negative`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['limit']
- `API-GET-deliverability-integrations-metrics-VAL-query-int-huge`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['limit']
- `API-GET-sales-reports-forecast-VAL-query-int-negative`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['fy']
- `API-GET-sales-reports-forecast-VAL-query-int-huge`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['fy']

### Repeated DELETE is not 404 (idempotent success or 400 instead)  (3 case(s); severity Low; category defect)

DELETE /campaigns/{id}/prospects/{pid} returns 200 {removed:true} twice; DELETE /api/users/{id} and DELETE /api/users/invitations/{id} return 400 'already inactive/REVOKED'. Spec expects 404 for the second delete.

- `API-DELETE-campaigns-campaign-id-prospects-prospect-id-IDEM-second-enrolled`: second DELETE returned 200: {"removed": true} (expected 404)
- `API-DELETE-api-users-user-id-IDEM-second-user`: second DELETE returned 400: {"detail": "User is already inactive"} (expected 404)
- `API-DELETE-api-users-invitations-invitation-id-IDEM-second-invite`: second DELETE returned 400: {"detail": "Cannot revoke invitation with status: REVOKED"} (expected 404)

### reports/export/campaign-comparison 500 on special-character user_id  (1 case(s); severity Low; category defect)

reports_router.py:243 export_campaign_comparison_csv puts the user-supplied user_id into a header/filename -> UnicodeEncodeError (latin-1) -> 500.

- `API-GET-reports-export-campaign-comparison-VAL-query-str-special`:  expected no 5xx, got 500: {"success": false, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"} | req: query ['user_id']

### Unknown id answered 200 where the endpoint is stateless/reference data  (1 case(s); severity n/a; category by-design)

persona-blueprints (falls back), uploads revalidations.

- `API-POST-uploads-upload-id-revalidations-VAL-path-nonexistent-id`:  expected 4xx rejection, got 200: {"total_rows": 3, "accepted": 3, "rejected": 0, "records": [{"row": 1, "email": "up0@uswa1a58a304d.example.com", "first_name": "U0", "last_name": "Ser", "company_name": "Acme swa1a58a304d", "designati... | req: POST unknown uu

### Upload id is not server state (stateless revalidation)  (1 case(s); severity n/a; category by-design)

/uploads/{id}/revalidations works on the records in the body; the id is not looked up, so there is nothing to isolate.

- `API-POST-uploads-upload-id-revalidations-ISO-path-foreign-id`: tenant A used tenant B's ['upload_id'] -> 200 (expected 403/404): {"total_rows": 3, "accepted": 3, "rejected": 0, "records": [{"row": 1, "email": "up0@uswa1a58a304d.example.com", "first_name": "U0", "last_name": "Ser", "company_name": "Acme swa1a58a304d", "des

## Unhandled 500s with server-side location (from API logs)

- `GET /campaigns/lists` x13 probes ['happy', 'iso-list', 'role-allowed', 'role-read'] -> 500; example body `null` ; server: File "/app/app/routers/campaign_wizard_router.py", line 233, in get_available_lists; AttributeError: 'ProspectList' object has no attribute 'prospect_count'
- `POST /templates/{template_id}/regenerations` x2 probes ['happy'] -> 500; example body `{"template_id": "b009eca6-22d0-4d2a-9bc4-413dde818251", "field": "subject", "campaign_desc` ; server: 
- `POST /campaigns/wizard-creations` x13 probes ['happy', 'iso-body', 'val-badids', 'val-intextreme', 'val-oversize', 'val-oversize-bisect'] -> 500; example body `{"campaign_name": "Wiz swa1a58a304d", "send_start_hour": 9, "send_end_hour": 17, "timezone` ; server: File "/app/app/routers/campaign_wizard_router.py", line 245, in create_campaign; File "/app/app/services/campaign_wizard_service.py", line 154, in step_1_create_campaign; File "/app/app/services/campaign_wizard_service.py", line 402, in _log_campaign_event; TypeError: 'event_id' is an invalid keyword argument for CampaignStateEvent
- `POST /campaigns/wizard-executions` x6 probes ['happy', 'val-intextreme', 'val-oversize', 'val-special'] -> 500; example body `{"campaign_name": "WizEx swa1a58a304d", "list_id": "b1cb60ec-d760-44ae-a1d7-c27198348788"}` ; server: File "/app/app/routers/campaign_wizard_router.py", line 603, in run_complete_wizard; File "/app/app/services/campaign_wizard_service.py", line 482, in run_campaign_wizard; File "/app/app/services/campaign_wizard_service.py", line 154, in step_1_create_campaign; File "/app/app/services/campaign_wizard_service.py", line 402, in _log_campaign_event
- `POST /campaigns/{campaign_id}/generations` x10 probes ['happy', 'val-oversize', 'val-oversize-bisect', 'val-special', 'val-special-bisect'] -> 500; example body `{"campaign_id": "323a5313-07e6-440e-9787-5ce5f52d5304", "list_id": "b1cb60ec-d760-44ae-a1d` ; server: pymysql.err.IntegrityError: (1451, 'Cannot delete or update a parent row: a foreign key constraint fails (`outreach_ai`.`email_template_versions`, CONSTRAINT `email_template_versions_ibfk_1` FOREIGN KEY (`template_id`) REFERENCES `email_templates` (`template_id`))'); File "/app/app/routers/campaign_wizard_router.py", line 306, in process_list_auto; File "/app/app/services/batch_processor_service.py", line 444, in pro
- `POST /campaigns/{campaign_id}/activations` x4 probes ['happy'] -> 500; example body `{"campaign_id": "323a5313-07e6-440e-9787-5ce5f52d5304"}` ; server: File "/app/app/routers/campaign_wizard_router.py", line 554, in activate_campaign; File "/app/app/services/campaign_wizard_service.py", line 379, in step_7_activate_campaign; File "/app/app/services/campaign_email_service.py", line 373, in launch; ValueError: Cannot launch campaign with status: ACTIVE
- `POST /inboxes/{inbox_id}/sync` x2 probes ['happy'] -> 500; example body `null` ; server: 
- `POST /api/company-profiles` x7 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"profile_name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'profile_name' at row 1"); File "/app/app/routers/company_profile_router.py", line 194, in create_company_profile; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'profile_name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'company_name' at row 1")
- `PUT /api/company-profiles/{profile_id}` x7 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"company_description": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'profile_name' at row 1"); File "/app/app/routers/company_profile_router.py", line 241, in update_company_profile; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'profile_name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'company_name' at row 1")
- `POST /campaign-drafts/` x2 probes ['val-intextreme', 'val-oversize'] -> 500; example body `{"draft_name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'draft_name' at row 1"); File "/app/app/routers/campaign_draft_router.py", line 82, in save_draft; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'draft_name' at row 1"); pymysql.err.DataError: (1264, "Out of range value for column 'current_step' at row 1")
- `PUT /automation-rules/{rule_id}` x3 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"description": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/automation_router.py", line 74, in update_rule; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'description' at row 1")
- `POST /campaign-wizard/update-template` x3 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"template_id": "b009eca6-22d0-4d2a-9bc4-413dde818251", "subject": "AAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'subject' at row 1"); File "/app/app/routers/campaign_wizard_router.py", line 982, in update_template_manual; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'subject' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'body' at row 1")
- `POST /campaigns` x3 probes ['val-intextreme', 'val-special', 'val-special-bisect'] -> 500; example body `{"campaign_name": "Zo\u00eb \ud83d\ude00 \u0000 ' \" ; -- <script>alert(1)</script> \\ %s ` ; server: 
- `PATCH /campaigns/prospects/{prospect_id}/revalidate` x2 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"new_email": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'email' at row 1"); File "/app/app/routers/campaign_router.py", line 2179, in revalidate_bounced_prospect; File "/app/app/services/reconnect_service.py", line 134, in revalidate_prospect; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'email' at row 1")
- `PUT /campaigns/{campaign_id}` x4 probes ['val-intextreme', 'val-special', 'val-special-bisect'] -> 500; example body `{"sender_name": "Zo\u00eb \ud83d\ude00 \u0000 ' \" ; -- <script>alert(1)</script> \\ %s {{` ; server: 
- `POST /campaigns/{campaign_id}/enrollments` x1 probes ['val-intextreme'] -> 500; example body `{"campaign_id": "323a5313-07e6-440e-9787-5ce5f52d5304", "list_id": "b1cb60ec-d760-44ae-a1d` ; server: File "/app/app/routers/campaign_wizard_router.py", line 498, in enroll_prospects; File "/app/app/services/campaign_wizard_service.py", line 256, in step_4_run_safety_checks; File "/app/app/services/enrollment_rules.py", line 66, in enrolled_elsewhere_within; OverflowError: Python int too large to convert to C int
- `POST /campaigns/{campaign_id}/personas/{persona_type}/generations` x2 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"list_id": "b1cb60ec-d760-44ae-a1d7-c27198348788", "product_name": "AAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'body' at row 1"); File "/app/app/routers/campaign_wizard_router.py", line 381, in generate_persona_template; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'body' at row 1")
- `POST /campaigns/{campaign_id}/templates/{template_id}/regenerate` x4 probes ['val-oversize', 'val-oversize-bisect', 'val-special', 'val-special-bisect'] -> 500; example body `{"regenerate_subject": true, "tone": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: 
- `POST /contacts/fields` x1 probes ['val-intextreme'] -> 500; example body `{"label": "CF swa1a58a304d", "field_type": "TEXT", "sort_order": 1000000000000000}` ; server: pymysql.err.DataError: (1264, "Out of range value for column 'sort_order' at row 1"); File "/app/app/routers/contacts_router.py", line 1110, in create_field; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1264, "Out of range value for column 'sort_order' at row 1")
- `PATCH /contacts/fields/{field_id}` x1 probes ['val-intextreme'] -> 500; example body `{"label": "CF swa1a58a304d b", "field_type": "TEXT", "sort_order": 1000000000000000}` ; server: pymysql.err.DataError: (1264, "Out of range value for column 'sort_order' at row 1"); File "/app/app/routers/contacts_router.py", line 1131, in update_field; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1264, "Out of range value for column 'sort_order' at row 1")
- `POST /conversations/{conversation_id}/reply` x1 probes ['val-oversize'] -> 500; example body `{"body_text": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'body_text' at row 1"); File "/app/app/routers/conversation_router.py", line 45, in reply_to_conversation; File "/app/app/services/conversation_service.py", line 134, in create_reply; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'body_text' at row 1")
- `GET /deliverability/integrations/events` x2 probes ['val-q-int-huge', 'val-q-int-negative'] -> 500; example body `null` ; server: pymysql.err.ProgrammingError: (1064, "You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near '-1' at line 4"); File "/app/app/routers/deliverability_router.py", line 307, in list_external_events; sqlalchemy.exc.ProgrammingError: (pymysql.err.ProgrammingError) (1064, "You have an error in your SQL syntax; check the manual that corresponds t
- `GET /deliverability/integrations/metrics` x2 probes ['val-q-int-huge', 'val-q-int-negative'] -> 500; example body `null` ; server: pymysql.err.ProgrammingError: (1064, "You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near '-1' at line 4"); File "/app/app/routers/deliverability_router.py", line 289, in list_external_metrics; sqlalchemy.exc.ProgrammingError: (pymysql.err.ProgrammingError) (1064, "You have an error in your SQL syntax; check the manual that corresponds 
- `POST /inboxes` x3 probes ['val-oversize', 'val-oversize-bisect', 'val-special'] -> 500; example body `{"email_address": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'email_address' at row 1"); File "/app/app/routers/inbox_router.py", line 181, in create_inbox; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'email_address' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'provider' at row 1")
- `PUT /inboxes/{inbox_id}` x15 probes ['val-oversize', 'val-oversize-bisect', 'val-special', 'val-special-bisect'] -> 500; example body `{"daily_limit": 400, "provider": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'provider' at row 1"); File "/app/app/routers/inbox_router.py", line 331, in update_inbox; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'provider' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'smtp_host' at row 1")
- `POST /opportunities/{opportunity_id}/activities` x1 probes ['val-intextreme'] -> 500; example body `{"activity_type": "NOTE", "subject": "hi", "duration_minutes": 1000000000000000}` ; server: pymysql.err.DataError: (1264, "Out of range value for column 'duration_minutes' at row 1"); File "/app/app/routers/sales_router.py", line 770, in log_deal_activity; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1264, "Out of range value for column 'duration_minutes' at row 1")
- `POST /products` x7 probes ['val-oversize', 'val-oversize-bisect', 'val-special', 'val-special-bisect'] -> 500; example body `{"name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/quotes_router.py", line 66, in create_product; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'sku' at row 1")
- `PATCH /products/{product_id}` x7 probes ['val-oversize', 'val-oversize-bisect', 'val-special', 'val-special-bisect'] -> 500; example body `{"description": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/quotes_router.py", line 88, in update_product; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'sku' at row 1")
- `POST /reports/custom` x3 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/custom_reports_router.py", line 262, in create_report; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'description' at row 1")
- `PATCH /prospects/{prospect_id}` x8 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"designation": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'company_name' at row 1")
- `GET /reports/export/campaign-comparison` x1 probes ['val-q-str-special'] -> 500; example body `null` ; server: File "/app/app/routers/reports_router.py", line 243, in export_campaign_comparison_csv; UnicodeEncodeError: 'latin-1' codec can't encode character '\U0001f600' in position 45: ordinal not in range(256)
- `PATCH /reports/custom/{report_id}` x3 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/custom_reports_router.py", line 293, in update_report; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'description' at row 1")
- `GET /sales-reports/forecast` x2 probes ['val-q-int-huge', 'val-q-int-negative'] -> 500; example body `null` ; server: File "/app/app/routers/sales_reports_router.py", line 346, in forecast; File "/app/app/services/sales.py", line 119, in fiscal_year_bounds; ValueError: year -1 is out of range; OverflowError: Python int too large to convert to C long
- `POST /sales/stages` x1 probes ['val-oversize'] -> 500; example body `{"name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/sales_router.py", line 476, in create_stage; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1")
- `PATCH /sales/stages/{stage_id}` x1 probes ['val-oversize'] -> 500; example body `{"name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/sales_router.py", line 505, in update_stage; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1")
- `POST /template-library` x5 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"name": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/sales_admin_router.py", line 324, in create_template; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'category' at row 1")
- `PATCH /template-library/{template_id}` x5 probes ['val-oversize', 'val-oversize-bisect'] -> 500; example body `{"subject": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: pymysql.err.DataError: (1406, "Data too long for column 'name' at row 1"); File "/app/app/routers/sales_admin_router.py", line 342, in update_template; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'name' at row 1"); pymysql.err.DataError: (1406, "Data too long for column 'category' at row 1")
- `POST /uploads/{upload_id}/confirmations` x1 probes ['val-oversize'] -> 500; example body `{"upload_id": "02662ccd-3391-4ea4-abc1-594670968480", "title": "AAAAAAAAAAAAAAAAAAAAAAAAAA` ; server: 
- `POST /views` x2 probes ['val-special', 'val-special-bisect'] -> 500; example body `{"name": "Zo\u00eb \ud83d\ude00 \u0000 ' \" ; -- <script>alert(1)</script> \\ %s {{x}}", "` ; server: pymysql.err.DataError: (1406, "Data too long for column 'object_type' at row 1"); File "/app/app/routers/crm_router.py", line 81, in create_view; sqlalchemy.exc.DataError: (pymysql.err.DataError) (1406, "Data too long for column 'object_type' at row 1")
- `POST /uploads/validations` x1 probes ['val-mp-bigfile'] -> 598; example body `null` ; server: 
- `GET /reports/dashboard-summary` x1 probes ['iso-list'] -> 598; example body `null` ; server: 
- `GET /reports/export/global-analytics/docx` x1 probes ['iso-list'] -> 500; example body `null` ; server: File "/app/app/routers/reports_router.py", line 224, in export_global_analytics_docx; File "/app/app/services/reports_service.py", line 1291, in export_global_analytics_docx; File "/app/app/services/reports_service.py", line 1182, in _add_data_table; ValueError: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters
