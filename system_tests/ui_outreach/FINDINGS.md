# UI outreach/admin functional test findings

Suite: `system_tests/ui_outreach` (Playwright, real login/cookies, assertions on UI + DB/API). Run: `node run_all.mjs` (or `node run_all.mjs <suite...>`; merges `out/*/results.json`). Results: `results.md` / `results.json`. Coverage matrix: `SCREEN_COVERAGE.md`.

## Counts (final)
196 cases: **193 PASS, 3 FAIL (all defect-backend), 0 blocked**. Suites: auth 43, shell, security 9, campaigns_list, wizard 12, details 9, library 14, prospects 10, mailboxes 12, inboxdomain 12, admin 18.
Environment items (not failures): no SES/OpenAI/IMAP/DNS; LLM-absent paths were asserted to degrade gracefully (templates are generated from blueprints, regenerate/persona-generate/classify do not crash).

## Open defects (backend, report only)
1. **Medium - UIO-WIZ-05**: with the LLM absent, wizard "Process & Generate" for a 2-step sequence stores only 1 template (DB: 2 `email_sequences`, 1 `email_templates`). The follow-up step has no content, the wizard shows "1 EMAILS" and still launches. `POST /campaigns/{id}/generations` (app/routers/campaign_wizard_router.py ~L300-330) -> blueprint fallback ignores `sequence_steps`.
2. **Medium - UIO-AUTO-02b**: Automation Rules "Action: Pause Campaign" sends `action_type=PAUSE_CAMPAIGN`; the enum (app/models/automation_rule.py L21-25) has no such value, API returns 422 and the UI only says "Failed to create rule. Please check the fields." Contract mismatch (pause is `PAUSE_PROSPECT` server side).
3. **Low - UIO-MBX-06**: `PUT /inboxes/{id}` accepts negative `max_emails_per_day`/`daily_limit` (200; stored -5/-3). No lower-bound validation in schema/router; UI number input also has no `min`.
Minor observations (no failing case): custom_permissions reset stores JSON `null` rather than SQL NULL; unknown URLs redirect Home (no 404 page); Change-password modal ignores Escape; Campaigns "No campaigns yet" is also shown for a search with no match; Campaign preview signature in the wizard uses placeholder "[Your Name]"; AI email writer's "3-Email Sequence" is canned client-side text.

## Frontend defects found and FIXED (Vector_Frontend/src; build OK, web redeployed, re-run green)
1. `pages/Campaigns.jsx`: pause/resume/delete failures were silent -> added dismissible error banner (LIST-08).
2. `pages/Campaigns.jsx`: **bulk Pause/Resume/Delete sent only one request** (loop of `mutate()` on a single useMutation) -> now `Promise.allSettled` over the API calls (LIST-07).
3. `components/campaigns/tabs/LeadListTab.jsx`: **Export button had no handler** -> wired to `reportsApi.exportProspectsCsv` (DET-02).
4. `pages/AutomationRules.jsx`: **page crash `templates.map is not a function`** when choosing a campaign (API returns `{templates:[...]}`) -> normalised (AUTO-*).
5. `pages/AIEmailGenerator.jsx`: classification failure was silent -> inline error (AIGEN-03).
6. `pages/ProspectValidate.jsx`: after Confirm Upload the new list was missing until reload (stale react-query cache) -> invalidate prospect-lists/stats (PROS-05).
7. `components/campaigns/tabs/InboxTab.jsx`: Manager/Agent opening Inbox always triggered a 403 on `GET /inboxes` -> only admins call it (SHELL-NAV-manager/agent); and existing conversations were hidden behind the "IMAP not configured" guide -> guide shown only when there are no conversations (INBOX-00).
8. `components/layout/Sidebar.jsx` + `App.jsx`: "Campaign analytics" was shown/routable for users without `view_analytics` (page fired 403 `/reports/global-analytics`) -> menu item + route now require `view_analytics` (AGENT redirected Home).
9. `components/layout/Sidebar.jsx`: **Change Password modal rendered inside the sticky blurred header** (backdrop-filter becomes the containing block of `position:fixed`), so it was clipped/unreachable (close button off-screen) -> rendered via `createPortal` to `document.body` (SHELL-PWD-01..04).

## Test-side fixes (not product issues)
Case-insensitive button names (CSS uppercase), shared-handler dialog stacking, route mocks that intercepted the document request, first-visible vs bulk-bar buttons, `lg:hidden` View icon, history.state surviving same-URL navigation, JSON-null permissions, react-query retry delay on error states.

## Coverage gaps (see SCREEN_COVERAGE.md)
PlatformAdmin screens (no PLATFORM_ADMIN account), IMAP/SES/DNS-dependent results, inbound-reply "Create lead" rail, wizard draft-resume and upload-inside-wizard, subsequence edit-save/reschedule, per-user report filter values, no UI exists for list rename, domain add, invite resend, hierarchy fields (not-implemented).
