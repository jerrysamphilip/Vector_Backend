# Sales hierarchy, daily limit, leads & pipeline (BRD 5.4-5.7) results

PASS 90 · FAIL 6 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| SP-001 | Admin configures the 4-level hierarchy (L1..L4) via the API | PASS |  |  |
| SP-002 | Hierarchy validation rejects invalid configurations (400) | PASS |  |  |
| SP-003 | Only admins may change the hierarchy; other tenants cannot (403/403/404) | PASS |  |  |
| SP-004 | Team directory: exec sees self, BD sees own team, Sales Head sees all | PASS |  |  |
| SP-010 | Fixture: users across the hierarchy own contacts, leads and deals | PASS |  |  |
| SP-021 | ag1a contact visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-022 | ag1a lead visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-023 | ag1a opp visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-024 | mgr1 contact visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-025 | mgr1 lead visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-026 | mgr1 opp visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-027 | mgr2 contact visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-028 | mgr2 lead visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-029 | mgr2 opp visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-030 | head contact visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-031 | head lead visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-032 | head opp visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-033 | mr1 contact visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-034 | mr1 lead visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-035 | mr1 opp visibility: open-by-URL and list match the hierarchy | PASS |  |  |
| SP-036 | Exec cannot find a peer's contact/lead/deal by search (BR-SH-02 acceptance) | PASS |  |  |
| SP-037 | Exec cannot read/edit/convert/delete a peer's records by URL (404) | PASS |  |  |
| SP-038 | Exec cannot create a deal on a peer's contact (400/404) | PASS |  |  |
| SP-039 | Proposal pipeline only lists proposals on visible deals | PASS |  |  |
| SP-040 | Exec can't assign to a peer, BD can't assign outside own team, owner can't be blanked | PASS |  |  |
| SP-041 | BD reassigns a lead within the team; new owner sees it, old owner no longer does; owner change logged | PASS |  |  |
| SP-042 | L4 contact is invisible to the exec until the BD hands it over; then the exec can work it | PASS |  |  |
| SP-043 | Revenue pipeline is scoped: Head = all teams, BD = own team, exec = own deals | PASS |  |  |
| SP-044 | BD's contact export contains own team and not other teams (NFR: every screen, API and export) | PASS |  |  |
| SP-045 | Another tenant's admin cannot read, edit, convert or list tenant A's sales records | PASS |  |  |
| SP-046 | Assigning to another tenant's user or stage is rejected (400) | PASS |  |  |
| SP-047 | Agent outside the hierarchy sees only own leads; admin sees all; BD cannot see it | PASS |  |  |
| SP-048 | With amounts hidden for AGENT/L4: agent sees no amounts anywhere, cannot write one; manager still sees | PASS |  |  |
| SP-049 | Hidden amounts: list still works when sorted by amount and stays masked | PASS |  |  |
| SP-060 | Create lead: source, stage NEW, owner = contact owner, linked contact & company shown | PASS |  |  |
| SP-061 | Duplicate open lead → 409 (with existing id); unknown contact → 404; bad stage → 400 and nothing saved | PASS |  |  |
| SP-062 | POST /leads with stage=SQL and no BANT does not create an SQL | PASS |  |  |
| SP-063 | Lead stage ↔ lifecycle: NEW/CONTACTED=LEAD, ENGAGED=MQL, moving back never downgrades the contact | PASS |  |  |
| SP-064 | Set CONVERTED directly / unknown stage / disqualify without reason → 400; stage unchanged | PASS |  |  |
| SP-065 | SQL needs all four BANT criteria; missing ones are named; once met: SQL, qualified_at set, contact → SQL | PASS |  |  |
| SP-066 | SQL queue (BD view): only SQL leads of the team, with owner, age, next step & overdue flag, by-owner summary | FAIL | defect | in=True other_team=False only_sql=True team=True mine={'owner_name': 'Agoneaabf133 Test', 'sql_age_days': -1, 'next_step': 'Send pricing', 'next_step_overdue': True} |
| SP-067 | SQL queue tracks age (oldest first, >14-day count) and filters by owner | PASS |  |  |
| SP-068 | Lead board: one column per open stage, each open lead in exactly its stage column; closed stages on request | PASS |  |  |
| SP-069 | Invariant: no lead in the workspace without a stage or an owner (DB check) | PASS |  |  |
| SP-070 | Convert: deal linked to the lead's contact, company and owner, default name from company, first open stage, NEW client | PASS |  |  |
| SP-071 | Convert (integration): lead → CONVERTED with opportunity link, contact lifecycle → OPPORTUNITY, history on lead and deal | PASS |  |  |
| SP-072 | Converting twice / a non-SQL lead → 400; converted lead can't change stage or be deleted; one deal only | PASS |  |  |
| SP-073 | BD converts with name/owner/stage/client-type overrides; owner outside team → 403 | PASS |  |  |
| SP-074 | Converting a lead whose contact has only a company name links the deal to a company record | PASS |  |  |
| SP-075 | Disqualify needs a reason (BR-SF-02); reason and recycle date (30 days) stored | PASS |  |  |
| SP-076 | Reply → lead in one click: ENGAGED, source 'Campaign reply', need ticked, campaign linked; peer 404; repeat 409 | PASS |  |  |
| SP-077 | Unowned leads are assigned round robin (BR-SF-03) and the contact follows its lead's owner | PASS |  |  |
| SP-090 | Sales stages are classified (open/won/lost) with a probability each, in order | PASS |  |  |
| SP-091 | Admin adds/re-weights a stage; prob outside 0-100, won+lost, duplicate, blank → 4xx; non-admins 403 | PASS |  |  |
| SP-092 | New deal: first stage 10% → weighted 2,000; move to Proposal: 50% → weighted 10,000; '20,000' parsed | PASS |  |  |
| SP-093 | History has stage (by name), amount and close-date changes with user and time; stage_history lists stays | PASS |  |  |
| SP-094 | Negative/non-numeric amount, bad date, bad client type, unknown stage, blank name → 4xx; amount unchanged | PASS |  |  |
| SP-095 | Close date in the past → overdue + stale flags and listed in the stale filter | PASS |  |  |
| SP-096 | Closed won without a reason → 400 (still OPEN); with reason → WON, closed_at, forecast CLOSED, contact CUSTOMER | PASS |  |  |
| SP-097 | Closed lost without a reason → 400; with reason → LOST, forecast OMITTED, not in open list | PASS |  |  |
| SP-098 | Deal for a company that already won defaults to EXISTING; brand-new company defaults to NEW | PASS |  |  |
| SP-099 | Proposal statuses draft/sent/under review/accepted/rejected with sent & decided dates; amount defaults to deal; bad status 400 | PASS |  |  |
| SP-100 | Proposal pipeline has a column per status with counts/amounts and filters by new vs existing client | PASS |  |  |
| SP-101 | Proposal PDF downloads for the owner (BR-SF-15) and is hidden from a peer | PASS |  |  |
| SP-102 | Revenue pipeline totals (unweighted & weighted) = sum of open deals, all deals (admin) | PASS |  |  |
| SP-103 | Revenue pipeline totals = open deals, filtered to client type NEW | PASS |  |  |
| SP-104 | Revenue pipeline totals = open deals, filtered to client type EXISTING | PASS |  |  |
| SP-105 | Revenue pipeline totals = open deals, filtered by owner (BD view of one exec) | PASS |  |  |
| SP-106 | Revenue pipeline totals = open deals, filtered by close-date window | PASS |  |  |
| SP-107 | Pipeline reports won/lost counts and amounts and splits open amount by NEW/EXISTING | PASS |  |  |
| SP-108 | Deal board: open stage columns with count/amount/weighted; closed columns on request | PASS |  |  |
| SP-109 | Admin retires a stage: hidden from the active list, still listed with include_inactive | PASS |  |  |
| SP-110 | Exec cannot delete a deal (403); their BD manager can (200) | PASS |  |  |
| SP-120 | Exec journey NEW→CONTACTED→ENGAGED→BANT→SQL→(in BD queue)→convert→stages→proposal accepted→won; contact CUSTOMER; Head sees it; BD won total includes it | PASS |  |  |
| SP-121 | Reply-to-loss journey: deal keeps the reply's campaign (ROI link), lead link and LOST with reason | PASS |  |  |
| SP-130 | User with no first emails today: limit 500, used 0, remaining 500, not reached | PASS |  |  |
| SP-131 | BD manager enrolls an exec's contacts while the exec is at 500: user is told the first emails will wait | FAIL | defect | HTTP 200 enrolled=2 rejected={} notice=None |
| SP-132 | At 499 new contacts (plus 20 follow-ups today and 7 yesterday): used 499, remaining 1, not reached | PASS |  |  |
| SP-133 | At exactly 500: reached, remaining 0, and the user is told (message mentions 500 and follow-ups) | PASS |  |  |
| SP-134 | Another user's 500 first emails don't consume this user's quota | PASS |  |  |
| SP-135 | First emails to unowned contacts count for the campaign creator | PASS |  |  |
| SP-136 | Held-for-tomorrow count and message tell the user how many first emails were queued for the next day | PASS |  |  |
| SP-137 | Enrolling past the remaining quota returns a notice (1 left, 2 wait); within quota → no notice | PASS |  |  |
| SP-138 | Importing 3 contacts for a user at the limit leaves used_today unchanged (only first sends count) | PASS |  |  |
| SP-139 | Worker: 501st new contact's first email is held (QUEUED, DAILY_NEW_CONTACT_LIMIT) and rescheduled to tomorrow | FAIL | defect | row=QUEUED\|-\|2026-10-10\|- |
| SP-140 | Worker: a follow-up for the same user at the limit is not held by the daily limit (it proceeds to send) | FAIL | test-issue | follow-up never processed within 120 s: QUEUED\|-\|2026-10-10\|- |
| SP-141 | Worker reopens the disqualified lead (→ NEW), notifies the owner and logs the change | FAIL | defect | back=False kinds=['ASSIGNED', 'ASSIGNED'] |
| SP-142 | Overdue deal raises a STALE_DEAL notification for its owner within a worker cycle | FAIL | defect | no STALE_DEAL notification within 170 s |
| SP-151 | UI (BD manager): Leads screen shows own team leads and no other team | PASS |  | missing=[] leaked=[] |
| SP-152 | UI (BD manager): SQL queue shows own team SQLs only | PASS |  | missing=[] leaked=[] |
| SP-153 | UI (BD manager): Deals screen shows team deals only | PASS |  | missing=[] leaked=[] |
| SP-154 | UI (BD manager): Pipeline screen renders without errors and without other-team deals | PASS |  | errors= |
| SP-155 | UI (exec): Leads screen shows own lead and not a peer's | PASS |  | missing=[] leaked=[] |
| SP-156 | UI (exec): Deals screen shows own deal and not a peer's | PASS |  | missing=[] leaked=[] |
| SP-157 | UI (exec): opening a peer's lead by URL shows no peer data | PASS |  | leaked=false |
| SP-158 | UI (exec): opening a peer's deal by URL shows no peer data | PASS |  | leaked=false |
