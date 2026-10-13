# API sweep findings

Run: 5715 sweep calls, 5917 HTTP requests, 481s. Cases: 5843 (PASS 5630, FAIL 12, BLOCKED 201).

## Coverage

- Sweep only: 311/313 endpoints hit (99.4%), 286/313 with a 2xx (91.4%).
- Since API start (all suites): 311/313 hit (99.4%), 286 with 2xx (91.4%).
- Per-endpoint table with reasons: `ENDPOINT_COVERAGE.md`.

## Counts per check type

| Check | PASS | FAIL | BLOCKED |
|---|---|---|---|
| AUTH | 883 | 0 | 0 |
| HAPPY | 306 | 3 | 0 |
| HIER | 133 | 0 | 0 |
| IDEM | 114 | 0 | 0 |
| ISO | 443 | 2 | 0 |
| ROLE | 1859 | 6 | 0 |
| SCHEMA | 79 | 0 | 201 |
| VAL | 1813 | 1 | 0 |

BLOCKED = response schema not declared in OpenAPI (cannot be validated) or external dependency unavailable.

## Failures by group (defects first)

### HAPPY:  (1 case(s); severity n/a; category defect)

- `API-POST-campaigns-campaign-id-prospects-prospect-id-mark-opened-HAPPY`: status 404: {"detail": "No sent message found for this prospect"}

### ROLE: MANAGER  (1 case(s); severity n/a; category defect)

- `API-GET-imports-ROLE-MANAGER`: MANAGER expected allowed (not 403), got 403: {"detail": "Your account does not have the 'manage_prospects' permission."}

### ROLE: AGENT  (1 case(s); severity n/a; category defect)

- `API-GET-imports-ROLE-AGENT`: AGENT expected allowed (not 403), got 403: {"detail": "Your account does not have the 'manage_prospects' permission."}

### ROLE: AGENT_NOPERM  (1 case(s); severity n/a; category defect)

- `API-GET-imports-ROLE-AGENT_NOPERM`: AGENT_NOPERM expected allowed (not 403), got 403: {"detail": "Your account does not have the 'manage_prospects' permission."}

### ROLE: MANAGER-read  (1 case(s); severity n/a; category defect)

- `API-GET-imports-ROLE-MANAGER-read`: MANAGER reading list endpoint expected 2xx, got 403: {"detail": "Your account does not have the 'manage_prospects' permission."}

### ROLE: AGENT-read  (1 case(s); severity n/a; category defect)

- `API-GET-imports-ROLE-AGENT-read`: AGENT reading list endpoint expected 2xx, got 403: {"detail": "Your account does not have the 'manage_prospects' permission."}

### ROLE: AGENT_NOPERM-read  (1 case(s); severity n/a; category defect)

- `API-GET-imports-ROLE-AGENT_NOPERM-read`: AGENT_NOPERM reading list endpoint expected 2xx, got 403: {"detail": "Your account does not have the 'manage_prospects' permission."}

### Unknown id answered 200 where the endpoint is stateless/reference data  (1 case(s); severity n/a; category by-design)

persona-blueprints (falls back), uploads revalidations.

- `API-POST-uploads-upload-id-revalidations-VAL-path-nonexistent-id`:  expected 4xx rejection, got 200: {"total_rows": 3, "accepted": 3, "rejected": 0, "records": [{"row": 1, "email": "up0@uswa30ce336e3.example.com", "first_name": "U0", "last_name": "Ser", "company_name": "Acme swa30ce336e3", "designati... | req: POST unknown uu

### Upload id is not server state (stateless revalidation)  (1 case(s); severity n/a; category by-design)

/uploads/{id}/revalidations works on the records in the body; the id is not looked up, so there is nothing to isolate.

- `API-POST-uploads-upload-id-revalidations-ISO-path-foreign-id`: tenant A used tenant B's ['upload_id'] -> 200 (expected 403/404): {"total_rows": 3, "accepted": 3, "rejected": 0, "records": [{"row": 1, "email": "up0@uswa30ce336e3.example.com", "first_name": "U0", "last_name": "Ser", "company_name": "Acme swa30ce336e3", "des

### ISO: victim-intact  (1 case(s); severity n/a; category defect)

- `API-GET-templates-template-id-ISO-victim-intact`: tenant B's own template_id still readable after cross-tenant attempts by tenant A -> 404

### HAPPY: attach  (1 case(s); severity n/a; category defect)

- `API-DELETE-templates-template-id-attachments-attachment-id-HAPPY-attach`: status 404: {"detail": "Template not found"}

### HAPPY: template  (1 case(s); severity n/a; category defect)

- `API-DELETE-templates-template-id-HAPPY-template`: status 404: {"detail": "Template not found"}

## Unhandled 500s with server-side location (from API logs)

- `POST /templates/{template_id}/regenerations` x2 probes ['happy'] -> 500; example body `{"template_id": "9a96b6f3-4334-4344-bfa5-44afd3ec7b09", "field": "subject", "campaign_desc` ; server: 
