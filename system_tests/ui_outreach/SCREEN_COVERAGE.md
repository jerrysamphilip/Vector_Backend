# Screen coverage: outreach + admin UI (Playwright, system_tests/ui_outreach)

Y = covered by an automated case, N = not covered, n/a = the screen has no such control. Case IDs are in results.md.
Roles: O = Super Admin (owner), A = Admin, M = Manager, G = Agent.

| Screen / route | Renders | Actions tested | Roles tested | Notes / gaps |
|---|---|---|---|---|
| Login `/login` | Y (AUTH-01) | empty submit, wrong pw, suspended, locked (10 fails -> 429), show/hide pw, forgot link, 500, success, authed redirect, logout+Back (AUTH-02..11) | O,G | "remember me" has no checkbox (dead code), not testable |
| MFA second step (Login/MagicLogin) | Y | short/wrong code, TOTP, recovery code (single use), cancel, via magic link (AUTH-60..65) | O | |
| ForgotPassword `/forgot-password` | Y | empty, known email (token row), unknown email (no enumeration), 500 (AUTH-20..23) | n/a | email send itself is environment (SES) |
| ResetPassword `/reset-password` | Y | no token, short, mismatch, success (+old pw rejected), reuse, expired (AUTH-30..35) | n/a | |
| AcceptInvite `/accept-invite` | Y | no token, validation, bad token, expired, success, reuse (AUTH-40..45) | n/a | |
| MagicLogin `/magic-login` | Y | no token, bad, expired, valid (INVITED activation), replay, MFA (AUTH-50..56, 65) | n/a | |
| SetPassword `/set-password` | Y | validation, wrong temp pw, success (AUTH-54,55) | G-like | |
| Session expiry | Y | access cookie deleted (silent refresh), all cookies (-> login), deep link return (AUTH-70..72) | G,A | |
| Home `/app/dashboard` (My day rail) | Y (HOME-01/02) | empty state, task listed, complete task -> DB | O | replies rail ("Create lead") N (needs inbound reply seed; CRM agent area) |
| Dashboard / Campaign analytics `/app/analytics` | Y (DASH-*) | totals vs DB, Daily/Weekly/Monthly/Yearly, alerts bell, table, View Campaigns, API 500, role guard | O,G | date-range picker is the four buttons; no custom range control exists |
| Campaigns list `/app/campaigns` | Y | empty state, rows/status/counts vs DB, search, column picker (+persist), pause/resume, delete (cancel/confirm), row nav (draft->wizard), bulk pause/resume, error surfacing, API 500 (LIST-*) | O,M,G(redirect) | status filter, sort, pagination, duplicate: n/a (not implemented in UI) |
| CreateCampaign wizard `/app/campaigns/new` | Y | step1 validation + persistence of identity/sender/CTA/window/mode/start date, step2 follow-up + wait days + Back, step3 list/deselect, generate (LLM absent), edit subject/body (rich text), single-brace guard, regenerate, persona generate, live preview substitution, launch review, EXECUTE, missing sender identity message (WIZ-01..12) | O | Upload-new-list inside wizard N (covered via Prospects upload); draft resume prompt N; timezone picker search N |
| CampaignDetails `/app/campaigns/:id` | Y | tabs Analytics/Prospects/Subsequence/Schedule/Trigger logs/Sending Accounts/Inbox, add prospects, remove prospect, export CSV, approve all, pause/resume, send-now (confirm/cancel; outcome dialog or visible error, counts match API, DET-08), bad id, agent guard (DET-*, 404) | O,G | subsequence editing save, schedule reschedule N; "enroll/remove" Y |
| TemplateLibrary `/app/templates` | Y | create (merge field), validation, preview, edit, search/category, delete (cancel/confirm), shared visibility + API authz (TPL-*) | O,G | "approve" is not a library feature (campaign templates are approved in Subsequence tab: Approve All Y) |
| Prospects `/app/prospects` | Y | empty, New List validation, bad file type, upload preview (accepted/needs attention), direct URL redirect, confirm -> DB members, search, view members, stats vs DB, delete list, revalidate bounced access, upload 500 (PROS-*) | O,G(redirect) | list rename: n/a; add-members-to-list from this screen N (Lists screen belongs to the CRM suite); inline row edit in preview N |
| ProspectValidate `/app/prospects/validate` | Y | per-row reasons, Confirm, refresh-redirect | O | inline edit/Revalidate N |
| EmailAccounts `/app/inboxes` | Y | stats, sender identity (validation + save), filters/search, warmup toggle, drawer tabs, test SMTP (unreachable), settings save, connect wizard (validation, SMTP connect, password at rest), MS365 unconfigured message, alert prefs, run warmup, delete (MBX-*) | O,M,G(redirect) | edit credentials modal N (wizard covers fields); IMAP test needs IMAP server (environment) |
| DomainHealth `/app/domain-health` | Y | list, KPIs, trend filter, details modal, run diagnostics, delete (confirm), agent access (DOM-*) | O,G | add-domain UI does not exist (domains derive from inboxes); alerts list N; DNS-record values need DNS (environment) |
| Inbox `/app/inbox` | Y | conversations, IMAP-guide regression, search, open thread -> read, reply (Enter) + draft kept on failure, sync, tenant isolation (INBOX-*) | O | filters/tabs beyond search N; attachments N |
| AutomationRules `/app/automation` | Y | empty, create (validation), toggle (repaired PUT), search, delete, "Pause this contact" (PAUSE_PROSPECT) rule persisted in automation_rules (AUTO-*, AUTO-02b) | O | |
| AIEmailGenerator `/app/ai-email` | Y | classify enable/disable, classify result, sequence tab, compliance, failure UX (AIGEN-*) | O | page "generate sequence" is client-side canned text |
| Reports `/app/reports` | Y | KPIs, range buttons -> requests, 3 exports download, user filter, API 500, role guards (REP-*) | O,M,G | per-user filter result values N |
| TeamManagement `/app/team` | Y | list/KPIs, invite (validation, role, success, duplicate), search/filter, role change, deactivate/reactivate (+login blocked), revoke, permissions modal + reset, delete, admin limits, API 500 (TEAM-*) | O,A,G(redirect) | resend invite and hierarchy fields: n/a (not in UI) |
| AccountSecurity `/app/account/security` | Y | enable (QR+secret, wrong/right code), recovery codes (download/copy), policy switch + forced setup, regenerate, disable (password, code, recovery), API failure (SEC-*) | O,A | `/admin/security` N (needs PLATFORM_ADMIN) |
| ChangePassword modal | Y | open/close, validation, wrong current, success, forgot link (PWD-*) | O | |
| Help panel + Help Center | Y | button, ?, F1, Esc, search, context article for 24 screens, all 42 article deep links, unknown id, Go-to-screen (HELP-*) | O | |
| GlobalSearch (Ctrl+K) | Y | focus, min length, results, navigation, no-match, Enter, Esc, 500, tenant isolation (SEARCH-*) | O | |
| NotificationBell | Y | unread badge, list, click -> read + navigate, mark all, email pref persistence, empty (NOTIF-*) | M,G | |
| ProfileMenu | Y | entries, security/connections nav, sign out (PROFILE-*) | A,O | |
| Sidebar | Y | per-role item visibility, accordion groups, every link navigates and renders for each role (SB-*, NAV-*) | O,A,M,G | |
| 404 / unknown routes (NotFound) | Y (SHELL-404) | unknown /app/* renders the 404 page inside the shell (URL kept, names the path), "Back to Home" link -> /app/dashboard, nested unknown path; unknown root route -> Home; bad campaign id | O | |
| Route guards | Y (GUARD-*) | agent/manager restricted URLs, /admin/* for tenant users, API 403 parity | M,G,O | |
| PlatformAdmin `/admin/*` | N | only the guard (tenant users redirected) | n/a | needs a PLATFORM_ADMIN account (none available) |
| Narrow viewport | Y (RESP-01) | 390px renders | O | |
