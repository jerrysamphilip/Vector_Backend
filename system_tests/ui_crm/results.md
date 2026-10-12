# ui_crm results (partial snapshot of in-progress run)

PASS 66 · FAIL 6 · BLOCKED 1

| ID | Test | Status | Detail |
|---|---|---|---|
| UIC-CD-01 | Record page renders header, stats, 3 columns of cards | PASS |  |
| UIC-CD-02 | Change lifecycle stage inline -> persisted, appears as property change with old/new | FAIL | Command failed: docker exec vector-mysql-1 mysql -uvector -pvector outreach_ai -N -e SELECT COUNT(*) FROM property_changes WHERE entity_id='d91cfee3-d0c3-4093-80a9-8761d2b96281' AND field='lifecycle_s |
| UIC-CD-03 | Change lead status inline persisted | PASS |  |
| UIC-CD-04 | Edit details: save changes (name, phone, title, company, location) -> DB; Cancel discards | PASS |  |
| UIC-CD-05 | Edit details: invalid email shows an error and saves nothing | PASS |  |
| UIC-CD-06 | Tags: add and remove in header persists | PASS |  |
| UIC-CON-01 | Contacts list renders 25/60 with pager text and header | PASS |  |
| UIC-CON-02 | Pagination next/prev/last page counts and disabled state | PASS |  |
| UIC-CON-03 | Sort by name asc/desc matches API order | PASS |  |
| UIC-CON-04 | Sort by Email and Company headers | PASS |  |
| UIC-CON-05 | Search box filters by name/email/company; empty-state; clear | PASS |  |
| UIC-CON-06 | Column chooser hides/shows columns, persists, Reset restores | PASS |  |
| UIC-CON-07 | Filter builder: enum (lifecycle stage is Lead) equals DB count | PASS |  |
| UIC-CON-08 | Filter builder: AND of two conditions (stage + country text) | PASS |  |
| UIC-CON-09 | Filter builder: toggle AND->OR changes result to union | PASS |  |
| UIC-CON-10 | Filter builder: tag has tag "hot"; "has no tags" | PASS |  |
| UIC-CON-11 | Filter builder: text contains / starts with / is empty | PASS |  |
| UIC-CON-12 | Filter builder: date "in the last N days" and owner "Me" | PASS |  |
| UIC-CON-13 | Filter builder: nested group  stage=Lead AND (country=US OR tag=hot) | PASS |  |
| UIC-CON-14 | Filter builder: remove condition / Clear all resets to 60 | PASS |  |
| UIC-CON-15 | Built-in views from sidebar: My contacts / Unassigned / Recently created | PASS |  |
| UIC-CON-16 | Save filtered view -> appears in sidebar & DB; reopening applies filter | PASS |  |
| UIC-CON-17 | Rename saved view (Update this view) and persisted | PASS |  |
| UIC-CON-18 | Save-view modal: empty name disables Save; Cancel closes | PASS |  |
| UIC-CON-19 | Delete saved view removes it from sidebar and DB | PASS |  |
| UIC-CON-20 | Board view: lane totals equal DB; drag card to another stage persists | PASS |  |
| UIC-CON-21 | Board: admin drags a card backwards (allowed for admins, BR-CM-08) and it persists | PASS |  |
| UIC-CON-22 | Board: group by lead status and switch back to table | PASS |  |
| UIC-CON-23 | Add contact: empty email -> validation message, nothing created | PASS |  |
| UIC-CON-24 | Add contact: invalid email format -> clear error, nothing created | PASS |  |
| UIC-CON-25 | Add contact: full form creates contact, company account linked, tags, redirect to detail | PASS |  |
| UIC-CON-26 | Add contact: duplicate email -> error + "Open the existing contact" link works | PASS |  |
| UIC-CON-27 | Add contact: email of a deleted contact -> restore hint (Recently deleted) | PASS |  |
| UIC-CON-28 | Add contact modal: Cancel / X close paths, no row created | PASS |  |
| UIC-CON-29 | Properties modal: create NUMBER and SELECT custom fields; appear in DB and Add form | PASS |  |
| UIC-CON-30 | Properties modal: blank label disables Add; duplicate label error | FAIL | duplicate property label should be rejected with a message (found 2 'Budget' properties) |
| UIC-CON-31 | Create contact with custom property values; shown in table column + detail; filterable | FAIL | detail shows values |
| UIC-CON-32 | Properties modal: delete a custom field | PASS |  |
| UIC-CON-33 | Bulk: select-all-on-page shows "25 selected", Clear selection works | PASS |  |
| UIC-CON-34 | Bulk: Edit property (lead status) updates DB for selected only | PASS |  |
| UIC-CON-35 | Bulk: Add to new list -> notice and list membership in DB | PASS |  |
| UIC-CON-36 | Bulk: Assign owner persists | PASS |  |
| UIC-CON-37 | Bulk: Add tag / Remove tag | PASS |  |
| UIC-CON-38 | Bulk: Enroll in campaign shows enrolled count + rejection reasons (unsubscribed, invalid) | PASS |  |
| UIC-CON-39 | Bulk: Delete (confirm) soft-deletes and notice; appears in Recently deleted | PASS |  |
| UIC-CON-40 | Export CSV downloads file whose rows equal the visible filtered set | PASS |  |
| UIC-CON-41 | Export XLSX downloads a valid workbook with the same row count | PASS |  |
| UIC-CON-42 | Duplicates modal: no duplicates state, then merge with per-property choices | PASS |  |
| UIC-CON-43 | AGENT (no manage_prospects): sees only own contacts; no Import/Export/Properties/Duplicates/checkboxes | PASS |  |
| UIC-CON-44 | AGENT: direct API refused for bulk/export/merge; own-contact create works without owner field | PASS |  |
| UIC-CON-45 | AGENT: cannot open someone else's contact by URL ("Contact not found") | PASS |  |
| UIC-CON-46 | List API 500 -> page does not crash (graceful state) | PASS |  |
| UIC-RD-01 | Recently deleted lists deleted contacts (30 minus purged-window one), paginated, days remaining | PASS |  |
| UIC-RD-02 | Search, select-all restore and single restore update DB and the Contacts list | PASS |  |
| UIC-RD-03 | Manual purge of a deleted record | BLOCKED | No purge/permanent-delete control on Recently deleted (items expire after 90 days) |
| UIC-RD-04 | AGENT without manage_prospects: page not reachable, API refused | PASS |  |
| UIC-SAL-01 | Leads split view lists open leads; stage chips filter; counts equal DB | PASS |  |
| UIC-SAL-02 | Leads list mode + board mode render with per-stage counts | PASS |  |
| UIC-SAL-03 | Leads: search and owner filter | PASS |  |
| UIC-SAL-04 | Create lead via modal (contact search) -> DB; duplicate open lead shows link to existing | PASS |  |
| UIC-SAL-05 | LeadDetail: stage steps, BANT qualification, next step persisted | FAIL | locator.check: Clicking the checkbox did not change its state |
| UIC-SAL-06 | Disqualify requires a reason; stores reason + recycle; recycle as new | PASS |  |
| UIC-SAL-07 | SQL queue: tiles equal DB; convert to opportunity creates deal and redirects | FAIL | locator.innerText: Timeout 15000ms exceeded. |
| UIC-SAL-08 | Deals board: stage column counts and amounts equal DB; list view total | PASS |  |
| UIC-SAL-09 | Deals: create via modal (validation, company, amount, close date) -> DB | PASS |  |
| UIC-SAL-10 | Deals board: drag to next stage persists; drag to Closed won asks for reason (required) | FAIL | locator.click: Timeout 15000ms exceeded. |
| UIC-TSK-01 | Tasks page: counters and list equal DB | PASS |  |
| UIC-TSK-02 | Date filters: overdue / next 7 days / no due date; status Done/All | PASS |  |
| UIC-TSK-03 | New task: title required; create with type/priority/due/assignee -> DB and visible to assignee | PASS |  |
| UIC-TSK-04 | Complete, reopen and delete (confirm) a task | PASS |  |
| UIC-TSK-05 | Task linked to a contact shows a link to it | PASS |  |
| UIC-TSK-06 | AGENT: no "Everyone" scope, only own tasks; cannot modify others' task via API | PASS |  |
| UIC-TSK-07 | Empty state and API failure | PASS |  |
