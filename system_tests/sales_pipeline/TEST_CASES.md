# Sales hierarchy, daily limit, leads & pipeline: test case catalogue

BRD v2.0 sections: §4 personas, §5.4 Sales Hierarchy (BR-SH-01/02), §5.5 Daily New-Contact Limit (BR-OV-01),
§5.6 Leads & SQL (BR-LD-01..06), §5.7 Sales Pipeline (BR-SP-01..04), plus the related "should" items BR-SF-01/02/03/04/05/12/15,
§6 NFR Security/Auditability and §7 "amounts visible only to permitted levels".

Automation: `run_sales_pipeline.py` (python3 stdlib + `../common.py`) and `ui_sales.mjs` (Playwright, called by the runner).
Each run creates two new tenants: **A** (main) and **B** (for cross-tenant checks). Results go to `results.json` and `results.md`,
and screenshots to `ui_shots/`.

**Tenant A hierarchy (fixture, configured through `PUT /team/hierarchy`)**

```
head (MANAGER, L1 Sales Head)
├── mgr1 (MANAGER + manage_prospects/export, L2 BD)
│   ├── ag1a (AGENT, L3 Exec)   ├── ag1b (AGENT, L3 Exec)   └── mr1 (AGENT, L4 Market Research)
├── mgr2 (MANAGER, L2 BD)
│   └── ag2a (AGENT, L3 Exec)
└── dlm (MANAGER, L2)  ── dl1, dl2 (AGENT, L3)    <- daily-limit users
owner = SUPER_ADMIN (workspace owner), loner = AGENT with no sales level (SP-047)
```

Type: **S** = System (an end-to-end user journey or a requirement checked through the public API/UI), **I** = Integration
(across components: lead/opportunity/contact lifecycle/history, scheduler and worker, imports and enrollment).
Priority: P1 = Must acceptance criterion or a security leak, P2 = Must detail, P3 = Should (BR-SF) or an edge case.

