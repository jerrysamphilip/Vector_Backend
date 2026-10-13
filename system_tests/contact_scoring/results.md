# contact_scoring results

PASS 28 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| CS-001 | Admin reads the scoring model, seeded with default fit rules and 7 engagement signals | PASS |  |  |
| CS-002 | Default fit rules cover seniority, size, revenue, industry, country, stage and email type | PASS |  |  |
| CS-003 | Agents and managers cannot read, change or recompute the model (403) | PASS |  |  |
| CS-004 | Weights that do not add up to 100 are rejected (400) | PASS |  |  |
| CS-005 | Rule points outside -100..100 are rejected (422) | PASS |  |  |
| CS-006 | Oversize rule value is rejected (422) | PASS |  |  |
| CS-007 | Unknown rule property is rejected (400) | PASS |  |  |
| CS-008 | Grade thresholds that do not decrease are rejected (400) | PASS |  |  |
| CS-009 | Admin saves a custom model | PASS |  |  |
| CS-010 | A new contact is scored straight away (score, fit, engagement, grade on the record) | PASS |  |  |
| CS-011 | Contact list sorts by score (desc) | PASS |  |  |
| CS-012 | Contact list sorts by score (asc) | PASS |  |  |
| CS-013 | Filter by grade A | PASS |  |  |
| CS-014 | Filter by several grades | PASS |  |  |
| CS-015 | Filter by score range | PASS |  |  |
| CS-016 | Bad grade 400, out-of-range score 422, bad sort 422 | PASS |  |  |
| CS-017 | Score is an available list / export column | PASS |  |  |
| CS-018 | Score breakdown shows which fit rules contributed | PASS |  |  |
| CS-019 | Logging a meeting raises engagement and the score right away | PASS |  |  |
| CS-020 | A grade change writes a property history row (D -> C, SYSTEM) | PASS |  |  |
| CS-021 | Email opens (one per email) and clicks count towards engagement | PASS |  |  |
| CS-022 | Recompute now is accepted asynchronously (202, queued) | PASS |  |  |
| CS-023 | The background recompute rescores every contact | PASS |  |  |
| CS-024 | Rescoring does not change the contact's Last updated | PASS |  |  |
| CS-025 | An agent reads the score of their own contact but not of others' (404) | PASS |  |  |
| CS-026 | Another workspace cannot read or recompute the score (404) | PASS |  |  |
| CS-027 | Each workspace has its own model | PASS |  |  |
| CS-028 | An agent's score-sorted list holds only their own contacts | PASS |  |  |
