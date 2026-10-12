# UI outreach/admin - admin results

PASS 18 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-TEAM-01 | Team page lists every workspace member with role/status; KPI tiles match DB counts | PASS |  |  |
| UIO-TEAM-02 | Invite modal: opens/cancels; empty and malformed email are blocked (no request); roles radios incl. Super Admin for owner | PASS |  |  |
| UIO-TEAM-03 | Invite a Manager: modal closes, confirmation banner; DB has INVITED user with role, invitation row and magic-login token | PASS |  |  |
| UIO-TEAM-04 | Inviting an existing member shows the server error in the modal (409), stays open | PASS |  |  |
| UIO-TEAM-05 | Search and status filter narrow the member table | PASS |  |  |
| UIO-TEAM-06 | Change role (owner): Manager -> Admin saved to DB; Save button only after a change | PASS |  |  |
| UIO-TEAM-07 | Deactivate agent -> INACTIVE in DB and cannot log in; Reactivate restores ACTIVE | PASS |  |  |
| UIO-TEAM-08 | Revoke a pending invite: Revoke button marks the invited user INACTIVE/removed; self row has no Deactivate | PASS |  |  |
| UIO-TEAM-09 | Permissions modal: toggling a permission saves custom_permissions (DB) and the user's access changes; Reset restores defaults | PASS |  |  |
| UIO-TEAM-10 | Permanent delete: Cancel keeps user, OK removes the row from DB | PASS |  |  |
| UIO-TEAM-11 | ADMIN view: no role dropdowns, no Permissions buttons; API refuses admin role changes and Super Admin invites (403) | PASS |  |  |
| UIO-TEAM-12 | Team API 500 on user list shows an error state, not an empty team | PASS |  |  |
| UIO-REP-01 | Reports renders KPIs and campaign table for a campaign with sends; no NaN/undefined | PASS |  |  |
| UIO-REP-02 | Date range buttons (7d/30d/90d/365d) re-query with the matching start_date | PASS |  |  |
| UIO-REP-03 | Export CSV / Export Word / campaign Export CSV trigger file downloads | PASS |  |  |
| UIO-REP-04 | User filter dropdown (All Users) lists workspace users and filters the report | PASS |  |  |
| UIO-REP-05 | Reports API 500 does not crash (error boundary not shown) | PASS |  |  |
| UIO-REP-06 | MANAGER (view_analytics) can open Reports; AGENT is redirected Home | PASS |  |  |
