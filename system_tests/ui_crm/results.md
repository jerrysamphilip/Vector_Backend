# ui_crm (CRM + Sales functional UI) results

PASS 150 · FAIL 3 · BLOCKED 4

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-CON-01 | Contacts list renders 25/60 with pager text and header | PASS |  |  |
| UIC-CON-02 | Pagination next/prev/last page counts and disabled state | PASS |  |  |
| UIC-CON-03 | Sort by name asc/desc matches API order | PASS |  |  |
| UIC-CON-04 | Sort by Email and Company headers | PASS |  |  |
| UIC-CON-05 | Search box filters by name/email/company; empty-state; clear | PASS |  |  |
| UIC-CON-06 | Column chooser hides/shows columns, persists, Reset restores | PASS |  |  |
| UIC-CON-07 | Filter builder: enum (lifecycle stage is Lead) equals DB count | PASS |  |  |
| UIC-CON-08 | Filter builder: AND of two conditions (stage + country text) | PASS |  |  |
| UIC-CON-09 | Filter builder: toggle AND->OR changes result to union | PASS |  |  |
| UIC-CON-10 | Filter builder: tag has tag "hot"; "has no tags" | PASS |  |  |
| UIC-CON-11 | Filter builder: text contains / starts with / is empty | PASS |  |  |
| UIC-CON-12 | Filter builder: date "in the last N days" and owner "Me" | PASS |  |  |
| UIC-CON-13 | Filter builder: nested group  stage=Lead AND (country=US OR tag=hot) | PASS |  |  |
| UIC-CON-14 | Filter builder: remove condition / Clear all resets to 60 | PASS |  |  |
| UIC-CON-15 | Built-in views from sidebar: My contacts / Unassigned / Recently created | PASS |  |  |
| UIC-CON-16 | Save filtered view -> appears in sidebar & DB; reopening applies filter | PASS |  |  |
| UIC-CON-17 | Rename saved view (Update this view) and persisted | PASS |  |  |
| UIC-CON-18 | Save-view modal: empty name disables Save; Cancel closes | PASS |  |  |
| UIC-CON-19 | Delete saved view removes it from sidebar and DB | PASS |  |  |
| UIC-CON-20 | Board view: lane totals equal DB; drag card to another stage persists | PASS |  |  |
| UIC-CON-21 | Board: admin drags a card backwards (allowed for admins, BR-CM-08) and it persists | PASS |  |  |
| UIC-CON-22 | Board: group by lead status and switch back to table | PASS |  |  |
| UIC-CON-23 | Add contact: empty email -> validation message, nothing created | PASS |  |  |
| UIC-CON-24 | Add contact: invalid email format -> clear error, nothing created | PASS |  |  |
| UIC-CON-25 | Add contact: full form creates contact, company account linked, tags, redirect to detail | PASS |  |  |
| UIC-CON-26 | Add contact: duplicate email -> error + "Open the existing contact" link works | PASS |  |  |
| UIC-CON-27 | Add contact: email of a deleted contact -> restore hint (Recently deleted) | PASS |  |  |
| UIC-CON-28 | Add contact modal: Cancel / X close paths, no row created | PASS |  |  |
| UIC-CON-29 | Properties modal: create NUMBER and SELECT custom fields; appear in DB and Add form | PASS |  |  |
| UIC-CON-30 | Properties modal: blank label disables Add; duplicate label error | PASS |  |  |
| UIC-CON-31 | Create contact with custom property values; shown in table column + detail; filterable | PASS |  |  |
| UIC-CON-32 | Properties modal: delete a custom field | PASS |  |  |
| UIC-CON-33 | Bulk: select-all-on-page shows "25 selected", Clear selection works | PASS |  |  |
| UIC-CON-34 | Bulk: Edit property (lead status) updates DB for selected only | PASS |  |  |
| UIC-CON-35 | Bulk: Add to new list -> notice and list membership in DB | PASS |  |  |
| UIC-CON-36 | Bulk: Assign owner persists | PASS |  |  |
| UIC-CON-37 | Bulk: Add tag / Remove tag | PASS |  |  |
| UIC-CON-38 | Bulk: Enroll in campaign shows enrolled count + rejection reasons (unsubscribed, invalid) | PASS |  |  |
| UIC-CON-39 | Bulk: Delete (confirm) soft-deletes and notice; appears in Recently deleted | PASS |  |  |
| UIC-CON-40 | Export CSV downloads file whose rows equal the visible filtered set | PASS |  |  |
| UIC-CON-41 | Export XLSX downloads a valid workbook with the same row count | PASS |  |  |
| UIC-CON-42 | Duplicates modal: no duplicates state, then merge with per-property choices | PASS |  |  |
| UIC-CON-43 | AGENT (no manage_prospects): sees only own contacts; no Import/Export/Properties/Duplicates/checkboxes | PASS |  |  |
| UIC-CON-44 | AGENT: direct API refused for bulk/export/merge; own-contact create works without owner field | PASS |  |  |
| UIC-CON-45 | AGENT: cannot open someone else's contact by URL ("Contact not found") | PASS |  |  |
| UIC-CON-46 | List API 500 -> page does not crash (graceful state) | PASS |  |  |
| UIC-CD-01 | Record page renders header, stats, 3 columns of cards | FAIL | unclassified | card Company |
| UIC-CD-02 | Change lifecycle stage inline -> persisted, appears as property change with old/new | PASS |  |  |
| UIC-CD-03 | Change lead status inline persisted | PASS |  |  |
| UIC-CD-04 | Edit details: save changes (name, phone, title, company, location) -> DB; Cancel discards | PASS |  |  |
| UIC-CD-05 | Edit details: invalid email shows an error and saves nothing | PASS |  |  |
| UIC-CD-06 | Tags: add and remove in header persists | PASS |  |  |
| UIC-CD-07 | Owner change inline persists and is recorded in history | PASS |  |  |
| UIC-CD-08 | Unsubscribe then resubscribe (with confirm) updates consent + global unsubscribe list | PASS |  |  |
| UIC-CD-09 | Log a note -> timeline entry + DB activity | PASS |  |  |
| UIC-CD-10 | Log a call (subject, outcome, duration) -> timeline + Calls stat | PASS |  |  |
| UIC-CD-11 | Log an email and a meeting | PASS |  |  |
| UIC-CD-12 | Edit a logged activity then delete it (confirm) | FAIL | unclassified | deleted: got 1 expected 0 |
| UIC-CD-13 | Create task from record (overdue due date) -> card + Open tasks stat + DB; complete it | PASS |  |  |
| UIC-CD-14 | Timeline filters: Notes / Calls / Changes / All and user filter | PASS |  |  |
| UIC-CD-15 | Create lead from the record -> redirected to lead, lead in DB, Sales card lists it | PASS |  |  |
| UIC-CD-16 | Create a deal directly from the contact record | BLOCKED | not-implemented | Record page only offers "+ Create lead"; deals are created from a lead conversion or the Deals page |
| UIC-CD-17 | @mention in notes | BLOCKED | not-implemented | Composer has no @mention support |
| UIC-CD-18 | Associations: company link opens the company page; list + campaign cards link out | PASS |  |  |
| UIC-CD-19 | Unknown contact id shows "Contact not found" with back link | PASS |  |  |
| UIC-CD-20 | Property save API failure shows the error and keeps the old value | PASS |  |  |
| UIC-CD-21 | AGENT on own contact: can edit/log note, owner shown read-only, no Delete | PASS |  |  |
| UIC-CD-22 | Non-admin manager cannot move lifecycle stage backwards (forward only message) | PASS |  |  |
| UIC-CD-23 | Delete contact (confirm) -> back to list; listed in Recently deleted; restore brings it back | PASS |  |  |
| UIC-ACC-01 | Companies list renders with count, columns and revenue formatting | PASS |  |  |
| UIC-ACC-02 | Sort by Company asc/desc and by Contacts | PASS |  |  |
| UIC-ACC-03 | Search by name / domain / industry; empty state; owner segments | PASS |  |  |
| UIC-ACC-04 | New company: name required (button disabled), full create -> detail page + DB | PASS |  |  |
| UIC-ACC-05 | Domain must be unique per workspace: duplicate shows message, nothing created | PASS |  |  |
| UIC-ACC-06 | Company detail: stats, contacts listing, "Open in Contacts" filter chip | PASS |  |  |
| UIC-ACC-07 | Edit company (cancel path then rename + fields) -> DB, contacts get new company name, history row | PASS |  |  |
| UIC-ACC-08 | Owner change on company detail persists | PASS |  |  |
| UIC-ACC-09 | Add contact from company page: company prefilled, linked, contact count increments | PASS |  |  |
| UIC-ACC-10 | Link contacts by company (backfill) shows a notice and links contacts without a company record | PASS |  |  |
| UIC-ACC-11 | Pagination across 25 per page | PASS |  |  |
| UIC-ACC-12 | AccountDetail lists the company's deals | BLOCKED | not-implemented | AccountDetail shows contacts and history only; deals appear on the Deals screen |
| UIC-ACC-13 | Delete company (confirm): contacts kept, appears in Recently deleted > Companies, restore works | PASS |  |  |
| UIC-ACC-14 | AGENT: sees only own companies; no backfill button, owner read-only, no delete | PASS |  |  |
| UIC-ACC-15 | AGENT cannot open another user's company (not found) and API refuses | PASS |  |  |
| UIC-ACC-16 | Companies API failure: page keeps its shell | PASS |  |  |
| UIC-LST-01 | Lists page empty state, then headings/filters render | PASS |  |  |
| UIC-LST-02 | New list: name required; create static list -> DB + opens Contacts filtered by it | PASS |  |  |
| UIC-LST-03 | New list modal: cancel closes without creating | PASS |  |  |
| UIC-LST-04 | Active list: needs a criterion; live preview count equals DB; saved list has the same members | PASS |  |  |
| UIC-LST-05 | Active list membership follows data: changing a contact moves it in/out automatically | PASS |  |  |
| UIC-LST-06 | Static list: add members from Contacts bulk bar, remove from this list, counts match DB | PASS |  |  |
| UIC-LST-07 | Add contact modal can add the new contact to a static list | PASS |  |  |
| UIC-LST-08 | Edit list: rename + description persisted; active list criteria edit changes membership | FAIL | unclassified | locator.selectOption: Error: Element is not a <select> element |
| UIC-LST-09 | Type tabs and search filter the lists | PASS |  |  |
| UIC-LST-10 | AGENT: can see lists but cannot create/edit/delete (buttons hidden, API refuses) | PASS |  |  |
| UIC-LST-11 | Delete list (confirm) removes it, contacts are kept | PASS |  |  |
| UIC-LST-12 | Lists API failure keeps page shell | PASS |  |  |
| UIC-IMP-01 | Import page renders drop zone + options hidden until a file is chosen | PASS |  |  |
| UIC-IMP-02 | CSV preview: row/column count, suggested mapping, sample values | PASS |  |  |
| UIC-IMP-03 | Run import with defaults: result counts, DB rows, owner = me, company linked | PASS |  |  |
| UIC-IMP-04 | Re-import same file: update_existing OFF leaves records untouched; ON updates (no duplicates) | PASS |  |  |
| UIC-IMP-04B | With update_existing OFF the result card must report 0 contacts updated | PASS |  |  |
| UIC-IMP-05 | Blank cells never overwrite existing values | PASS |  |  |
| UIC-IMP-06 | Mapping: create a new custom property from a column (type Number) -> property + values stored | PASS |  |  |
| UIC-IMP-07 | Options: add to a new list, set owner to Alice | PASS |  |  |
| UIC-IMP-08 | Options: existing list requires choosing one; imports into it | PASS |  |  |
| UIC-IMP-09 | Bad rows (invalid email, missing email, duplicate in file): counts, reasons list, error file download | PASS |  |  |
| UIC-IMP-10 | Past imports table lists runs with error counts and downloadable error file | PASS |  |  |
| UIC-IMP-11 | Bad files: .txt / binary / empty / header-only show a clear message and no crash | PASS |  |  |
| UIC-IMP-12 | One-column CSV (email only) imports | PASS |  |  |
| UIC-IMP-13 | File without an Email/Company column: warning shown and Import disabled | PASS |  |  |
| UIC-IMP-14 | Two columns mapped to the same property show the "last one wins" warning | PASS |  |  |
| UIC-IMP-15 | Company-only import (no email column) creates companies | PASS |  |  |
| UIC-IMP-16 | Excel (.xlsx) file imports | PASS |  |  |
| UIC-IMP-17 | Cancel resets the wizard | PASS |  |  |
| UIC-IMP-18 | "Import another file" after a run starts over | PASS |  |  |
| UIC-IMP-19 | 1,000-row CSV imports in one run (DB count + time) | PASS |  |  |
| UIC-IMP-20 | AGENT without manage_prospects cannot use import (route redirected, API 403) | PASS |  |  |
| UIC-IMP-21 | Preview API failure shows an error message | PASS |  |  |
| UIC-TSK-01 | Tasks page: counters and list equal DB | PASS |  |  |
| UIC-TSK-02 | Date filters: overdue / next 7 days / no due date; status Done/All | PASS |  |  |
| UIC-TSK-03 | New task: title required; create with type/priority/due/assignee -> DB and visible to assignee | PASS |  |  |
| UIC-TSK-04 | Complete, reopen and delete (confirm) a task | PASS |  |  |
| UIC-TSK-05 | Task linked to a contact shows a link to it | PASS |  |  |
| UIC-TSK-06 | AGENT: no "Everyone" scope, only own tasks; cannot modify others' task via API | PASS |  |  |
| UIC-TSK-07 | Empty state and API failure | PASS |  |  |
| UIC-RD-01 | Recently deleted lists deleted contacts (30 minus purged-window one), paginated, days remaining | PASS |  |  |
| UIC-RD-02 | Search, select-all restore and single restore update DB and the Contacts list | PASS |  |  |
| UIC-RD-03 | Manual purge of a deleted record | BLOCKED | not-implemented | No purge/permanent-delete control on Recently deleted (items expire after 90 days) |
| UIC-RD-04 | AGENT without manage_prospects: page not reachable, API refused | PASS |  |  |
| UIC-SAL-01 | Leads split view lists open leads; stage chips filter; counts equal DB | PASS |  |  |
| UIC-SAL-02 | Leads list mode + board mode render with per-stage counts | PASS |  |  |
| UIC-SAL-03 | Leads: search and owner filter | PASS |  |  |
| UIC-SAL-04 | Create lead via modal (contact search) -> DB; duplicate open lead shows link to existing | PASS |  |  |
| UIC-SAL-05 | LeadDetail: stage steps, BANT qualification, next step persisted | PASS |  |  |
| UIC-SAL-06 | Disqualify requires a reason; stores reason + recycle; recycle as new | PASS |  |  |
| UIC-SAL-07 | SQL queue: tiles equal DB; convert to opportunity creates deal and redirects | PASS |  |  |
| UIC-SAL-08 | Deals board: stage column counts and amounts equal DB; list view total | PASS |  |  |
| UIC-SAL-09 | Deals: create via modal (validation, company, amount, close date) -> DB | PASS |  |  |
| UIC-SAL-10 | Deals board: drag to next stage persists; drag to Closed won asks for reason (required) | PASS |  |  |
| UIC-SAL-11 | DealDetail: stage buttons, inline edits (amount/close/next step), forecast category | PASS |  |  |
| UIC-SAL-12 | DealDetail: log note/call, task, timeline; mark lost requires reason | PASS |  |  |
| UIC-SAL-13 | Quote: add product + custom line with discount -> totals, proposal amount, PDF download | PASS |  |  |
| UIC-SAL-14 | Pipeline revenue tab: open/weighted/won tiles equal independently computed DB sums; proposals tab renders | PASS |  |  |
| UIC-SAL-15 | Sales dashboard: tiles match DB (won amount/count, open pipeline); period selector changes numbers | PASS |  |  |
| UIC-SAL-16 | Sales reports: every tab renders without errors | PASS |  |  |
| UIC-SAL-17 | Forecast: FY April-March quarters; won per quarter equals DB | PASS |  |  |
| UIC-SAL-18 | Custom report builder: build, run, save, reopen, delete | PASS |  |  |
| UIC-SAL-19 | Targets: edit quarterly cell persists, totals row, attainment on reports | PASS |  |  |
| UIC-SAL-20 | Sales team: change level/manager persists and changes visibility | PASS |  |  |
| UIC-SAL-21 | Sales settings: save persists after reload; stale days validation | PASS |  |  |
| UIC-SAL-22 | Sales settings: add reason, product and workflow rule | PASS |  |  |
| UIC-SAL-23 | Calendar sync: Google/Microsoft show a clear "not set up" message and Connect is disabled | PASS |  |  |
| UIC-SAL-24 | Amount hiding: AGENT hidden role sees no money in deals, pipeline, reports, quotes; API masks amounts | PASS |  |  |
| UIC-SAL-25 | AGENT visibility: sees only own leads/deals; peer's record by URL is not found | PASS |  |  |
| UIC-SAL-26 | Stale-deal notification job creates a bell notification for the owner | PASS |  |  |
| UIC-SAL-27 | Deals API failure keeps the page shell | PASS |  |  |
