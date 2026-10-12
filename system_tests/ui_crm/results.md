# ui_crm (CRM + Sales functional UI) results

PASS 83 · FAIL 18 · BLOCKED 2

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
| UIC-CON-30 | Properties modal: blank label disables Add; duplicate label error | FAIL | unclassified | duplicate property label should be rejected with a message (found 2 'Budget' properties) |
| UIC-CON-31 | Create contact with custom property values; shown in table column + detail; filterable | FAIL | unclassified | detail shows values |
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
| UIC-CD-01 | Record page renders header, stats, 3 columns of cards | PASS |  |  |
| UIC-CD-02 | Change lifecycle stage inline -> persisted, appears as property change with old/new | FAIL | unclassified | Command failed: docker exec vector-mysql-1 mysql -uvector -pvector outreach_ai -N -e SELECT COUNT(*) FROM property_changes WHERE entity_id='d91cfee3-d0c3-4093-80a9-8761d2b96281' AND field='lifecycle_stage' |
| UIC-CD-03 | Change lead status inline persisted | PASS |  |  |
| UIC-CD-04 | Edit details: save changes (name, phone, title, company, location) -> DB; Cancel discards | PASS |  |  |
| UIC-CD-05 | Edit details: invalid email shows an error and saves nothing | PASS |  |  |
| UIC-CD-06 | Tags: add and remove in header persists | PASS |  |  |
| UIC-CD-07 | Owner change inline persists and is recorded in history | PASS |  |  |
| UIC-CD-08 | Unsubscribe then resubscribe (with confirm) updates consent + global unsubscribe list | FAIL | unclassified | locator.click: Timeout 15000ms exceeded. |
| UIC-CD-09 | Log a note -> timeline entry + DB activity | PASS |  |  |
| UIC-CD-10 | Log a call (subject, outcome, duration) -> timeline + Calls stat | PASS |  |  |
| UIC-CD-11 | Log an email and a meeting | PASS |  |  |
| UIC-CD-12 | Edit a logged activity then delete it (confirm) | FAIL | unclassified | locator.click: Timeout 15000ms exceeded. |
| UIC-CD-13 | Create task from record (overdue due date) -> card + Open tasks stat + DB; complete it | PASS |  |  |
| UIC-CD-14 | Timeline filters: Notes / Calls / Changes / All and user filter | PASS |  |  |
| UIC-CD-15 | Create lead from the record -> redirected to lead, lead in DB, Sales card lists it | FAIL | unclassified | not equal: got "NEW\|Referral\|aaf790a9-4e4b-4f99-8e76-f0c4b796034f" expected "NEW\|Referral\|37d1adee-ff26-461c-8860-066b4e91d80d" |
| UIC-CD-16 | Create a deal directly from the contact record | BLOCKED | not-implemented | Record page only offers "+ Create lead"; deals are created from a lead conversion or the Deals page |
| UIC-CD-17 | @mention in notes | BLOCKED | not-implemented | Composer has no @mention support |
| UIC-CD-18 | Associations: company link opens the company page; list + campaign cards link out | FAIL | unclassified | locator.isVisible: Error: strict mode violation: getByRole('link', { name: 'Assoc Campaign' }) resolved to 2 elements: |
| UIC-CD-19 | Unknown contact id shows "Contact not found" with back link | PASS |  |  |
| UIC-CD-20 | Property save API failure shows the error and keeps the old value | PASS |  |  |
| UIC-CD-21 | AGENT on own contact: can edit/log note, owner shown read-only, no Delete | PASS |  |  |
| UIC-CD-22 | Non-admin manager cannot move lifecycle stage backwards (forward only message) | PASS |  |  |
| UIC-CD-23 | Delete contact (confirm) -> back to list; listed in Recently deleted; restore brings it back | PASS |  |  |
| UIC-ACCOUNTS-CRASH | area accounts crashed | FAIL | test-issue | POST /accounts -> 422 {"success":false,"error":"Validation Error","detail":[{"type":"string_type","loc":["body","annual_revenue"],"msg":"Input should be a valid string","input":500000}],"code":"VALIDATION_ERROR"} |
| UIC-LST-01 | Lists page empty state, then headings/filters render | PASS |  |  |
| UIC-LST-02 | New list: name required; create static list -> DB + opens Contacts filtered by it | PASS |  |  |
| UIC-LST-03 | New list modal: cancel closes without creating | PASS |  |  |
| UIC-LST-04 | Active list: needs a criterion; live preview count equals DB; saved list has the same members | FAIL | unclassified | preview count: got null expected 7 |
| UIC-LST-05 | Active list membership follows data: changing a contact moves it in/out automatically | FAIL | unclassified | locator.innerText: Timeout 15000ms exceeded. |
| UIC-LST-06 | Static list: add members from Contacts bulk bar, remove from this list, counts match DB | PASS |  |  |
| UIC-LST-07 | Add contact modal can add the new contact to a static list | FAIL | unclassified | list card on record page |
| UIC-LST-08 | Edit list: rename + description persisted; active list criteria edit changes membership | FAIL | unclassified | locator.click: Timeout 15000ms exceeded. |
| UIC-LST-09 | Type tabs and search filter the lists | FAIL | unclassified | active only |
| UIC-LST-10 | AGENT: can see lists but cannot create/edit/delete (buttons hidden, API refuses) | PASS |  |  |
| UIC-LST-11 | Delete list (confirm) removes it, contacts are kept | PASS |  |  |
| UIC-LST-12 | Lists API failure keeps page shell | PASS |  |  |
| UIC-IMP-01 | Import page renders drop zone + options hidden until a file is chosen | PASS |  |  |
| UIC-IMP-02 | CSV preview: row/column count, suggested mapping, sample values | PASS |  |  |
| UIC-IMP-03 | Run import with defaults: result counts, DB rows, owner = me, company linked | PASS |  |  |
| UIC-IMP-04 | Re-import same file: update_existing ON updates (no duplicates); OFF leaves records untouched | FAIL | unclassified | none updated when OFF: got 12 expected 0 |
| UIC-IMP-05 | Blank cells never overwrite existing values | FAIL | unclassified | values preserved: got 0 expected 1 |
| UIC-IMP-06 | Mapping: create a new custom property from a column (type Number) -> property + values stored | PASS |  |  |
| UIC-IMP-07 | Options: add to a new list, set owner to Alice | PASS |  |  |
| UIC-IMP-08 | Options: existing list requires choosing one; imports into it | FAIL | unclassified | locator.selectOption: Timeout 15000ms exceeded. |
| UIC-IMP-09 | Bad rows (invalid email, missing email, duplicate in file): counts, reasons list, error file download | PASS |  |  |
| UIC-IMP-10 | Past imports table lists runs with error counts and downloadable error file | PASS |  |  |
| UIC-IMP-11 | Bad files: .txt / binary / empty / header-only show a clear message and no crash | PASS |  |  |
| UIC-IMP-12 | One-column CSV (email only) imports | PASS |  |  |
| UIC-IMP-13 | File without an Email/Company column: warning shown and Import disabled | PASS |  |  |
| UIC-IMP-14 | Two columns mapped to the same property show the "last one wins" warning | PASS |  |  |
| UIC-IMP-15 | Company-only import (no email column) creates companies | FAIL | unclassified | locator.waitFor: Timeout 90000ms exceeded. |
| UIC-IMP-16 | Excel (.xlsx) file imports | PASS |  |  |
| UIC-IMP-17 | Cancel resets the wizard | PASS |  |  |
| UIC-IMP-18 | "Import another file" after a run starts over | PASS |  |  |
| UIC-IMP-19 | 1,000-row CSV imports in one run (DB count + time) | PASS |  |  |
| UIC-IMP-20 | AGENT without manage_prospects cannot use import (route redirected, API 403) | FAIL | unclassified | API list imports 403: got 200 expected 403 |
| UIC-IMP-21 | Preview API failure shows an error message | PASS |  |  |