## 5.4 Sales hierarchy and role-based access

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| SP-001 | BR-SH-01, §4 | S | P1 | Tenant A users exist without levels | Admin calls `PUT /team/hierarchy/{id}` for each user (L1..L4 + manager); then `GET /team/hierarchy` | 200 each; read-back matches; level labels 1-4 incl. "Market Research" | Y |
| SP-002 | BR-SH-01 | S | P2 | Hierarchy configured | Put: L3 without manager; manager at same level; manager below the user; level 5; remove a manager who has reports; manager from another tenant | 400 for each; nothing changes | Y |
| SP-003 | BR-SH-01 | S | P1 | — | BD (MANAGER) and Sales Head try to change the hierarchy; tenant B admin targets a tenant A user | 403, 403, 404; DB unchanged | Y |
| SP-004 | BR-SH-02 | S | P2 | — | `GET /team/hierarchy` as exec, BD and Head | Exec sees self only; BD sees self + ag1a, ag1b, mr1; Head sees everyone | Y |
| SP-010 | — | S | — | — | Fixture: ag1a/ag1b/ag2a/mgr1/mgr2 each create a contact, a lead and a deal; mr1 (L4) creates a contact; one SQL lead in each team; a proposal on ag2a's deal | All created | Y |
| SP-021..023 | BR-SH-02 acceptance | S | P1 | Fixture | ag1a (exec) opens each user's contact, lead and deal by URL, and lists them | Only own records: 200 for own, 404 for others; lists contain own only | Y |
| SP-024..026 | BR-SH-02 acceptance | S | P1 | Fixture | Same as mgr1 (BD) | Own + ag1a + ag1b + mr1; not mgr2/ag2a | Y |
| SP-027..029 | BR-SH-02 | S | P1 | Fixture | Same as mgr2 | Own + ag2a only | Y |
| SP-030..032 | BR-SH-02 acceptance | S | P1 | Fixture | Same as head (L1 MANAGER) | Every team | Y |
| SP-033..035 | BR-SH-01/02 (L4) | S | P2 | Fixture | Same as mr1 (L4 Market Research) | Own contacts only | Y |
| SP-036 | BR-SH-02 acceptance ("by search") | S | P1 | Fixture | ag1a searches contacts/leads/deals for ag1b's unique name | 0 hits for the peer; own record is found | Y |
| SP-037 | BR-SH-02 acceptance ("by URL") | S | P1 | Fixture | ag1a PATCHes/converts/deletes a peer lead, edits a peer deal, reads its timeline, logs activity, adds or edits a proposal, creates a lead on a peer contact, edits a peer contact | 404 each; data unchanged | Y |
| SP-038 | BR-SH-02 | S | P2 | Fixture | ag1a creates a deal with a peer's contact | 400/404 | Y |
| SP-039 | BR-SH-02, BR-SP-02 | S | P2 | Proposal on ag2a deal | `GET /proposals` as ag1a and as mgr2 | Hidden from ag1a, visible to mgr2 | Y |
| SP-040 | BR-SH-02, BR-LD-01 | S | P1 | Fixture | Exec assigns own lead to a peer; BD assigns to another team; exec assigns a deal to another team; owner set to null | 403, 403, 403, 400; owner unchanged | Y |
| SP-041 | BR-SH-02, §6 audit | S | P2 | — | BD reassigns ag1b's lead to ag1a | 200; ag1a sees it, ag1b gets 404; owner change in history with user | Y |
| SP-042 | §4 L4 persona ("hand contacts over for assignment") | S | P2 | — | mr1 creates a contact; ag1a can't see it; mgr1 sets owner = ag1a; ag1a creates a lead | 404 before, 200 after, mr1 then 404, lead 201 | Y |
| SP-043 | BR-SH-02 acceptance ("Head sees all teams") | I | P1 | Fixture deals | `GET /pipeline/revenue` as head, mgr1, mgr2, ag1a | open_amount = sum of open deals of the viewer's scope | Y |
| SP-044 | §6 Security ("every … export") | S | P1 | mgr1 has manage_prospects | mgr1 exports contacts CSV | Contains team contacts, not mgr2's team | Y |
| SP-045 | Multi-tenancy | S | P1 | Tenant B | B admin reads, patches, converts and lists A's leads, deals, contacts and proposals | 404 each; lists, SQL queue, proposals and revenue are empty | Y |
| SP-046 | Multi-tenancy | S | P1 | Tenant B | A admin assigns a lead/deal to a tenant B user and moves a deal to a tenant B stage | 400 each | Y |
| SP-047 | BR-SH-02 / BRD v1 roles | S | P3 | User with no sales level | Agent without a level lists leads; admin and BD open its lead | Agent: own only; admin 200; BD 404 | Y |
| SP-048 | BR-SF-12, §7 | S | P2 | Settings: amounts hidden for AGENT and L4 | Agent reads deal, list, board, revenue and proposals, and writes an amount; L4 meta; BD reads deal | Agent sees no amounts and gets 403 on write; L4 can_see_amounts=false; BD sees the amount | Y |
| SP-049 | BR-SF-12 | S | P3 | as above | Agent lists deals sorted by amount | 200 and amounts stay masked | Y |

## 5.5 Daily new-contact limit (BR-OV-01)

