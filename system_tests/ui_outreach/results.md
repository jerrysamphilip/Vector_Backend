# UI outreach/admin functional results

PASS 182 | FAIL 14 | BLOCKED 0

Failures by category: {"unclassified":14}

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-AUTH-01 | Login page renders email/password fields, Forgot password link, no problems | PASS |  |  |
| UIO-AUTH-02 | Empty submit shows "Email and password are required" and sends no request | PASS |  |  |
| UIO-AUTH-03 | Wrong password shows "Invalid email or password", stays on /login, no cookie session | PASS |  |  |
| UIO-AUTH-04 | Password show/hide toggle switches input type | PASS |  |  |
| UIO-AUTH-05 | Forgot password link navigates to /forgot-password | PASS |  |  |
| UIO-AUTH-06 | Suspended account: login shows "Account is suspended or inactive" | PASS |  |  |
| UIO-AUTH-07 | Locked after 10 failed attempts: UI shows "Too many failed sign-in attempts" (429), correct password also refused | PASS |  |  |
| UIO-AUTH-08 | Login API 500 degrades to a readable message (no crash / [object Object]) | PASS |  |  |
| UIO-AUTH-09 | Successful login lands on Home (/app/dashboard) with session cookies and stored user | PASS |  |  |
| UIO-AUTH-10 | Authenticated user opening /login is redirected to the app (PublicRoute) | PASS |  |  |
| UIO-AUTH-11 | Logout via profile menu returns to /login, clears stored user + cookies; Back does not reopen the app | PASS |  |  |
| UIO-AUTH-20 | ForgotPassword renders; empty email shows Email is required and sends nothing | PASS |  |  |
| UIO-AUTH-21 | Known email: generic success message and a reset-token row is stored (hash only) | PASS |  |  |
| UIO-AUTH-22 | Unknown email shows the same generic message (no enumeration) and stores nothing | PASS |  |  |
| UIO-AUTH-23 | Forgot password 500 -> readable error, no crash | PASS |  |  |
| UIO-AUTH-30 | ResetPassword without token: submitting shows "Invalid or missing reset token" | PASS |  |  |
| UIO-AUTH-31 | Short password rejected client-side (<8) | PASS |  |  |
| UIO-AUTH-32 | Mismatched confirmation rejected | PASS |  |  |
| UIO-AUTH-33 | Valid token: success message, redirect to /login, token marked used, new password works / old does not | PASS |  |  |
| UIO-AUTH-34 | Reusing the token shows "Invalid or expired reset token" | PASS |  |  |
| UIO-AUTH-35 | Expired token shows "Reset token has expired" | PASS |  |  |
| UIO-AUTH-40 | AcceptInvite without token: "Invalid or missing invitation token" | PASS |  |  |
| UIO-AUTH-41 | Validation: names required, password >= 8, confirmation matches | PASS |  |  |
| UIO-AUTH-42 | Invalid token -> server message shown (404 "Invalid or expired invitation") | PASS |  |  |
| UIO-AUTH-43 | Expired invitation -> "Invitation has expired" and invitation row EXPIRED | PASS |  |  |
| UIO-AUTH-44 | Valid invitation: account created (role/tenant from invite), redirect to /login, can then log in | PASS |  |  |
| UIO-AUTH-45 | Re-using the accepted invitation shows an error, no duplicate user | PASS |  |  |
| UIO-AUTH-50 | MagicLogin without token: "Missing magic login token" | PASS |  |  |
| UIO-AUTH-51 | MagicLogin with unknown token shows error (not blank) | PASS |  |  |
| UIO-AUTH-52 | Expired magic token shows "has expired" | PASS |  |  |
| UIO-AUTH-53 | Valid magic link on INVITED user: signs in, activates user, goes to /set-password; token single use | PASS |  |  |
| UIO-AUTH-54 | SetPassword validation (short, mismatch) and wrong temporary password error | PASS |  |  |
| UIO-AUTH-55 | SetPassword success: confirmation then Home; new password works | PASS |  |  |
| UIO-AUTH-56 | Used magic token cannot be replayed | PASS |  |  |
| UIO-AUTH-60 | MFA user: correct password shows the second step (no session yet) | PASS |  |  |
| UIO-AUTH-61 | Short code rejected client-side; wrong code shows server message | PASS |  |  |
| UIO-AUTH-62 | Correct TOTP completes sign-in to Home | PASS |  |  |
| UIO-AUTH-63 | Recovery code path signs in once; the same recovery code is then refused | PASS |  |  |
| UIO-AUTH-64 | Cancel on the second step returns to the email/password form | PASS |  |  |
| UIO-AUTH-65 | MFA user via magic link also gets the second step | PASS |  |  |
| UIO-AUTH-70 | Deleting only the access cookie: next navigation silently refreshes (stays signed in) | PASS |  |  |
| UIO-AUTH-71 | Deleting all session cookies redirects to /login and clears stored user | PASS |  |  |
| UIO-AUTH-72 | Unauthenticated deep link /app/campaigns redirects to /login, then back to the target after login | PASS |  |  |
| UIO-SHELL-SB-owner | Sidebar menu for OWNER: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-admin | Sidebar menu for ADMIN: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-manager | Sidebar menu for MANAGER: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-agent | Sidebar menu for AGENT: role-specific items visible/hidden, role badge, groups accordion | PASS |  |  |
| UIO-SHELL-SB-collapse | Sidebar group header toggles collapse/expand (aria-expanded) | PASS |  |  |
| UIO-SHELL-NAV-owner | Every visible sidebar link navigates and its page renders cleanly (OWNER) | PASS |  |  |
| UIO-SHELL-NAV-admin | Every visible sidebar link navigates and its page renders cleanly (ADMIN) | PASS |  |  |
| UIO-SHELL-NAV-manager | Every visible sidebar link navigates and its page renders cleanly (MANAGER) | PASS |  |  |
| UIO-SHELL-NAV-agent | Every visible sidebar link navigates and its page renders cleanly (AGENT) | FAIL | unclassified | Campaign analytics: link missing |
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
| UIO-SHELL-PWD-01 | Change password modal: opens/closes (X, backdrop); validation: min length, mismatch | FAIL | unclassified | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PWD-02 | Wrong current password shows server error and does not change the password | FAIL | unclassified | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PWD-03 | Correct change: success state, modal auto-closes, new password logs in, old one fails; user stays signed in | FAIL | unclassified | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PWD-04 | Forgot-password link inside the modal requests a reset (token row created) and confirms | FAIL | unclassified | locator.click: Timeout 12000ms exceeded. |
| UIO-SHELL-PROFILE-02 | Sign out: back on /login, stored user cleared, API session invalid; browser Back does not reopen app | PASS |  |  |
| UIO-SHELL-ERR-01 | A page whose API returns 500 shows an error state instead of crashing the shell (Campaigns list) | PASS |  |  |
| UIO-SHELL-RESP-01 | Narrow viewport (390px) login + dashboard do not crash | PASS |  |  |
| UIO-SEC-01 | Account security renders for Super Admin: 2FA Off, "Set up two-factor", workspace policy card (switch disabled until own 2FA on) | PASS |  |  |
| UIO-SEC-02 | Enable flow: QR + secret shown; Cancel returns; wrong code shows error; correct TOTP shows 10 recovery codes | PASS |  |  |
| UIO-SEC-03 | Recovery codes: Download produces a file, Copy gives feedback, "I've saved them" hides them and shows the 2FA On state with remaining count | PASS |  |  |
| UIO-SEC-04 | Policy switch now enabled; turning it on requires 2FA for everyone (DB tenants.require_mfa=1); admin is forced to the security screen | PASS |  |  |
| UIO-SEC-05 | Turning the policy off releases admins again (require_mfa=0) | PASS |  |  |
| UIO-SEC-06 | New recovery codes: needs a valid current code; success replaces all codes (old code no longer works) | PASS |  |  |
| UIO-SEC-07 | Disable: requires password + code; wrong password -> error; correct -> 2FA Off in DB, secret and codes wiped | PASS |  |  |
| UIO-SEC-08 | Disable with a recovery code instead of an authenticator code works | PASS |  |  |
| UIO-SEC-09 | Security screen API failure shows an error note; Agent can open own security page | PASS |  |  |
| UIO-CAMP-HOME-01 | Home (My day rail) renders empty state for a new workspace: "Nothing due today", no stale deals | PASS |  |  |
| UIO-CAMP-LIST-EMPTY | Campaigns list empty state: "No campaigns yet" + Create New Campaign button works | PASS |  |  |
| UIO-CAMP-DASH-EMPTY | Campaign analytics (Dashboard) empty workspace: zeros not NaN, range buttons render | PASS |  |  |
| UIO-CAMP-HOME-02 | Home My day rail lists a due task; ticking it marks it DONE in the DB and removes it | PASS |  |  |
| UIO-CAMP-LIST-01 | Campaigns list shows every campaign with correct status badge and prospect count (matches DB) | PASS |  |  |
| UIO-CAMP-LIST-02 | Search filters rows (server-side, debounced) and clearing restores; no-match shows empty state | PASS |  |  |
| UIO-CAMP-LIST-03 | Column picker toggles columns, persists in localStorage across reload, Reset restores defaults | PASS |  |  |
| UIO-CAMP-LIST-04 | Row Pause on ACTIVE campaign -> PAUSED (DB + badge), Resume -> ACTIVE | PASS |  |  |
| UIO-CAMP-LIST-05 | Delete: cancelling the confirm keeps the campaign; accepting removes it (UI + DB) | PASS |  |  |
| UIO-CAMP-LIST-06 | Row click: DRAFT opens the wizard in edit mode, ACTIVE opens details; View icon opens details | FAIL | unclassified | locator.click: Timeout 12000ms exceeded. |
| UIO-CAMP-LIST-07 | Select-all + bulk bar: Pause selected (confirm) pauses active campaigns in DB; clear selection | FAIL | unclassified | statuses PAUSED/ACTIVE |
| UIO-CAMP-LIST-08 | Pause API failure surfaces feedback to the user (not silent) | FAIL | unclassified | locator.click: Timeout 12000ms exceeded. |
| UIO-CAMP-LIST-09 | Campaigns list: API 500 shows an explicit error (not the empty state) | PASS |  |  |
| UIO-CAMP-DASH-01 | Dashboard shows real totals for the workspace (campaign count, status breakdown) consistent with DB | PASS |  |  |
| UIO-CAMP-DASH-02 | Dashboard range buttons (Daily/Weekly/Monthly/Yearly) change the chart without errors; alerts bell opens | PASS |  |  |
| UIO-CAMP-DASH-03 | Dashboard campaign table lists campaigns; rows and "View Campaigns" navigate | PASS |  |  |
| UIO-CAMP-DASH-04 | Dashboard API failure (global-analytics/dashboard-summary 500) degrades without crash | PASS |  |  |
| UIO-CAMP-ROLE-mgr | MANAGER sees the campaigns list and can open a campaign (manage_campaigns) | PASS |  |  |
| UIO-CAMP-ROLE-agent-dash | AGENT without view_analytics: no Campaign analytics link, URL redirects Home, no 403 calls | PASS |  |  |
| UIO-WIZ-01 | Step 1 validation: empty name -> "Campaign name is required"; name without inbox -> "Select at least one inbox" | PASS |  |  |
| UIO-WIZ-02 | Step 1 saves identity, sender details, CTA, send window, distribution + start date to the campaign row | PASS |  |  |
| UIO-WIZ-03 | Step 2 sequence: add follow-up with wait days, saved as 2 steps (wait_days persisted); Back keeps step 1 values | PASS |  |  |
| UIO-WIZ-04 | Step 3 prospects: list selection, deselect one contact -> "3 of 4 contacts selected" | PASS |  |  |
| UIO-WIZ-05 | Process & Generate (LLM absent): templates generated for EVERY sequence step (2 emails) | FAIL | unclassified | 1 template(s) stored for a 2-step sequence (follow-up step has no content) |
| UIO-WIZ-06 | Edit subject (textarea) persists to DB; Cancel discards; single-brace variable triggers the warning dialog | PASS |  |  |
| UIO-WIZ-07 | Edit body in the rich-text editor persists to DB | PASS |  |  |
| UIO-WIZ-08 | Live Preview substitutes real prospect data (name/company), and prospect switcher works | PASS |  |  |
| UIO-WIZ-09 | Regenerate subject with LLM absent: sensible result or error, no crash / [object Object] | PASS |  |  |
| UIO-WIZ-10 | Click an undetected persona -> generates or shows a clear error (LLM absent) | PASS |  |  |
| UIO-WIZ-11 | Launch step shows correct specs; Back to Edit returns; EXECUTE launches: ACTIVE, 3 enrolled (excluded contact skipped), redirect to list | PASS |  |  |
| UIO-WIZ-12 | Launch without sender identity shows a clear message and does not activate the campaign | PASS |  |  |
| UIO-DET-01 | Details header + Analytics tab: name, ACTIVE badge, enrolled counts and sequence cards match DB | PASS |  |  |
| UIO-DET-02 | Prospects tab: lists enrolled prospects, search filters, status pills carry counts, Export works | PASS |  |  |
| UIO-DET-03 | Prospects tab: Add Prospects (pick list) enrolls the contact (DB + table); Remove (confirm) cancels enrollment | PASS |  |  |
| UIO-DET-04 | Subsequence tab shows both steps with approval state; Approve All works; Edit step opens editor | PASS |  |  |
| UIO-DET-05 | Schedule tab shows queued emails per step/day; Trigger logs shows CREATE/LAUNCH audit events | PASS |  |  |
| UIO-DET-06 | Sending Accounts tab lists the campaign inbox; Inbox tab renders (empty state) and Sync button reports a clear error with no IMAP | PASS |  |  |
| UIO-DET-07 | Pause Campaign / Resume Campaign buttons change status (DB + badge) | PASS |  |  |
| UIO-DET-08 | Send Now: confirm dialog, then result summary dialog; cancel path sends nothing | PASS |  |  |
| UIO-DET-09 | Role AGENT cannot open a campaign by URL (redirect Home) | PASS |  |  |
| UIO-TPL-01 | Template library empty state renders; New template opens modal; Cancel closes without saving | PASS |  |  |
| UIO-TPL-02 | Create template: merge-field insert, save -> row in list and in DB with subject/body/category/shared | PASS |  |  |
| UIO-TPL-03 | Validation: saving a template with empty name shows a server error inside the modal (no crash) | PASS |  |  |
| UIO-TPL-04 | Preview renders with sample values; Edit updates DB; category filter + search narrow the list | PASS |  |  |
| UIO-TPL-05 | Agent sees shared template but only the owner can edit/delete (no edit buttons for agent; API refuses) | PASS |  |  |
| UIO-TPL-06 | Delete: dismissing confirm keeps it; accepting removes row and DB record | PASS |  |  |
| UIO-AUTO-01 | Automation empty state; Create rule modal validation (required fields) keeps modal open | PASS |  |  |
| UIO-AUTO-02 | Create Send-AI-Template rule: row appears; DB has trigger/action/template/delay and is active | PASS |  |  |
| UIO-AUTO-02b | Create "Pause Campaign" rule (offered in the Action dropdown) succeeds | FAIL | unclassified | UI offers Pause Campaign but POST /automation-rules/ rejects action_type PAUSE_CAMPAIGN (422); user only sees "Failed to create rule. Please check the fields." |
| UIO-AUTO-03 | Toggle rule off/on (the repaired PUT): DB is_active flips and UI reflects it | PASS |  |  |
| UIO-AUTO-04 | Search filters rules; delete (confirm) removes row and DB record | PASS |  |  |
| UIO-AIGEN-01 | AI generator: Classify disabled until designation entered; classification shows persona + confidence | PASS |  |  |
| UIO-AIGEN-02 | Sequence tab: 3-email sequence is generated, shows compliance check, copy button works | PASS |  |  |
| UIO-AIGEN-03 | Classification failure (500) shows an error message to the user | PASS |  |  |
| UIO-PROS-01 | Prospect lists empty state: zero stats, New List button, no errors | PASS |  |  |
| UIO-PROS-02 | New List validation: missing name, missing file -> inline messages; unsupported file type rejected | PASS |  |  |
| UIO-PROS-03 | Upload CSV -> preview: 2 accepted / 2 need attention with per-row reasons (invalid email, duplicate) | PASS |  |  |
| UIO-PROS-04 | Opening the preview URL directly (no upload state) redirects back to Prospect lists, no crash | PASS |  |  |
| UIO-PROS-05 | Confirm Upload creates the list with exactly the accepted rows; list appears in the table without reload | FAIL | unclassified | new list not shown in table after confirm (stale list cache) |
| UIO-PROS-06 | Search lists, sort by name; View opens the members modal with the 2 contacts | PASS |  |  |
| UIO-PROS-07 | Stats tiles match DB (total contacts, in campaigns) | PASS |  |  |
| UIO-PROS-08 | Delete list: row trash -> confirm dialog; Cancel keeps, Delete removes list (DB)  | PASS |  |  |
| UIO-PROS-09 | Revalidate Bounced (admin only): opens modal for owner; agent is redirected away from the screen | PASS |  |  |
| UIO-PROS-10 | Upload API 500 shows a readable message in the modal | PASS |  |  |
| UIO-MBX-01 | Email accounts renders: stats tiles, inbox row (SMTP, warmup off), sender-identity card prompting setup | PASS |  |  |
| UIO-MBX-02 | Sender identity: empty save is blocked with a message; valid save persists to the company profile (DB) and shows in the card; Cancel discards | PASS |  |  |
| UIO-MBX-03 | Filters and search: Warmup On hides the (warmup-off) inbox, Warmup Off shows it, search by email narrows, no-match empty state | PASS |  |  |
| UIO-MBX-04 | Warmup toggle on the row flips warmup_enabled in the DB and back | PASS |  |  |
| UIO-MBX-05 | Detail drawer: Overview/Connection/Settings/Activity tabs render; Test sending sign-in against an unreachable SMTP host reports a clear failure | PASS |  |  |
| UIO-MBX-06 | Settings tab: change daily limit / delay and Save Settings persists to DB; invalid value rejected | FAIL | unclassified | negative limit stored |
| UIO-MBX-07 | Connect Mailbox wizard: provider step -> SMTP form; validation for empty / invalid email and missing host | PASS |  |  |
| UIO-MBX-08 | Connect SMTP mailbox with unreachable host: ends in a clear result (connected-unverified or explicit error), row appears iff created; no crash | PASS |  |  |
| UIO-MBX-09 | Microsoft 365 connect button when MS365 is not configured shows a clear message (no redirect to undefined) | PASS |  |  |
| UIO-MBX-10 | Alert Preferences modal opens, saves, closes; Run Warmup gives feedback | PASS |  |  |
| UIO-MBX-11 | Delete mailbox: Cancel keeps it; Delete removes it (DB) and the row | PASS |  |  |
| UIO-MBX-12 | Roles: MANAGER and AGENT cannot open Email accounts (redirect Home) | PASS |  |  |
| UIO-INBOX-00 | Inbox with existing conversations but no IMAP-configured mailbox still shows the conversations (not only the IMAP setup guide) | FAIL | unclassified | conversations hidden behind IMAP setup guide although 2 conversations exist (API returns them) |
| UIO-INBOX-01 | Unified Inbox lists both conversations (unread highlighted) with subject/snippet; no problems | PASS |  |  |
| UIO-INBOX-02 | Search narrows the list; opening a thread shows the message and marks it read (DB is_unread=0) | FAIL | unclassified | search did not filter |
| UIO-INBOX-03 | Reply form: empty reply does nothing; Enter sends a reply -> OUTBOUND message stored for that conversation | PASS |  |  |
| UIO-INBOX-04 | Reply failure (API 500) restores the draft text and does not crash | PASS |  |  |
| UIO-INBOX-05 | Sync button without IMAP reports a clear message; Refresh works | PASS |  |  |
| UIO-INBOX-06 | Tenant isolation: conversations of this workspace are not visible to another workspace | PASS |  |  |
| UIO-DOM-01 | Domain Health lists the workspace sending domain with SPF/DKIM/DMARC chips, KPIs consistent with inbox count | PASS |  |  |
| UIO-DOM-02 | Trend filter select: switching All domains <-> single domain re-renders the chart without errors | PASS |  |  |
| UIO-DOM-03 | Details modal shows DNS/campaign/inbox info; Run Diagnostics gives feedback; Close works | PASS |  |  |
| UIO-DOM-04 | Delete domain: confirm, removes the domain card and DB record (no add-domain UI exists: domains derive from inboxes) | PASS |  |  |
| UIO-DOM-05 | AGENT role can open Domain Health without forbidden-API noise | PASS |  |  |
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
