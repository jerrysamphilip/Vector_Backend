# UI outreach/admin - campaigns_list results

PASS 13 · FAIL 6 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-CAMP-HOME-01 | Home (My day rail) renders empty state for a new workspace: "Nothing due today", no stale deals | PASS |  |  |
| UIO-CAMP-LIST-EMPTY | Campaigns list empty state: "No campaigns yet" + Create New Campaign button works | PASS |  |  |
| UIO-CAMP-DASH-EMPTY | Campaign analytics (Dashboard) empty workspace: zeros not NaN, range buttons render | PASS |  |  |
| UIO-CAMP-HOME-02 | Home My day rail lists a due task; ticking it marks it DONE in the DB and removes it | PASS |  |  |
| UIO-CAMP-LIST-01 | Campaigns list shows every campaign with correct status badge and prospect count (matches DB) | PASS |  |  |
| UIO-CAMP-LIST-02 | Search filters rows (server-side, debounced) and clearing restores; no-match shows empty state | PASS |  |  |
| UIO-CAMP-LIST-03 | Column picker toggles columns, persists in localStorage across reload, Reset restores defaults | PASS |  |  |
| UIO-CAMP-LIST-04 | Row Pause on ACTIVE campaign -> PAUSED (DB + badge), Resume -> ACTIVE | PASS |  |  |
| UIO-CAMP-LIST-05 | Delete: cancelling the confirm keeps the campaign; accepting removes it (UI + DB) | FAIL | defect | locator.click: Error: strict mode violation: locator('tbody tr').filter({ hasText: 'Delta Delete db94ff' }).first().getByTitle('Delete') resolved to 2 elements: |
| UIO-CAMP-LIST-06 | Row click: DRAFT opens the wizard in edit mode, ACTIVE opens details; View icon opens details | FAIL | defect | locator.click: Timeout 12000ms exceeded. |
| UIO-CAMP-LIST-07 | Select-all + bulk bar: Pause selected (confirm) pauses active campaigns in DB; clear selection | FAIL | defect | statuses ACTIVE/PAUSED |
| UIO-CAMP-LIST-08 | Pause API failure surfaces feedback to the user (not silent) | FAIL | defect | API error on pause shown nowhere (silent failure) |
| UIO-CAMP-LIST-09 | Campaigns list: API 500 shows an explicit error (not the empty state) | FAIL | defect | no error message: {"detail":"x"} |
| UIO-CAMP-DASH-01 | Dashboard shows real totals for the workspace (campaign count, status breakdown) consistent with DB | PASS |  |  |
| UIO-CAMP-DASH-02 | Dashboard range buttons (Daily/Weekly/Monthly/Yearly) change the chart without errors; alerts bell opens | PASS |  |  |
| UIO-CAMP-DASH-03 | Dashboard campaign table lists campaigns; rows and "View Campaigns" navigate | PASS |  |  |
| UIO-CAMP-DASH-04 | Dashboard API failure (global-analytics/dashboard-summary 500) degrades without crash | PASS |  |  |
| UIO-CAMP-ROLE-mgr | MANAGER sees the campaigns list and can open a campaign (manage_campaigns) | PASS |  |  |
| UIO-CAMP-ROLE-agent-dash | AGENT on Campaign analytics: page shows a clear state (no raw 403 errors / crash) | FAIL | defect | http 403 GET /vector/api/reports/global-analytics?start_date=2026-05-01&end_date=2026-10-11&granularity=daily ; console: Failed to load resource: the server responded with a status of 403 (Forbidden) ; http 403 GET /vector/api/reports/global-analytics?start_date=2026-05-01&end_date=2026-10-11&granularity=daily ; console: Failed to load resource: the server responded with a status of 403 (Forbidden) |