A "new contact" is a step-1 campaign email, counted against the contact owner (or against the campaign creator for an
unowned contact) per UTC day. Sent history is **seeded in the DB**, because sending 500 real emails is not possible in this environment.

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| SP-130 | BR-OV-01 | S | P2 | dl1 fresh | `GET /sales-reports/daily-limit` | limit 500, used 0, remaining 500, not reached | Y |
| SP-131 | BR-OV-01 acceptance ("user is told") | I | P1 | dl1 has 500 first emails today; DRAFT campaign owned by BD dlm | dlm enrolls 2 contacts owned by dl1 | Enrolled, and the response warns that their first emails will wait (the quota used is dl1's) | Y |
| SP-132 | BR-OV-01 boundary N-1 | S | P1 | dl2: 499 first emails today, 20 follow-ups today, 7 first emails yesterday | status | used 499, remaining 1, not reached (follow-ups and yesterday's emails excluded, so the count resets daily) | Y |
| SP-133 | BR-OV-01 boundary N | S | P1 | +1 first email | status | used 500, remaining 0, reached, message mentions 500 and follow-ups | Y |
| SP-134 | BR-OV-01 per user | S | P2 | dl1/dl2 at the limit | status of dlm and ag1a | 0 used each | Y |
| SP-135 | BR-OV-01 attribution | S | P3 | 3 first emails to unowned contacts in dlm's campaign | status of dlm | used 3 | Y |
| SP-136 | BR-OV-01 acceptance ("queued … user is told") | S | P1 | 2 held first emails (DAILY_NEW_CONTACT_LIMIT) | status of dl2 | held_for_tomorrow 2; message says "2 first emails are queued for tomorrow" | Y |
| SP-137 | BR-OV-01 + enrollment | I | P1 | dlm at 499 | dlm enrolls 3 own contacts; mgr2 (0 used) enrolls 2 | Notice "Only 1 of today's 500 … other 2"; mgr2: no notice | Y |
| SP-138 | BR-OV-01 + imports | I | P2 | dl1 at 500 | Admin imports 3 contacts owned by dl1 (`POST /imports`) | Contacts are imported; used_today stays 500 | Y |
| SP-139 | BR-OV-01 acceptance (501st queued for next day) | I | P1 | dl1 at 500; ACTIVE campaign (24h window, no mailbox); due step-1 email | Wait ≤200 s for the scheduler in vector-worker-1 | Email stays QUEUED with last_error_code DAILY_NEW_CONTACT_LIMIT and scheduled_at = tomorrow 00:00 UTC | Y (worker) |
| SP-140 | BR-OV-01 acceptance (follow-ups keep sending) | I | P1 | as above; due step-2 email whose step 1 went out 3 days ago | Wait ≤120 s | Not held by the limit: the scheduler goes on to the send attempt, which fails because there is no mailbox, and that is acceptable here | Y (worker) |
| SP-143 | BR-OV-01 exact 501st in one batch | I | P2 | 499 sent; two due first emails | Scheduler | One sent, one held | N: needs a working mailbox and real sending; in this sandbox the failed send releases the reservation, so the result can't be observed |

## 5.6 Leads & SQL management

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| SP-060 | BR-LD-01 | S | P1 | ag1a contact with company | `POST /leads` with source | stage NEW, owner = contact owner, contact, company and account linked | Y |
| SP-061 | BR-LD-01 | S | P2 | Open lead exists | Duplicate lead; unknown contact; bad stage | 409 (returns the existing id), 404, 400 and nothing saved | Y |
| SP-062 | BR-LD-03 | S | P1 | — | `POST /leads` with stage=SQL and no BANT | Not created as SQL | Y |
| SP-063 | BR-LD-02 | I | P1 | Lead NEW | NEW→CONTACTED→ENGAGED→CONTACTED, reading the contact lifecycle each time | LEAD, LEAD, MQL, MQL (the lifecycle never goes back) | Y |
| SP-064 | BR-LD-02, BR-SF-02 | S | P2 | Lead ENGAGED | Set CONVERTED directly; unknown stage; DISQUALIFIED without a reason | 400 each; stage unchanged | Y |
| SP-065 | BR-LD-03 | I | P1 | Lead ENGAGED | Tick 2 of 4 BANT criteria → SQL; tick all → SQL | 400 naming the missing criteria; then SQL, qualified_at set, contact lifecycle SQL | Y |
| SP-066 | BR-LD-03, BR-LD-04 | S | P1 | SQLs in both teams | `GET /leads/sql-queue` as mgr1 | Only the team's SQLs; owner, age, next step, overdue flag; by-owner summary | Y |
| SP-067 | BR-LD-04 | S | P2 | One SQL aged 20 days (DB) | Queue filtered by owner | Oldest first, sql_age_days 20, age_days 25, counted in over_14_days | Y |
| SP-068 | BR-LD-05 | S | P1 | Leads in several stages | `GET /leads/board` (with and without include_closed) | Columns NEW/CONTACTED/ENGAGED/SQL (+CONVERTED/DISQUALIFIED); each lead in exactly its stage column; counts match | Y |
| SP-069 | BR-LD acceptance ("exactly one stage and one owner") | I | P1 | After all lead tests | DB check across the tenant | No lead without an owner or a stage | Y |
| SP-070 | BR-LD-06 acceptance ("no re-typing") | S | P1 | SQL lead | `POST /leads/{id}/convert` with amount and close date only | Deal carries the contact, company/account and owner; default name from the company; first open stage; client type NEW | Y |
| SP-071 | BR-LD-06, BR-LD-02 | I | P1 | as above | Read the lead, contact and history | Lead CONVERTED and linked; contact → OPPORTUNITY; history on the lead and the deal | Y |
| SP-072 | BR-LD-06 | S | P1 | Converted lead | Convert again; convert a NEW lead; change stage; delete | 400 each; exactly one deal | Y |
| SP-073 | BR-LD-06, BR-SH-02 | S | P2 | SQL lead of ag1b | BD converts with an owner outside the team; then with name/owner/stage/client-type overrides | 403; then 201 with the overrides applied | Y |
| SP-074 | BR-LD-06 | I | P3 | Contact with a company name but no account | Convert | A company record is resolved and linked | Y |
| SP-075 | BR-SF-02 | S | P3 | — | Disqualify with a reason and recycle_in_days=30 | Stored | Y |
| SP-076 | BR-SF-01 | I | P3 | Inbound reply seeded in the DB | `POST /leads/from-message` as owner / peer / again | 201 ENGAGED "Campaign reply", need ticked, campaign linked; peer 404; repeat 409 | Y |
| SP-077 | BR-SF-03 | I | P3 | Round robin [ag1a, ag1b] | Admin creates 3 leads for unowned contacts | Owners ag1a, ag1b, ag1a; the contact gets the same owner | Y |
| SP-141 | BR-SF-02 | I | P3 | Disqualified lead, recycle_at moved into the past | Wait ≤170 s for the worker | Lead back to NEW, owner notified (LEAD_RECYCLED), history logged | Y (worker) |

## 5.7 Sales pipeline management

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| SP-090 | BR-SP-01 | S | P1 | — | `GET /sales/stages` | 6 default stages with probabilities 10/25/50/75/100/0 and status OPEN/WON/LOST | Y |
| SP-091 | BR-SP-01 | S | P2 | — | Admin adds a stage (40%), then re-weights it to 45%; probability 150 or -1; won+lost; duplicate; blank name; agent and Head try to manage stages | 201/200; 400,400,400,409,400; 403 | Y |
| SP-092 | BR-SP-01, BR-SP-03 | S | P1 | — | Create a deal for "20,000"; move it to Proposal | Probability 10 → weighted 2,000; then 50 → weighted 10,000 | Y |
| SP-093 | BR-SF-04, §6 audit | I | P2 | Deal | Change stage, amount and close date | History records each change with old/new values, user and time; stage_history lists each stage | Y |
| SP-094 | BR-SP-01 | S | P2 | Deal | Negative or text amount, bad date, bad client type, unknown stage, blank name | 400/422; amount unchanged | Y |
| SP-095 | BR-SF-05 (close dates) | S | P3 | Deal with a past close date | Create; list with stale=true | overdue and stale flags set; the deal is in the stale filter | Y |
| SP-096 | BR-SF-05, BR-SP-01 | I | P1 | Open deal | Move to Closed won without a reason, then with one | 400 (still OPEN); then WON, closed_at set, forecast CLOSED, contact CUSTOMER | Y |
| SP-097 | BR-SF-05 | S | P1 | Open deal | Move to Closed lost without a reason, then with one | 400; then LOST, OMITTED, out of the open list | Y |
| SP-098 | BR-SP-04 | S | P1 | Company with a won deal | New deal for that company and for a new company | EXISTING and NEW by default | Y |
| SP-099 | BR-SP-02 | S | P1 | Deal | Proposal DRAFT→SENT→UNDER_REVIEW→ACCEPTED; another →REJECTED; invalid status; proposal without a deal | Statuses and dates set; amount defaults to the deal's; 400s; history entry | Y |
| SP-100 | BR-SP-02, BR-SP-04 acceptance | S | P1 | Proposals on NEW and EXISTING deals | `GET /proposals` (all / EXISTING / NEW) | One column per status with counts; the filter splits by client type | Y |
| SP-101 | BR-SF-15 | S | P3 | Proposal | Download the PDF as owner and as peer | %PDF for the owner, 404 for the peer | Y |
| SP-102..106 | BR-SP-03 acceptance ("totals match the sum of open opportunities for the filter"), BR-SP-04 | I | P1 | Deals in several stages and client types | `/pipeline/revenue` vs `/opportunities?status=OPEN` for: all, NEW, EXISTING, owner, close-date window | open_amount, weighted, count and per-stage sum equal the summed open deals | Y |
| SP-107 | BR-SP-03 | S | P2 | Won and lost deals | Revenue totals for the owner | won/lost counts and amounts correct; NEW+EXISTING = open | Y |
| SP-108 | BR-SP-01 | S | P2 | — | `GET /opportunities/board` | Open columns with count/amount/weighted (= amount × probability); closed columns on request | Y |
| SP-109 | BR-SP-01 | S | P3 | Custom stage | Retire it (active=false) | Hidden from the active list, kept with include_inactive | Y |
| SP-110 | BRD v1 roles | S | P3 | Deal | Exec deletes; BD deletes | 403; 200 | Y |
| SP-142 | BR-SF-05 (stale alerts) | I | P3 | Overdue deal | Wait ≤170 s for the worker | STALE_DEAL notification for the owner | Y (worker) |

## System journeys

| ID | BRD ref | Type | Pri | Steps | Expected | Auto |
|---|---|---|---|---|---|---|
| SP-120 | 5.6 + 5.7 end to end | S | P1 | ag1b: contact → lead → CONTACTED → ENGAGED → BANT → SQL; BD sees it in the SQL queue; convert (50k) → Needs analysis → Proposal; proposal SENT → ACCEPTED; Closed won with reason | Every step 2xx; contact CUSTOMER; Head can open the deal; BD's won total includes it | Y |
| SP-121 | BR-SF-01 + 5.6 + 5.7 | S | P2 | Reply → lead (SP-076) → BANT → SQL → convert → Closed lost (Price) | LOST with reason, linked to the lead and to the reply's campaign | Y |

## UI checks (Playwright, Chromium, logged in as tenant A users)

| ID | BRD ref | Type | Pri | Steps | Expected | Auto |
|---|---|---|---|---|---|---|
| SP-151 | BR-SH-02, BR-LD-01 | S | P1 | mgr1 opens /app/leads | Team leads shown; other team's not | Y |
| SP-152 | BR-LD-03 | S | P1 | mgr1 opens /app/sql-queue | Team SQL shown; other team's not | Y |
| SP-153 | BR-SH-02, 5.7 | S | P1 | mgr1 opens /app/deals | Team deals only | Y |
| SP-154 | BR-LD-05/BR-SP-03 | S | P2 | mgr1 opens /app/pipeline | Renders without JS errors; no other-team deals | Y |
| SP-155 | BR-SH-02 | S | P1 | ag1a opens /app/leads | Own lead; no peer/other team | Y |
| SP-156 | BR-SH-02 | S | P1 | ag1a opens /app/deals | Own deal only | Y |
| SP-157 | BR-SH-02 acceptance (URL) | S | P1 | ag1a opens /app/leads/{peer lead id} | No peer data rendered | Y |
| SP-158 | BR-SH-02 acceptance (URL) | S | P1 | ag1a opens /app/deals/{peer deal id} | No peer data rendered | Y |

## Not automated / out of reach

| ID | BRD ref | Reason |
|---|---|---|
| SP-143 | BR-OV-01 | Requires a working mailbox and real SES delivery (see above) |
| SP-160 | Open question §12 ("what can each level see and edit") | The visibility rules are still to be confirmed by the SO; tests assert the BRD's §5.4 rule (own + everyone below) |
| SP-161 | §6 Usability (convert SQL in ≤3 clicks) | Manual UX review |
| SP-162 | §6 Performance (lists < 1 s at 100k) | Owned by the performance suite |
