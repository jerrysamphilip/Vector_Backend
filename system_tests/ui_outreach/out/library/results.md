# UI outreach/admin - library results

PASS 14 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-TPL-01 | Template library empty state renders; New template opens modal; Cancel closes without saving | PASS |  |  |
| UIO-TPL-02 | Create template: merge-field insert, save -> row in list and in DB with subject/body/category/shared | PASS |  |  |
| UIO-TPL-03 | Validation: saving a template with empty name shows a server error inside the modal (no crash) | PASS |  |  |
| UIO-TPL-04 | Preview renders with sample values; Edit updates DB; category filter + search narrow the list | PASS |  |  |
| UIO-TPL-05 | Agent sees shared template but only the owner can edit/delete (no edit buttons for agent; API refuses) | PASS |  |  |
| UIO-TPL-06 | Delete: dismissing confirm keeps it; accepting removes row and DB record | PASS |  |  |
| UIO-AUTO-01 | Automation empty state; Create rule modal validation (required fields) keeps modal open | PASS |  |  |
| UIO-AUTO-02 | Create Send-AI-Template rule: row appears; DB has trigger/action/template/delay and is active | PASS |  |  |
| UIO-AUTO-02b | Create "Pause this contact" rule (PAUSE_PROSPECT, replaced "Pause Campaign"): option offered, no template picker, row listed, DB row persisted | PASS |  |  |
| UIO-AUTO-03 | Toggle rule off/on (the repaired PUT): DB is_active flips and UI reflects it | PASS |  |  |
| UIO-AUTO-04 | Search filters rules; delete (confirm) removes row and DB record | PASS |  |  |
| UIO-AIGEN-01 | AI generator: Classify disabled until designation entered; classification shows persona + confidence | PASS |  |  |
| UIO-AIGEN-02 | Sequence tab: 3-email sequence is generated, shows compliance check, copy button works | PASS |  |  |
| UIO-AIGEN-03 | Classification failure (500) shows an error message to the user | PASS |  |  |
