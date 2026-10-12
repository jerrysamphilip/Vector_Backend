# UI outreach/admin - shell results

PASS 33 · FAIL 5 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-SHELL-SB-owner | Sidebar menu for OWNER: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-admin | Sidebar menu for ADMIN: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-manager | Sidebar menu for MANAGER: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-agent | Sidebar menu for AGENT: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-collapse | Sidebar group header toggles collapse/expand (aria-expanded) | PASS |  |  |
| UIO-SHELL-NAV-owner | Every visible sidebar link navigates and its page renders cleanly (OWNER) | PASS |  |  |
| UIO-SHELL-NAV-admin | Every visible sidebar link navigates and its page renders cleanly (ADMIN) | PASS |  |  |
| UIO-SHELL-NAV-manager | Every visible sidebar link navigates and its page renders cleanly (MANAGER) | PASS |  |  |
| UIO-SHELL-NAV-agent | Every visible sidebar link navigates and its page renders cleanly (AGENT) | FAIL | defect | Campaign analytics: link missing |
| UIO-SHELL-GUARD-agent | AGENT opening restricted URLs is redirected to Home (no flash of forbidden data) | PASS |  |  |
| UIO-SHELL-GUARD-manager | MANAGER opening restricted URLs is redirected to Home (no flash of forbidden data) | PASS |  |  |
| UIO-SHELL-GUARD-admin-owner | OWNER opening /vector/admin/... is redirected to Home | PASS |  |  |
| UIO-SHELL-GUARD-admin-agent | AGENT opening /vector/admin/... is redirected to Home | PASS |  |  |
| UIO-SHELL-GUARD-api | Server also refuses AGENT on team/inbox/campaign management endpoints (403), not just the UI | PASS |  |  |
| UIO-SHELL-404 | Unknown routes (/app/does-not-exist, /vector/xyz, /app/campaigns/<bad id>) end on a safe page, no crash | PASS |  |  |
| UIO-SHELL-HELP-01 | Help button opens panel for the current screen; Close button and Esc close it | PASS |  |  |
| UIO-SHELL-HELP-02 | ? and F1 keyboard shortcuts toggle help; ? is ignored while typing in a field | PASS |  |  |
| UIO-SHELL-HELP-03 | Help panel search finds articles, opens one, Back returns; "Open in Help Center" deep-links | PASS |  |  |
| UIO-SHELL-HELP-04 | Context help: every routed article opens for its own screen (panel title == article title) | PASS |  |  |
| UIO-SHELL-HELP-05 | Help Center: every article id deep link /app/help/<id> resolves to its own article and renders cleanly | PASS |  |  |
| UIO-SHELL-HELP-06 | Help Center: unknown article falls back gracefully; search + topic navigation + related links + "Go to this screen" work | PASS |  |  |
| UIO-SHELL-SEARCH-01 | Ctrl+K focuses global search; typing 1 char shows nothing, 2+ shows matching contact | PASS |  |  |
| UIO-SHELL-SEARCH-02 | Clicking a result opens that contact; query cleared | PASS |  |  |
| UIO-SHELL-SEARCH-03 | No-match query shows "No matches"; Enter goes to contacts?q=; Escape closes | PASS |  |  |
| UIO-SHELL-SEARCH-04 | Search API failure degrades silently (no crash, no [object Object]) | PASS |  |  |
| UIO-SHELL-SEARCH-05 | Tenant isolation: another workspace cannot find this contact via search | PASS |  |  |
| UIO-SHELL-NOTIF-01 | Bell shows unread count, lists notifications, unread highlighted; empty state for other user | PASS |  |  |
| UIO-SHELL-NOTIF-02 | Clicking a notification marks it read (DB), closes the list and follows its link | PASS |  |  |
| UIO-SHELL-NOTIF-03 | Mark all read clears the badge and reads everything in DB | PASS |  |  |
| UIO-SHELL-NOTIF-04 | Email-copy checkbox persists the preference | PASS |  |  |
| UIO-SHELL-PROFILE-01 | Profile menu shows name, email, role; entries: Change password, Account security, Calendar sync, Sign out | PASS |  |  |
| UIO-SHELL-PWD-01 | Change password modal: opens/closes (X, backdrop); validation: min length, mismatch | FAIL | defect | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PWD-02 | Wrong current password shows server error and does not change the password | FAIL | defect | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PWD-03 | Correct change: success state, modal auto-closes, new password logs in, old one fails; user stays signed in | FAIL | defect | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PWD-04 | Forgot-password link inside the modal requests a reset (token row created) and confirms | FAIL | defect | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PROFILE-02 | Sign out: back on /login, stored user cleared, API session invalid; browser Back does not reopen app | PASS |  |  |
| UIO-SHELL-ERR-01 | A page whose API returns 500 shows an error state instead of crashing the shell (Campaigns list) | PASS |  |  |
| UIO-SHELL-RESP-01 | Narrow viewport (390px) login + dashboard do not crash | PASS |  |  |
