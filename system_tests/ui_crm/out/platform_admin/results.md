# ui_crm platform_admin results

PASS 18 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIF-PA-01 | Platform admin signs in through the login form and lands on /admin (Platform Administration, no tenant navigation) | PASS |  |  |
| UIF-PA-02 | Tenant roster: search narrows to the tenant; status ACTIVE, user / campaign counts match the DB | PASS |  |  |
| UIF-PA-03 | Search with no match shows "No tenants found for the current search." | PASS |  |  |
| UIF-PA-04 | Tenant detail: View opens the modal with the tenant name, user count and the team (owner email, SUPER ADMIN) | PASS |  |  |
| UIF-PA-05 | Suspend from the roster: badge SUSPENDED, Activate offered, DB status SUSPENDED | PASS |  |  |
| UIF-PA-06 | Suspended tenant: API login -> 403 "Tenant suspended"; the login form shows the error and stays on /login | PASS |  |  |
| UIF-PA-07 | A user signed in before the suspension is signed out or told the workspace is suspended on the next screen (no broken page) | PASS |  |  |
| UIF-PA-08 | Reactivate: Activate -> ACTIVE badge, DB ACTIVE, the tenant owner can sign in again | PASS |  |  |
| UIF-PA-09 | Add Tenant: required fields block an empty submit (no API call); valid form creates tenant + INVITED SUPER_ADMIN + magic link | PASS |  |  |
| UIF-PA-10 | Add Tenant duplicates: same tenant name -> "A tenant with this name already exists"; same admin email -> "A user with this email already exists" | PASS |  |  |
| UIF-PA-11 | Stats tiles: Total Tenants / Suspended match /api/admin/stats (read around the page load) | PASS |  |  |
| UIF-PA-12 | Tenant SUPER_ADMIN cannot reach the console: /admin and /admin/tenants redirect to /app/dashboard; /api/admin/* -> 403 | PASS |  |  |
| UIF-PA-13 | Platform admin is kept out of tenant screens: /app/contacts redirects to /admin; tenant API -> 403 | PASS |  |  |
| UIF-PA-14 | Scheduler card on /admin: current state shown; Pause / Resume ask for confirmation and flip the flag; original state restored | PASS |  |  |
| UIF-PA-18 | /admin/tenants shows the tenant list (no stats tiles); /admin/users lists tenant users with search | PASS |  |  |
| UIF-PA-15 | Scheduler switch (API, platform admin): stop -> paused + flag holds the admin email; start -> running; original state restored | PASS |  |  |
| UIF-PA-16 | Tenant admin / agent cannot pause or run the scheduler (403), flag unchanged; status is readable | PASS |  |  |
| UIF-PA-17 | Platform admin signs out from the profile popup: /login, and /admin then redirects to /login | PASS |  |  |
