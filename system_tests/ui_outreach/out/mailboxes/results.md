# UI outreach/admin - mailboxes results

PASS 11 · FAIL 1 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-MBX-01 | Email accounts renders: stats tiles, inbox row (SMTP, warmup off), sender-identity card prompting setup | PASS |  |  |
| UIO-MBX-02 | Sender identity: empty save is blocked with a message; valid save persists to the company profile (DB) and shows in the card; Cancel discards | PASS |  |  |
| UIO-MBX-03 | Filters and search: Warmup On hides the (warmup-off) inbox, Warmup Off shows it, search by email narrows, no-match empty state | PASS |  |  |
| UIO-MBX-04 | Warmup toggle on the row flips warmup_enabled in the DB and back | PASS |  |  |
| UIO-MBX-05 | Detail drawer: Overview/Connection/Settings/Activity tabs render; Test sending sign-in against an unreachable SMTP host reports a clear failure | PASS |  |  |
| UIO-MBX-06 | Settings tab: change daily limit / delay and Save Settings persists to DB; invalid value rejected | FAIL | defect | negative limit stored |
| UIO-MBX-07 | Connect Mailbox wizard: provider step -> SMTP form; validation for empty / invalid email and missing host | PASS |  |  |
| UIO-MBX-08 | Connect SMTP mailbox with unreachable host: ends in a clear result (connected-unverified or explicit error), row appears iff created; no crash | PASS |  |  |
| UIO-MBX-09 | Microsoft 365 connect button when MS365 is not configured shows a clear message (no redirect to undefined) | PASS |  |  |
| UIO-MBX-10 | Alert Preferences modal opens, saves, closes; Run Warmup gives feedback | PASS |  |  |
| UIO-MBX-11 | Delete mailbox: Cancel keeps it; Delete removes it (DB) and the row | PASS |  |  |
| UIO-MBX-12 | Roles: MANAGER and AGENT cannot open Email accounts (redirect Home) | PASS |  |  |
