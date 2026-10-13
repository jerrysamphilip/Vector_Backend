# crm_kpis results

PASS 32 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| CK-001 | Schema is at alembic 0014_crm_kpis: committed columns on forecast_snapshots and the user_activity_days table | PASS |  |  |
| CK-002 | Owner gets all six KPIs in BRD order with value, numerator, denominator, target, period, note and definition | PASS |  |  |
| CK-003 | Only the duplicate rate has a BRD target (< 1%) | PASS |  |  |
| CK-004 | Agents and level-2 managers get the workspace KPIs (duplicate rate, adoption) as unavailable | PASS |  |  |
| CK-005 | The Sales Head (level 1) sees the workspace KPIs | PASS |  |  |
| CK-006 | Bad filters: quarter without fy 400, fy out of range 422, member outside your team 404, no token 401 | PASS |  |  |
| CK-010 | Duplicate contact rate: numerator / denominator match the BRD rules applied to the rows from SQL | PASS |  |  |
| CK-011 | The dataset's duplicates: similar name at the same company, Gmail dots/+tag, same phone (6 of 8 live; deleted one ignored) | PASS |  |  |
| CK-012 | The contacts counted are exactly those the Duplicates page groups | PASS |  |  |
| CK-013 | member= narrows the duplicate rate to that person's contacts (their duplicate partner is someone else's) | PASS |  |  |
| CK-014 | Merging a duplicate group resolves it: 4 of 7 | PASS |  |  |
| CK-020 | Leads from a reply (one-click, source 'Campaign reply'), by hand after a reply, and unrelated leads are created | PASS |  |  |
| CK-021 | Reply-to-lead: leads from replies / contacts with a positive reply match SQL (3 / 4 = 75%) | PASS |  |  |
| CK-022 | An agent's reply-to-lead covers their own leads and contacts (2 / 3) | PASS |  |  |
| CK-023 | No positive replies: value null with a note (the stack runs without an AI classifier key) | PASS |  |  |
| CK-030 | Lead-to-SQL: SQLs among the period's new leads / new leads match SQL (3 / 7) | PASS |  |  |
| CK-031 | SQL-to-win: won SQLs / SQLs match SQL and the SQL-to-win report (2 / 3) | PASS |  |  |
| CK-032 | Lead-to-SQL equals the Leads & SQLs report's rate | PASS |  |  |
| CK-040 | Forecast accuracy for the last finished quarter = won in it / committed at its start, from SQL (3000 / 4000 = 75%) | PASS |  |  |
| CK-041 | Forecast accuracy can exceed 100% (rep 1: 1200 won / 1000 committed = 120%) | PASS |  |  |
| CK-042 | The forecast-accuracy report uses the same BRD formula (accuracy = actual / committed) and keeps the weighted measure as secondary | PASS |  |  |
| CK-043 | A snapshot stores committed = won to date + open Commit deals (stage default or set by hand; Omitted excluded) | PASS |  |  |
| CK-044 | The current quarter by fy/quarter is in progress: won so far / committed, with a note | PASS |  |  |
| CK-045 | A quarter with nothing committed in scope gives null with a note | PASS |  |  |
| CK-046 | Hidden amounts: an agent sees the accuracy % but not revenue or committed amounts | PASS |  |  |
| CK-050 | Adoption this week = users active this week / licensed seats, from SQL (5 / 5; the deactivated user is no seat) | PASS |  |  |
| CK-051 | Adoption trend: 9 weeks, last week 2 / 5 = 40% (deactivated user ignored), Sunday counted in its own week | PASS |  |  |
| CK-052 | member= narrows adoption to that team's seats (manager + rep 1) | PASS |  |  |
| CK-053 | Signing in and using the API records one activity row per user per day | PASS |  |  |
| CK-060 | Another workspace sees only its own numbers (1 seat, no contacts, no leads) | PASS |  |  |
| CK-061 | Another workspace cannot report on this workspace's people (404) | PASS |  |  |
| CK-062 | The KPI call answers quickly (< 3 s) | PASS |  |  |
