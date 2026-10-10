# Contact Management — System & Integration Test Cases

Source: BRD v2.0 §5.2 (BR-CM-01..43, acceptance criteria), §6 NFRs (performance, scale, security), §7 data & compliance.
Automation: `run_contacts.py` (API + SQL checks, stdlib + `../common.py`), `ui_contacts.mjs` (Playwright, run by the script).
Every run signs up fresh workspaces: **A** (owner SUPER_ADMIN + ADMIN "Ada", MANAGER "Max", AGENT "Ann", AGENT "Bob",
AGENT "Pia" with `custom_permissions=["manage_prospects"]`), **B** (a second tenant for isolation), **P** (performance tenant).

Type: **System** = user journey through the product surface; **Integration** = crosses components (import/history/search
index, merge/dependents, purge/FKs, campaigns/suppression, email events). Priority: P1 must-pass for go-live, P2 important, P3 edge.

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected result | Automated? |
|---|---|---|---|---|---|---|---|
| CM-001 | BR-CM-01 | System | P1 | Workspace A | POST /contacts with name, email, phone, mobile, job title, LinkedIn, city/state/country, lead source, owner=Ada; GET it | All values stored; record has prospect_id, create date, owner name, time zone derived from state, last activity / last contacted fields | Y |
| CM-002 | BR-CM-01 | Integration | P2 | Contact exists | Log NOTE, check dates; log CALL (outcome Connected), check again | NOTE sets last activity only; CALL sets last contacted and moves lead status NEW→CONNECTED | Y |
| CM-003 | BR-CM-02, §7 | System | P1 | Jane exists | POST /contacts with Jane's email | 409, response points at the existing record | Y |
| CM-004 | BR-CM-02 | System | P1 | Jane exists | Create with UPPER-case email and with surrounding spaces | 409 (or 422); still exactly one row for the address | Y |
| CM-005 | BR-CM-02 | System | P1 | Two contacts | PATCH contact B's email to A's email (different case) | 409 | Y |
| CM-006 | BR-CM-02 | System | P2 | – | Create with invalid email; create without email | 422 both | Y |
| CM-007 | BR-CM-02 | System | P3 | Jane exists | PATCH with a forged `prospect_id` | Record ID unchanged (36-char UUID) | Y |
| CM-008 | BR-CM-03 | System | P1 | – | POST /accounts with name, URL-style domain, industry, employees, revenue "1,250,000", phone, address, owner | Domain normalised to `globex-<rid>.com`, revenue 1250000, all fields returned | Y |
| CM-009 | BR-CM-03 | System | P1 | Globex exists | Create company with same domain; same name; PATCH another company to the same domain | 409 for each | Y |
| CM-010 | BR-CM-04 | Integration | P1 | – | Create two contacts on the same corporate domain | One company auto-created from the domain; both contacts linked; contact_count ≥ 2 | Y |
| CM-011 | BR-CM-04 | System | P2 | – | Create gmail.com contact with company_name | No gmail.com company; linked to the named company | Y |
| CM-012 | BR-CM-04 | Integration | P2 | Company soft-deleted | Create a new contact on the deleted company's domain | 201; contact created (relinked or new company), never a 500 | Y |
| CM-013 | BR-CM-04 | Integration | P2 | Company soft-deleted | Import a contact on that domain | Contact not linked to the soft-deleted company | Y |
| CM-014 | BR-CM-05 | System | P2 | – | Inspect API/schema for multi-company associations with primary flag + labels | Contact can be associated with several companies with labels | Y (detects absence → not-implemented) |
| CM-015 | BR-CM-03 | Integration | P3 | Jane linked to company | Rename the company | Contacts' company name follows | Y |
| CM-016 | BR-CM-06 | System | P1 | – | Set each of the 8 stages on a contact and a company (admin); try "PROSPECT" | Meta lists 8 stages in order; all accepted; invalid → 400 | Y |
| CM-017 | BR-CM-07 | System | P1 | – | Set each of the 8 lead statuses; try "HOT" | All accepted; invalid → 400 | Y |
| CM-018 | BR-CM-08 | System | P1 | Agent owns contact | Agent moves LEAD→MQL then MQL→LEAD | Forward 200; backward 400 | Y |
| CM-019 | BR-CM-08 | System | P1 | Contact at CUSTOMER | Admin moves to SUBSCRIBER | 200 | Y |
| CM-020 | BR-CM-08 | System | P3 | Agent owns company | Agent moves company LEAD→SQL→LEAD | Forward 200, backward 400 | Y |
| CM-021 | BR-CM-09/10 | System | P1 | Owner | Create TEXT, NUMBER, DATE, SELECT(dropdown), MULTI_CHECKBOX, RADIO, PHONE, URL properties in group "Qualification" | 201 each; agent can read definitions with group | Y |
| CM-022 | BR-CM-09 | System | P1 | Properties exist | Save valid values; then invalid per type; unknown key | Valid normalised (42 → int, multi ordered, URL https://); invalid → 400 each | Y |
| CM-023 | BR-CM-09/38 | System | P2 | – | Agent creates property; owner creates bad type / SELECT without options | 403; 400; 400 | Y |
| CM-024 | BR-CM-10 | System | P2 | Required property defined | Create contact without / with it | 400 / 201 (required flag reset afterwards) | Y |
| CM-024b | BR-CM-10 | Integration | P3 | Required property defined | Import a row without it | Row rejected with reason (creates must not bypass required properties) | Y |
| CM-025 | BR-CM-11 | Integration | P1 | Jane | Admin changes phone + stage; GET history; filter by field | Rows with old/new value, user, time, source=UI; stage shown with labels; field filter works | Y |
| CM-026 | BR-CM-11 | Integration | P2 | Custom contact | Change custom NUMBER and owner | History rows `custom.<key>` 42→43 with label, owner shown by name | Y |
| CM-027 | BR-CM-12 | System | P1 | Jane | GET record | One payload with properties, company card, lists, campaigns, stats | Y |
| CM-028 | BR-CM-13 | System | P1 | Contact | Log NOTE/CALL/EMAIL/MEETING; invalid type; empty; duration 5000 | 201 ×4, 400, 400, 422; stats count 1 each | Y |
| CM-029 | BR-CM-14 | Integration | P1 | Activities, task, property change by two users | GET timeline; types=CALL; user_id=Ada | All kinds present newest-first; type filter and user filter work | Y |
| CM-030 | BR-CM-14 | Integration | P1 | Outbound + inbound email rows and open×2/click events (SQL in own tenant) | GET timeline + record | EMAIL_SENT, one EMAIL_OPENED, EMAIL_CLICKED, EMAIL_RECEIVED; stats sent=1 replies=1; last contacted set | Y |
| CM-031 | BR-CM-15 | System | P1 | Contact | Owner creates overdue HIGH task for Ann with reminder; Ann lists "mine"; invalid priority; no title; Ann marks DONE | Task in Ann's queue flagged overdue + reminder due; 400s; DONE sets completed_at and leaves the open queue | Y |
| CM-032 | BR-CM-15/38 | System | P2 | Bob's task | Ann PATCHes it; owner lists scope=all | 404 for Ann; owner sees it | Y |
| CM-033 | BR-CM-16 | Integration | P3 | – | Note containing "@Ada…"; Ada reads /notifications | Ada notified | Y (absence → not-implemented) |
| CM-034 | BR-CM-13 | System | P3 | Ann's note | Bob edits; admin edits; Ann deletes | 403/404; 200; 200 | Y |
| CM-035 | BR-CM-17 | System | P1 | 5 tagged contacts | Sort by name asc/desc, page_size=2 pages; invalid sort; page_size=10000; meta columns | Case-insensitive order, correct pages, 422s, ≥20 chooser columns | Y |
| CM-036 | BR-CM-17 | System | P2 | 3 companies | Search, sort, page companies; search by domain | Correct order/pages; domain search hits | Y |
| CM-037 | BR-CM-18 | System | P1 | 3 tagged contacts | AND, nested OR, custom numeric gt, custom is_empty, company.domain; bad operator/field/JSON | Exact expected sets; 400 for bad input | Y |
| CM-038 | BR-CM-19 | System | P1 | – | Owner creates personal + shared view; agent tries to share; bad filter; agent lists/edits | Agent sees shared not personal; 403 share; 400 bad filter; 403/404 edits | Y |
| CM-039 | BR-CM-19 | System | P2 | Mine / Ada's / unassigned contacts | owner=me, owner=unassigned, created in last day | Correct sets for All / My / Unassigned / Recently created | Y |
| CM-040 | BR-CM-20 | System | P2 | Tagged LEAD + MQL contacts | Board by stage; move card (PATCH = drop); board by lead status | Lanes for all 8 values; counts move | Y |
| CM-041 | BR-CM-21 | System | P1 | Contact with phone + company | /search by name, email, phone digits, formatted phone, company, domain; list q with spaced phone; q of 1 char | All hit; company found by domain; 422 for 1 char | Y |
| CM-042 | BR-CM-21/38 | System | P1 | Owner's contact | Agent searches for it, and for own contact | Not visible; own visible | Y |
| CM-043 | BR-CM-22 | Integration | P1 | Active list tag∧SQL | Change a contact to SQL; check list, members, record lists; move back | Membership updates immediately (≤5 min target), shows on record, leaves when criteria no longer match | Y |
| CM-044 | BR-CM-22 | System | P2 | Active list | Manual add; empty criteria; agent creates list; blank name | 400; 400; 403; 400 | Y |
| CM-045 | BR-CM-23 | System | P1 | 4 contacts | Add (incl. unknown id), re-add, remove one, change property, soft-delete one | added=4 not_found=1, already=1, removed=1, final count 2 | Y |
| CM-046 | BR-CM-24 | Integration | P1 | Static + active list, campaign DRAFT, one member unsubscribed, one deleted | Enroll via list_ids; read the wizard's list picker | Static + active members enrolled; unsubscribed and deleted not; both lists in picker with live counts | Y |
| CM-046b | BR-CM-24 | Integration | P3 | – | GET /campaigns/lists | 200 list of lists | Y |
| CM-047 | BR-CM-26 | System | P2 | 20-row journey CSV | POST /imports/preview | Headers, 5-row sample, suggested mapping (Email, Company, Company Domain, Job Title, Owner Email) | Y |
| CM-048 | BR-CM-25/26/29 | System | P1 | Journey CSV: 15 good rows over 3 companies + 5 bad rows | Import with mapping incl. `new:NUMBER:Imported Seats` and owner email | 15 created, 3 companies, associations by domain/name/email-domain, new NUMBER property filled, owner/tags/stage set, 5 errors with reasons | Y |
| CM-049 | BR-CM-29 | System | P1 | CM-048 | Import history, detail, errors.csv; agent and tenant B fetch it | Job listed; CSV has Row, Error + original columns, 5 rows with reasons; 404 for others | Y |
| CM-050 | BR-CM-27 | Integration | P1 | CM-048 | Re-import 6 emails in UPPER case with new title and company website/employees | 0 created, 6 updated; no new rows/companies; history source=IMPORT | Y |
| CM-051 | BR-CM-27 | System | P3 | CM-050 | Import with update_existing=false | Existing title kept; blank LinkedIn filled | Y |
| CM-052 | BR-CM-30 | System | P2 | – | Import with owner_id=Bob and new_list_name; foreign owner | Static list of 5, all owned by Bob; 400 for foreign owner | Y |
| CM-053 | BR-CM-25 | System | P2 | – | Preview + import a 4-row XLSX | 4 created, 1 company | Y |
| CM-054 | BR-CM-25 | System | P2 | Globex exists | Companies-only import (2 new, 1 existing by domain, 1 blank) | 2 created, 1 updated, 1 error | Y |
| CM-055 | BR-CM-25/26 | System | P2 | – | No key column; unknown target; agent; 10,001 rows; garbage XLSX | 400; 400; 403; 400 "10,000"; 400 | Y |
| CM-056 | BR-CM-28 | Integration | P1 | 3 users with manage permission | Import the same 300 emails (rotated order) concurrently | 300 rows, 300 distinct, 7 companies, created sum 300, no errors | Y |
| CM-057 | BR-CM-21/25/11 | Integration | P1 | – | Import with full name + mobile; search by mobile digits and name; history | Found via search index; "created" history row source=IMPORT | Y |
| CM-058 | BR-CM-31 | System | P1 | Imported contacts | Export view (tag + MQL) CSV with 4 columns | Header labels; rows equal to view; stage labels | Y |
| CM-059 | BR-CM-31 | System | P2 | Imported contacts | Export XLSX | Valid workbook, rows = view total + header | Y |
| CM-060 | BR-CM-31 | System | P1 | Default roles | Export as agent, admin, manager | Agent 403; admin and manager 200 | Y |
| CM-061 | BR-CM-31 | System | P3 | Contact with name "=HYPERLINK(...)" | Export | Cell prefixed with `'` | Y |
| CM-062 | BR-CM-32 | System | P1 | gmail dot-variants; Robert/Rob Brown at one company | GET /contacts/duplicates; agent call | Both pairs grouped with reasons; agent 403 | Y |
| CM-063 | BR-CM-33 | Integration | P1 | Dup has call, task, list, campaign, tags, phone; primary has note | Merge choosing dup's job title | Chosen value kept, blanks filled, tags unioned, timeline/task/list/campaign/history moved, duplicate gone | Y |
| CM-064 | BR-CM-33 | System | P2 | Robert/Rob | Self-merge; agent merge; merge keeping dup's email; recreate old address | 400; 403; email swapped; 201 | Y |
| CM-065 | BR-CM-34 | System | P2 | – | Create "john SMITHSON" phone "12ab"; and a clean contact | Phone and capitalisation flags; none on the clean contact | Y |
| CM-066 | BR-CM-35 | System | P1 | Contact | Delete; GET/search/list; recycle bin; recreate same email; restore; history | Hidden everywhere; in bin with purge date and deleter; 409 deleted=true; restore 1; history delete+restore | Y |
| CM-067 | BR-CM-35/38 | System | P2 | Agent | Agent deletes own, bulk-deletes, opens bin, restores | 403 each | Y |
| CM-068 | BR-CM-35 | Integration | P1 | Deleted contact with note, task, list membership; deleted_at set to −91 d (SQL, own tenant) | Bin, restore, run purge_expired | Not in bin; restore 0; purge removes contact, activities, tasks, memberships, history | Y |
| CM-069 | BR-CM-35 | Integration | P2 | Company with contact and task | Delete, bin, restore, delete; −91 d; purge | Company gone; contact kept with account NULL; company task removed | Y |
| CM-070 | BR-CM-36 | System | P1 | 3 contacts | Bulk assign to Bob; foreign owner | updated 3; Bob sees them; history source BULK; 400 | Y |
| CM-071 | BR-CM-37 | System | P1 | 4 contacts, campaign | Bulk set stage, invalid status, add tags (dedupe), add to new list, set custom dropdown, enroll 2, delete 1, bulk edit email, unknown action | Results per action; invalid values reported as skipped; 400s | Y |
| CM-072 | BR-CM-38 | System | P1 | Agent, owner's contact | Agent GET/PATCH/timeline/activity on it, reassign own, bulk, list, create duplicate, edit own | 404s, 403s, only own in list, 409 without leaking id, own edit 200 | Y |
| CM-073 | BR-CM-38 | System | P1 | – | Admin vs owner totals; Pia (agent + manage_prospects) reads Ann's contact | Admin sees all; permission grants access | Y |
| CM-074 | §6 security | Integration | P1 | Tenant B | B calls get/patch/delete/timeline/history/company/list/import/bulk/merge/restore/task/erase/list-add/search on A's records; B creates same email | All 404/403/0; A unchanged; same email allowed in B | Y |
| CM-075 | BR-CM-36 | System | P2 | – | Owner = tenant B user (create/patch); agent assigns to Bob | 400; 400; 403 | Y |
| CM-076 | BR-CM-39 | Integration | P1 | Contact | Unsubscribe; opt-in; invalid; bulk unsubscribe 2 | global_unsubscribes row added/removed; consent source/time; 400; 2 rows | Y |
| CM-077 | BR-CM-39, §7 | Integration | P1 | Addresses in global unsubscribe list | Create via API with OPT_IN; import the other | Both UNSUBSCRIBED | Y |
| CM-078 | BR-CM-39 | Integration | P1 | Outbound email row (CM-030) | POST unsubscribe link | Contact UNSUBSCRIBED, global row, UNSUBSCRIBED on timeline | Y |
| CM-079 | BR-CM-40 | System | P2 | – | Create with legal basis CONSENT; change; invalid | Stored, history row, 400 | Y |
| CM-080 | §7 | Integration | P1 | Contact with emails, events, note | DELETE /prospects/{id} (erasure) | Contact and dependent personal data removed | Y |
| CM-080b | §7 | Integration | P1 | as above | Look for a deletion log | Erasure logged (who/when/what) | Y |
| CM-081 | BR-CM-41 | System | P1 | Pia (research, manage_prospects) | Import 12 research contacts → shared filtered view → bulk assign Ann → add to new list → enroll list in campaign → export → Ann opens list | 12/3 created, 6 in view, assigned, listed, enrolled, export shows owner, Ann sees 6 | Y |
| CM-082 | BR-CM-42 (C) | System | P3 | – | Create company from domain only | Industry/size/location enriched | Y (absence → not-implemented) |
| CM-083 | BR-CM-43 (C) | System | P3 | – | Look for fit/engagement score | Score on record, filter/sort | Y (absence → not-implemented) |
| CM-084 | §5.2 acceptance | Integration | P1 | – | Import 1,000-row contact+company file (990 good, 10 bad) | < 10 s, 50 companies, all 990 linked by domain, a reason per rejected row | Y |
| CM-085 | BR-CM-25 | System | P1 | – | Preview + import a CSV with only an `Email` column | Header "Email" detected; 3 contacts created | Y |
| CM-090 | §6 Scale | System | P1 | Tenant P | Import a 10,000-row contact+company file | 10,000 created, 800 companies; time reported | Y |
| CM-091 | §6 Perf, §5.2 acceptance | System | P1 | CM-090 | 20 global searches (name, email, phone, company, domain, no-match), after warm-up | p95 < 1 s at ~10k; 100k extrapolated | Y |
| CM-092 | §6 Perf | System | P1 | CM-090 | Same 20 terms on the contacts list (q=) | p95 < 1 s | Y |
| CM-093 | §6 Perf | System | P1 | CM-090 | List views: default, sort by name, stage filter, page 200, AND/IN filter, owner=me; board | p95 < 1 s | Y |
| CM-094 | §6 Perf | System | P2 | CM-091 | Extrapolate search p50 to 100k from small vs 10k tenant | < 1 s (flagged: estimate, not measured) | Y |
| CM-UI-01 | BR-CM-17 | System (UI) | P1 | Owner credentials | Log in via the form, open /app/contacts | Table with contacts, no page/console errors, no 5xx | Y (Playwright) |
| CM-UI-02 | BR-CM-12 | System (UI) | P1 | Jane | Open /app/contacts/{id} | Name, lifecycle, timeline shown, no errors | Y (Playwright) |
| CM-UI-03 | BR-CM-25 | System (UI) | P1 | Owner | Open /app/import | Import page with file picker, no errors | Y (Playwright) |
| CM-UI-04 | BR-CM-15/17/22 | System (UI) | P2 | Owner | Open /app/lists, /app/tasks, /app/accounts | Render without errors | Y (Playwright) |
| CM-M01 | BR-CM-12 | System (UI) | P2 | – | Visually verify three-column record layout and associated records panel | Layout matches HubSpot-style 3 columns | N (visual judgement) |
| CM-M02 | BR-CM-20 | System (UI) | P3 | – | Drag a card between board lanes in the browser | Card moves, stage saved | N (drag gesture flaky to automate; API path covered by CM-040) |
| CM-M03 | BR-CM-15 | Integration | P3 | Task with reminder | Wait for reminder time | In-app/email reminder delivered | N (depends on worker schedule/email) |
| CM-M04 | §6 Perf | System | P2 | 100k-contact tenant | Measure search/list at 100k | < 1 s | N (instructed not to insert 100k rows; CM-094 extrapolates) |
| CM-M05 | §6 Scale | System | P3 | 20 users | 20+ concurrent users working on contacts | No errors / acceptable latency | N (load-test tooling out of scope; CM-056 covers concurrent imports) |
