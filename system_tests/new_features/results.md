# new_features results

PASS 37 · FAIL 3 · BLOCKED 1

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| NF-001 | A contact with one company lists it as the primary link | PASS |  |  |
| NF-002 | Add a second company with a label | PASS |  |  |
| NF-003 | Linking the same company twice is 409 | PASS |  |  |
| NF-004 | Another workspace's company is rejected (400/403/404/422) | PASS |  |  |
| NF-005 | Label over 100 characters is 422 | PASS |  |  |
| NF-006 | Contact detail returns both companies with labels | PASS |  |  |
| NF-007 | Contact list filtered by a linked (non-primary) company finds the contact | PASS |  |  |
| NF-008 | Company page lists the associated contact with its label | PASS |  |  |
| NF-009 | Make the second company primary and relabel it | PASS |  |  |
| NF-010 | Removing the primary company without a replacement is refused (400) | PASS |  |  |
| NF-011 | Removing the primary with a replacement works | PASS |  |  |
| NF-012 | Property history records company association changes | PASS |  |  |
| NF-013 | An agent who cannot see the contact cannot link companies (403/404) | PASS |  |  |
| NF-014 | Merging contacts moves the duplicate's company links to the survivor | PASS |  |  |
| NF-020 | A note with a mention is accepted | FAIL | defect | (400, {'detail': 'Nora Test cannot see this record, so cannot be mentioned'}) |
| NF-021 | The mentioned colleague gets a MENTION notification | FAIL | defect | 0 |
| NF-022 | The author is not notified of their own mention | PASS |  |  |
| NF-023 | Mentioning a user id that does not exist is 400 | PASS |  |  |
| NF-024 | Mentioning a user from another workspace is 400 | PASS |  |  |
| NF-025 | More than 20 mentions is rejected (400/422) | PASS |  |  |
| NF-026 | Mentions are only allowed on notes (400) | PASS |  |  |
| NF-027 | Mentionable-users lists active colleagues matching the search | PASS |  |  |
| NF-028 | Contact timeline returns the mentions on the note | FAIL | defect | (200, '{"items": [{"kind": "PROPERTY_CHANGE", "at": "2026-10-13T10:40:34.346891", "change": {"change_id": "1a17713e-cd33-4adf-ae63-2aa623329bb3", "field": "created", "label": "Created", "old_value": null, "n') |
| NF-040 | SQL-to-win returns the documented fields | PASS |  |  |
| NF-041 | SQL-to-win over an empty period is 200 with zero rate, not an error | PASS |  |  |
| NF-042 | SQL-to-win with a bad date is 422 | PASS |  |  |
| NF-043 | Forecast accuracy returns history list and average | PASS |  |  |
| NF-044 | Unknown period type is 422 | PASS |  |  |
| NF-045 | An agent cannot take a forecast snapshot (403) | PASS |  |  |
| NF-046 | An admin can take a forecast snapshot | PASS |  |  |
| NF-047 | The snapshot is stored for the workspace | PASS |  |  |
| NF-048 | SQL-to-win equals the value computed from the database for a built dataset | BLOCKED | test-gap | needs lead qualification flow details; covered by the E2E journeys and the developer's pytest tests |
| NF-060 | Purge ignores a contact that is not in the recycle bin | PASS |  |  |
| NF-061 | An agent without manage_prospects cannot purge (403) | PASS |  |  |
| NF-062 | Another workspace cannot purge it | PASS |  |  |
| NF-063 | Purging a deleted contact removes it and writes an audit row | PASS |  |  |
| NF-064 | Import history needs manage_prospects (403 for a plain agent) | PASS |  |  |
| NF-065 | Deleting an already inactive user again is 404 | PASS |  |  |
| NF-066 | A property with a duplicate label is 409 | PASS |  |  |
| NF-067 | Oversize value gives 422/409, never 500 | PASS |  |  |
| NF-068 | NUL character is rejected with 422 | PASS |  |  |
