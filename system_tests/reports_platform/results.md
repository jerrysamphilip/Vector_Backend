# reports_platform results

PASS 93 · FAIL 3 · BLOCKED 9

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| RP-001 | GET /health returns 200 healthy | PASS |  |  |
| RP-002 | GET /health/ready reports database ok | PASS |  |  |
| RP-003 | GET /metrics serves Prometheus exposition | PASS |  |  |
| RP-004 | Every report/platform endpoint rejects anonymous calls with 401 | PASS |  |  |
| RP-005 | A forged/garbage bearer token is rejected (401) on every report | PASS |  |  |
| RP-010 | Owner dashboard: role view SALES_HEAD and every KPI equals independently computed value | PASS |  |  |
| RP-011 | Sales-head dashboard breaks down by Business Development teams with exact per-team KPIs | PASS |  |  |
| RP-012 | Level 1 (Sales Head) dashboard covers all teams (same KPIs as admin) | PASS |  |  |
| RP-013 | Business Development dashboard: role view BUSINESS_DEVELOPMENT, KPIs limited to own team | PASS |  |  |
| RP-014 | Business Executive dashboard: own+report KPIs and work queue (SQLs, deals closing in 30 days) | PASS |  |  |
| RP-015 | Dashboard member filter narrows to that person and their team | PASS |  |  |
| RP-016 | Member filter outside the caller's hierarchy is refused (404) | PASS |  |  |
| RP-017 | date_from after date_to is rejected with 400 | PASS |  |  |
| RP-018 | A period before any activity shows zero period counts (open pipeline stays point-in-time) | PASS |  |  |
| RP-020 | Leads & SQL report for owner: totals, stage, source, owner and lead-to-SQL rate exact | PASS |  |  |
| RP-021 | Leads & SQL report for bd1: totals, stage, source, owner and lead-to-SQL rate exact | PASS |  |  |
| RP-160 | KPI Lead-to-SQL conversion = SQLs / leads x 100 (reported value matches formula) | PASS |  |  |
| RP-025 | Pipeline report by owner: open count/amount, weighted, won, lost, win rate exact | PASS |  |  |
| RP-026 | Pipeline report by expected close month (open deals; undated grouped separately) exact | PASS |  |  |
| RP-027 | Pipeline report client-type filter (existing clients) exact | PASS |  |  |
| RP-028 | Pipeline totals reconcile: report = /pipeline/revenue = sum of /opportunities (open) | PASS |  |  |
| RP-030 | Funnel sent>replied>lead>SQL>opportunity>won counts exact (owner) | PASS |  |  |
| RP-031 | Funnel step conversion = count / previous step x 100 (none shown above 100%) | PASS |  |  |
| RP-032 | Funnel 'from first step' percentages stay within 0-100% (same rule as step ratios) | FAIL | defect | from_first above 100%: lead=140.0, opportunity=260.0 (app/routers/sales_reports_router.py funnel(): from_previous is capped at 100 but from_first is not) |
| RP-033 | Funnel sent>replied>lead>SQL>opportunity>won counts exact (bd1) | PASS |  |  |
| RP-040 | Quarter-wise forecast FY 2026-27: won, open, weighted, lost, forecast per quarter exact | PASS |  |  |
| RP-041 | Fiscal quarters are Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar with labels 'Qn FY 2026-27' | PASS |  |  |
| RP-042 | FY boundaries: 31 Mar falls in previous FY Q4, 1 Apr starts the new FY Q1 | PASS |  |  |
| RP-043 | Financial-year-wise forecast (FY 2025-26 .. 2028-29) and FY total exact | PASS |  |  |
| RP-044 | Won vs weighted: forecast = won + weighted open; best case = won + full open pipeline | PASS |  |  |
| RP-045 | Forecast categories: Commit (incl. manual override), Best case, Pipeline; commit = won + commit deals | PASS |  |  |
| RP-046 | Open deals without a close date are excluded from buckets and reported separately | PASS |  |  |
| RP-047 | Forecast totals reconcile with GET /opportunities for the same close-date range | PASS |  |  |
| RP-048 | Forecast filtered to existing clients is exact (and carries no per-rep target) | PASS |  |  |
| RP-049 | Forecast member filter (BD head) = that head's team only | PASS |  |  |
| RP-050 | Forecast is scoped to the caller's hierarchy (BD, executive, report, L1) | PASS |  |  |
| RP-051 | Forecast carries quarter/FY targets and attainment = won / target x 100 | PASS |  |  |
| RP-052 | Default forecast year is the current FY (2026) for today 2026-10-10 | PASS |  |  |
| RP-053 | Invalid fy parameter is rejected (422) | PASS |  |  |
| RP-060 | Team scorecard per rep: activities, emails, leads, SQLs, deals, won, lost, pipeline exact | PASS |  |  |
| RP-061 | Team totals and stage conversion rates (lead->SQL, SQL->opportunity, win rate) exact | PASS |  |  |
| RP-062 | Team performance for bd1 lists exactly ['bd1', 'ex1', 'ex2', 'mr1'] | PASS |  |  |
| RP-063 | Team performance for ex1 lists exactly ['ex1', 'mr1'] | PASS |  |  |
| RP-064 | Team performance for head lists exactly ['bd1', 'bd2', 'ex1', 'ex2', 'ex3', 'head', 'mr1', 'owner'] | PASS |  |  |
| RP-065 | Manager cannot open another team's (or their manager's) scorecard via member filter (404) | PASS |  |  |
| RP-066 | Logged calls, meetings and completed tasks count as rep activity | PASS |  |  |
| RP-089 | Target vs actual per rep (FY and quarters 1, 2): won, commit, best case, attainment, gap, rank | PASS |  |  |
| RP-090 | Leaderboard ranks reps by revenue won, then deals, SQLs, activity | PASS |  |  |
| RP-092 | Campaign ROI: emailed, replies, leads, SQLs, opportunities, pipeline and revenue per campaign exact | PASS |  |  |
| RP-098 | Custom report (opportunities, sum of amount by stage) equals independently summed values | PASS |  |  |
| RP-098b | Custom matrix report (deals by owner x status, close date in FY) exact | PASS |  |  |
| RP-099 | Shared custom report runs within each viewer's hierarchy; executives (L3 agents) cannot build reports | PASS |  |  |
| RP-099b | Custom report rejects unknown grouping/object with 400 | PASS |  |  |
| RP-110 | Another tenant's admin cannot report on this tenant's users (member=foreign id -> 404) on every report | PASS |  |  |
| RP-111 | Other tenant's reports contain only its own data (exact totals; ours excluded) and ours exclude its 99,999 | PASS |  |  |
| RP-112 | Cross-tenant object access (deal, timeline, saved report, target write) returns 404 | PASS |  |  |
| RP-113 | Hierarchy: users cannot widen any report to a peer's or their manager's data (404) | PASS |  |  |
| RP-114 | Campaign analytics (v1 reports) refuse a BD manager reading another team's rep (user_id filter) | FAIL | defect | /reports/dashboard-summary?user_id=<other team rep> -> 200; campaign-comparison -> 200 (app/routers/reports_router.py only forces AGENTs to their own id; MANAGERs may pass any user_id) |
| RP-115 | Role checks: agents cannot change sales settings, workflow rules, products or their own target; a BD manager can set targets only inside their team | PASS |  |  |
| RP-088 | Targets per rep per quarter: list is scoped to the caller's team with quarter amounts | PASS |  |  |
| RP-100 | Quote lines: qty x unit price less line discount; proposal amount = line total (3 x 1,200 -10% + 500) | PASS |  |  |
| RP-101 | Proposal PDF downloads as a valid PDF document | PASS |  |  |
| RP-101b | Proposal PDF is not available across tenants (404); negative prices rejected (400) | PASS |  |  |
| RP-093 | Amount/revenue fields are blank on every report and deal API for a role configured to hide them | PASS |  |  |
| RP-093b | Hidden-amount viewer still gets counts (open deals) so reports stay usable | PASS |  |  |
| RP-094 | Hidden-amount viewer cannot change amounts, run a shared money report or download a quote PDF (403) | PASS |  |  |
| RP-093c | Roles not listed keep seeing amounts | PASS |  |  |
| RP-095 | Workflow: amount rises above 50,000 -> alert to owner and manager + task for owner | PASS |  |  |
| RP-096 | Workflow: stage changes to Negotiation -> rule sets next step | PASS |  |  |
| RP-097 | Workflow: inactive rule never fires; run counts tracked; invalid field/threshold rejected (400) | PASS |  |  |
| RP-120 | Audit: owner, stage, amount and close-date changes are logged with old/new value, user and time | PASS |  |  |
| RP-083 | Opportunity keeps stage history with time in stage (BR-SF-04) | PASS |  |  |
| RP-121 | Audit: lead owner change logged with user and time | PASS |  |  |
| RP-084 | Closing a deal as won or lost without a reason is refused (400) | PASS |  |  |
| RP-084b | Stale deals: no activity for N days or past close date are flagged (list filter + flag) | PASS |  |  |
| RP-086 | In-app notifications: assignment alerts reach the new owner; mark-as-read lowers unread count | PASS |  |  |
| RP-087 | Email notifications: preference saved and the worker's email loop processes the notification | PASS |  |  |
| RP-087b | Email notification actually delivered to the user's mailbox | BLOCKED | env | SENDER_EMAIL is empty in the worker; local stack has no mail sink - verify in UAT |
| RP-091 | Template library: merge fields fill from the contact; shared within tenant, 404 across tenants, only owner/admin may edit | PASS |  |  |
| RP-080 | One-click lead from a campaign reply: lead linked to contact, owner and campaign; duplicate refused | PASS |  |  |
| RP-081 | Disqualified lead keeps its reason and a recycle date (30 days) and shows in the report | PASS |  |  |
| RP-082 | Round-robin assignment alternates unowned leads across the pool | PASS |  |  |
| RP-085 | Opportunity timeline shows logged activities (call, meeting) and its tasks | PASS |  |  |
| RP-022 | Report periods are inclusive whole days: 1 Sep 00:00 and 30 Sep 23:59:59 in, 31 Aug / 1 Oct out | PASS |  |  |
| RP-102 | Calendar/email sync API is present: lists connections, refuses unknown provider (404) | PASS |  |  |
| RP-102b | Connect Google/Microsoft account and sync calendar + email | BLOCKED | env | start -> 400 {"detail": "Google sync is not set up on the server. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_SYNC_REDIRECT_URI."} (OAuth client not configured on local stack; needs real Google/M365 accounts) |
| RP-070 | BR-AI-01 AI contact discovery / enrichment endpoint exists | BLOCKED | not-implemented | No discovery/enrichment endpoint in /openapi.json (BRD marks it optional - C) |
| RP-103 | BR-SF-17 discount above threshold routed for approval | BLOCKED | not-implemented | Quote lines accept any discount 0-100% with no approval step (quotes_router.set_lines) |
| RP-104 | BR-SF-18 A/B test of email subject lines | BLOCKED | not-implemented | No A/B / variant endpoint in /openapi.json |
| RP-105 | BR-SF-19 single sign-on with Microsoft 365 | BLOCKED | not-implemented | Only /api/auth/google sign-in exists; Microsoft OAuth is for mailboxes/sync, not sign-in |
| RP-106 | BR-SF-20 REST API documented (OpenAPI) | PASS |  |  |
| RP-106b | BR-SF-20 outbound webhooks for other systems | BLOCKED | not-implemented | Only the inbound SES webhook exists; no webhook subscription API |
| RP-107 | BR-SF-05 stale-deal alert notification raised by hourly job | BLOCKED | env | Job runs hourly in vector-worker (app/jobs.py); not waited for in an automated run - see manual case |
| RP-161 | KPI SQL-to-win rate and forecast accuracy (actual / committed per quarter) reported directly | BLOCKED | not-implemented | No report returns these ratios; derivable from team-performance counts and forecast commit/won |
| RP-130 | Dashboards and reports load in under 3 s (worst of 3, owner, seeded tenant) | PASS |  |  |
| RP-150 | UI: owner signs in through the login form and lands in the app | PASS |  | http://localhost:8190/vector/app/dashboard |
| RP-151 | UI: sales dashboard tiles show the same KPIs as the API (pipeline, won, counts) | PASS |  | $139.5K \| $45K \| 2 deals \| 11 deals \| 3 became SQL |
| RP-152 | UI: forecast tab shows quarter and FY figures equal to the API | PASS |  | 13 values matched |
| RP-153 | UI: team performance lists every rep with revenue won as the API | PASS |  | 12 values matched |
| RP-154 | UI: targets & leaderboard tab shows rep targets as the API | PASS |  | 5 values matched |
| RP-155 | UI: dashboard and every report tab render without page errors or 5xx responses | PASS |  |  |
| RP-131 | 25 concurrent users x 60s mixed reads: 0% errors and p95 < 3 s | PASS |  |  |
| RP-140 | Login: 10 wrong passwords lock the account; even the right password gets 429 with Retry-After (~15 min) | PASS |  |  |
| RP-141 | Login: per-IP limit (60 attempts / 5 min) answers 429 from the 61st attempt | PASS |  |  |
| RP-142 | Per-IP limits cannot be bypassed by sending a different X-Forwarded-For on the published API port | FAIL | defect | A new X-Forwarded-For value from the same client gets 401 (not 429): client_ip() in app/core/rate_limit.py trusts the right-most X-Forwarded-For hop, which the client controls when it calls :8191 directly (TRUSTED_PROXY_HOPS=1, no proxy in front) |
