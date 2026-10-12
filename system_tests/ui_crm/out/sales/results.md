# ui_crm sales results

PASS 20 · FAIL 7 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-SAL-01 | Leads split view lists open leads; stage chips filter; counts equal DB | PASS |  |  |
| UIC-SAL-02 | Leads list mode + board mode render with per-stage counts | PASS |  |  |
| UIC-SAL-03 | Leads: search and owner filter | PASS |  |  |
| UIC-SAL-04 | Create lead via modal (contact search) -> DB; duplicate open lead shows link to existing | PASS |  |  |
| UIC-SAL-05 | LeadDetail: stage steps, BANT qualification, next step persisted | PASS |  |  |
| UIC-SAL-06 | Disqualify requires a reason; stores reason + recycle; recycle as new | PASS |  |  |
| UIC-SAL-07 | SQL queue: tiles equal DB; convert to opportunity creates deal and redirects | FAIL | unclassified | locator.innerText: Timeout 15000ms exceeded. |
| UIC-SAL-08 | Deals board: stage column counts and amounts equal DB; list view total | PASS |  |  |
| UIC-SAL-09 | Deals: create via modal (validation, company, amount, close date) -> DB | PASS |  |  |
| UIC-SAL-10 | Deals board: drag to next stage persists; drag to Closed won asks for reason (required) | FAIL | unclassified | locator.check: Error: strict mode violation: locator('div.fixed.inset-0').last().getByLabel('Price') resolved to 2 elements: |
| UIC-SAL-11 | DealDetail: stage buttons, inline edits (amount/close/next step), forecast category | PASS |  |  |
| UIC-SAL-12 | DealDetail: log note/call, task, timeline; mark lost requires reason | FAIL | unclassified | locator.check: Error: strict mode violation: locator('div.fixed.inset-0').last().getByLabel('Timing') resolved to 2 elements: |
| UIC-SAL-13 | Quote: add product + custom line with discount -> totals, proposal amount, PDF download | FAIL | unclassified | locator.click: Error: strict mode violation: getByTitle('Quote lines and PDF') resolved to 2 elements: |
| UIC-SAL-14 | Pipeline revenue tab: open/weighted/won tiles equal independently computed DB sums; proposals tab renders | PASS |  |  |
| UIC-SAL-15 | Sales dashboard: tiles match DB (won amount/count, open pipeline); period selector changes numbers | PASS |  |  |
| UIC-SAL-16 | Sales reports: every tab renders without errors | PASS |  |  |
| UIC-SAL-17 | Forecast: FY April-March quarters; won per quarter equals DB | FAIL | unclassified | Q1 Apr-Jun (FY start 2026-04-01 in header) |
| UIC-SAL-18 | Custom report builder: build, run, save, reopen, delete | PASS |  |  |
| UIC-SAL-19 | Targets: edit quarterly cell persists, totals row, attainment on reports | FAIL | unclassified | attainment % shown |
| UIC-SAL-20 | Sales team: change level/manager persists and changes visibility | PASS |  |  |
| UIC-SAL-21 | Sales settings: save persists after reload; stale days validation | PASS |  |  |
| UIC-SAL-22 | Sales settings: add reason, product and workflow rule | FAIL | unclassified | not equal: got 0 expected 1 |
| UIC-SAL-23 | Calendar sync: Google/Microsoft show a clear "not set up" message and Connect is disabled | PASS |  |  |
| UIC-SAL-24 | Amount hiding: AGENT hidden role sees no money in deals, pipeline, reports, quotes; API masks amounts | PASS |  |  |
| UIC-SAL-25 | AGENT visibility: sees only own leads/deals; peer's record by URL is not found | PASS |  |  |
| UIC-SAL-26 | Stale-deal notification job creates a bell notification for the owner | PASS |  |  |
| UIC-SAL-27 | Deals API failure keeps the page shell | PASS |  |  |
