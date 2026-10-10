# Contact Management (BRD §5.2) results

PASS 86 · FAIL 7 · BLOCKED 4

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| CM-001 | Create contact with all standard properties (BR-CM-01) | PASS |  |  |
| CM-002 | Last activity / last contacted dates follow logged activities (BR-CM-01) | PASS |  |  |
| CM-003 | Duplicate email (exact) is rejected with 409 (BR-CM-02, §7) | PASS |  |  |
| CM-004 | Duplicate email differing only in case/whitespace is rejected (BR-CM-02) | PASS |  |  |
| CM-005 | Changing a contact's email to another contact's email is rejected (BR-CM-02) | PASS |  |  |
| CM-006 | Invalid / missing email rejected on create (BR-CM-02) | PASS |  |  |
| CM-007 | Each contact has an immutable system Record ID (BR-CM-02) | PASS |  |  |
| CM-008 | Create company with all properties; domain normalised (BR-CM-03) | PASS |  |  |
| CM-009 | Company domain is unique per workspace; duplicate name rejected (BR-CM-03) | PASS |  |  |
| CM-010 | Company auto-created from email domain; colleagues auto-associated (BR-CM-04) | PASS |  |  |
| CM-011 | Personal email domains don't create a company; company name is used instead (BR-CM-04) | PASS |  |  |
| CM-012 | Contact on a soft-deleted company's domain can still be created (BR-CM-04 edge) | FAIL | defect | POST /contacts after deleting company of ghostco-e5a905.com -> 500 {'success': False, 'error': 'An unexpected system error occurred', 'code': 'INTERNAL_SERVER_ERROR'} |
| CM-013 | Import row on a soft-deleted company's domain is not linked to the deleted company (BR-CM-04 edge) | FAIL | defect | import=201 created=1; contact linked to account with deleted_at='2026-10-10 17:56:10' |
| CM-014 | Contact linked to several companies with primary + association labels (BR-CM-05) | BLOCKED | not-implemented | Contact has a single account_id; no multi-company association or labels in API/schema |
| CM-015 | Renaming a company updates its contacts' company name (BR-CM-03) | PASS |  |  |
| CM-016 | All 8 lifecycle stages on contacts and companies; invalid rejected (BR-CM-06) | PASS |  |  |
| CM-017 | All 8 lead statuses; invalid rejected (BR-CM-07) | PASS |  |  |
| CM-018 | Non-admin can move lifecycle forward but not backward (BR-CM-08) | PASS |  |  |
| CM-019 | Admin can move lifecycle backward (BR-CM-08) | PASS |  |  |
| CM-020 | Company lifecycle backward move blocked for non-admin (BR-CM-08) | PASS |  |  |
| CM-021 | Admin creates custom properties of all 8 types (BR-CM-09) | PASS |  |  |
| CM-022 | Custom property values are validated and normalised by type (BR-CM-09) | PASS |  |  |
| CM-023 | Users without manage permission cannot create properties (BR-CM-09/38) | PASS |  |  |
| CM-024 | Required property enforced on create via UI/API (BR-CM-10) | PASS |  |  |
| CM-024b | Required property also enforced for contacts created by import (BR-CM-10) | FAIL | defect | import status=201 contacts_created=1 (row lacking required 'Must Have e5a905' was accepted) |
| CM-025 | Property change history keeps old/new value, user, time, source (BR-CM-11) | PASS |  |  |
| CM-026 | Custom property and owner changes are in history with readable values (BR-CM-11) | PASS |  |  |
| CM-027 | Record page payload: properties, company, lists, campaigns, stats in one call (BR-CM-12) | PASS |  |  |
| CM-028 | Add note and log call, email, meeting from the record (BR-CM-13) | PASS |  |  |
| CM-029 | Timeline shows notes, calls, meetings, tasks, property changes; filter by type and user (BR-CM-14) | PASS |  |  |
| CM-030 | Campaign emails (sent, opened, clicked, replied) appear on the timeline (BR-CM-14, integration) | PASS |  |  |
| CM-031 | Tasks with due date, reminder, owner, priority and a My-tasks queue (BR-CM-15) | PASS |  |  |
| CM-032 | Task visibility: an agent cannot see/edit a task of another agent (BR-CM-15/38) | PASS |  |  |
| CM-033 | @mention a colleague in a note notifies them (BR-CM-16) | BLOCKED | not-implemented | no notification for mentioned user (before=0, after=0); no mention parsing in code |
| CM-034 | Only the author or a manager can edit/delete an activity (BR-CM-13) | PASS |  |  |
| CM-035 | Contacts table: sort, paging and column chooser metadata (BR-CM-17) | PASS |  |  |
| CM-036 | Companies table: search, sort and paging (BR-CM-17) | PASS |  |  |
| CM-037 | Advanced AND / OR filters on any property incl. custom and company (BR-CM-18) | PASS |  |  |
| CM-038 | Saved views: personal vs shared; only admins share (BR-CM-19) | PASS |  |  |
| CM-039 | Default views: All, My contacts, Unassigned, Recently created (BR-CM-19) | PASS |  |  |
| CM-040 | Board view by lifecycle stage / lead status; drag-and-drop moves a card (BR-CM-20) | PASS |  |  |
| CM-041 | Global search by name, email, phone (any format), company and domain (BR-CM-21) | PASS |  |  |
| CM-042 | Search & list respect visibility: agent sees only own contacts (BR-CM-21/38) | PASS |  |  |
| CM-043 | Active list membership updates automatically from filter criteria (BR-CM-22) | PASS |  |  |
| CM-044 | Active list rejects manual members and empty criteria (BR-CM-22) | PASS |  |  |
| CM-045 | Static list holds a fixed set; add/remove; deleted contacts excluded (BR-CM-23) | PASS |  |  |
| CM-046b | Campaign wizard list endpoint GET /campaigns/lists is reachable (BR-CM-24) | FAIL | defect | GET /campaigns/lists -> 404 'Campaign not found' (shadowed by GET /campaigns/{campaign_id}) |
| CM-046 | Static and active lists can be used as a campaign audience (BR-CM-24, integration) | PASS |  |  |
| CM-047 | Import preview suggests a column mapping (BR-CM-26) | PASS |  |  |
| CM-048 | Import contacts + companies in one CSV with associations, new property, rejected-row reasons (BR-CM-25/26/29) | PASS |  |  |
| CM-049 | Import history and downloadable error file per import (BR-CM-29) | PASS |  |  |
| CM-050 | Re-import updates existing contacts matched by email (case-insensitive) and companies by domain (BR-CM-27) | PASS |  |  |
| CM-051 | Import with update_existing=false only fills blanks (BR-CM-27) | PASS |  |  |
| CM-052 | Import sets owner and adds contacts to a new static list (BR-CM-30) | PASS |  |  |
| CM-053 | Import an XLSX file (BR-CM-25) | PASS |  |  |
| CM-054 | Companies-only import creates/updates companies (BR-CM-25) | PASS |  |  |
| CM-055 | Import validation: no email/company column, bad mapping, agent, >10k rows (BR-CM-25/26) | PASS |  |  |
| CM-056 | Simultaneous imports by several users never create duplicate contacts/companies (BR-CM-28) | PASS |  |  |
| CM-057 | Import → property history → search index (integration) | PASS |  |  |
| CM-058 | Export a filtered view to CSV with chosen columns (BR-CM-31) | PASS |  |  |
| CM-059 | Export to XLSX (BR-CM-31) | PASS |  |  |
| CM-060 | Export restricted to admins and managers (BR-CM-31) | FAIL | defect | agent=403 admin=200 manager(default perms)=403 b'{"detail":"Only admins and managers can export contacts"}' |
| CM-061 | Export neutralises spreadsheet formula injection (BR-CM-31, security) | PASS |  |  |
| CM-062 | Duplicate detection: same email variants and similar name at same company (BR-CM-32) | PASS |  |  |
| CM-063 | Merge keeps chosen values, combines timeline, tasks, lists, tags, history (BR-CM-33, integration) | PASS |  |  |
| CM-064 | Merge can keep the duplicate's email; agent cannot merge; self-merge rejected (BR-CM-33) | PASS |  |  |
| CM-065 | Data-quality flags: phone format and name capitalisation (BR-CM-34) | PASS |  |  |
| CM-066 | Soft delete hides the contact everywhere; restore within 90 days (BR-CM-35) | PASS |  |  |
| CM-067 | Delete/restore permissions: agents cannot delete or see the recycle bin (BR-CM-35/38) | PASS |  |  |
| CM-068 | After 90 days: no restore; purge job removes contact and dependents (BR-CM-35, integration) | PASS |  |  |
| CM-069 | Company soft delete/restore; purge detaches contacts and removes company tasks (BR-CM-35, integration) | PASS |  |  |
| CM-070 | Bulk reassign owner; logged in history as BULK (BR-CM-36) | PASS |  |  |
| CM-071 | Bulk edit property, tags, add to (new) list, enroll in campaign and delete (BR-CM-37) | PASS |  |  |
| CM-072 | User role (agent) sees/edits only own contacts; cannot reassign or bulk (BR-CM-38) | PASS |  |  |
| CM-073 | Admin role sees every contact; manage_prospects permission grants admin-level access (BR-CM-38) | PASS |  |  |
| CM-074 | Tenant isolation: another workspace cannot read or change these records (§6 security) | PASS |  |  |
| CM-075 | Owner assignment restricted to users of the same workspace (BR-CM-36) | PASS |  |  |
| CM-076 | Subscription status change syncs with the global unsubscribe list (BR-CM-39) | PASS |  |  |
| CM-077 | Globally unsubscribed address stays unsubscribed when created via UI or import (BR-CM-39, §7) | PASS |  |  |
| CM-078 | Unsubscribe link in a campaign email updates the CRM contact (BR-CM-39, integration) | PASS |  |  |
| CM-079 | Consent and legal basis recorded per contact (BR-CM-40) | PASS |  |  |
| CM-080 | Erasure removes the contact and all dependent personal data (§7 GDPR/DPDP) | PASS |  |  |
| CM-080b | Erasure is logged (who/when/which record) (§7 'deletion logged') | FAIL | defect | audit_logs+property_changes rows for erased id=0; DELETE /prospects/{id} writes no log |
| CM-081 | Market Research journey: import → filter view → assign owners → list → campaign → export (BR-CM-41) | PASS |  |  |
| CM-082 | Company data enrichment from the domain (BR-CM-42, Could) | BLOCKED | not-implemented | no enrichment endpoint; company created from domain keeps industry/size/location empty |
| CM-083 | Contact scoring on fit and engagement (BR-CM-43, Could) | BLOCKED | not-implemented | no fit/engagement score on contact payload, filters or sort (only AI persona classification) |
| CM-084 | Acceptance: 1,000-row contact+company file imports < 10 s with associations and per-row reasons (§5.2) | PASS |  |  |
| CM-085 | Single-column CSV (just an Email column) imports correctly (BR-CM-25) | FAIL | defect | preview headers=['E', 'ail'] sample=[{'E': 'ono-e5a905.co', 'ail': None}, {'E': 'ono-e5a905.co', 'ail': None}, {'E': 'ono-e5a905.co', 'ail': None}]; import=400 {'detail': 'Map an Email column (contacts) or a Company name/domain column (companies)'} |
| CM-UI-01 | UI: log in and open the Contacts list (BR-CM-17) | PASS |  | contact visible=true errors=[] |
| CM-UI-02 | UI: contact record page shows properties, lifecycle and timeline (BR-CM-12) | PASS |  | name=true timeline=true lifecycle=true errors=[] |
| CM-UI-03 | UI: Import screen renders with a file picker (BR-CM-25) | PASS |  | import text=true file_input=true errors=[] |
| CM-UI-04 | UI: Lists, Tasks and Companies pages render without errors (BR-CM-15/17/22) | PASS |  | errors=[] |
| CM-090 | NFR: a 10,000-row file imports successfully (§6 Scale) | PASS |  |  |
| CM-091 | NFR: global search p95 < 1 s (measured at ~10k contacts; 100k target extrapolated) | PASS |  |  |
| CM-092 | NFR: contact list search (q=) p95 < 1 s at ~10k contacts | PASS |  |  |
| CM-093 | NFR: list views (sort/filter/deep page) and board p95 < 1 s at ~10k contacts | PASS |  |  |
| CM-094 | NFR: extrapolated search p50 at 100k contacts < 1 s (flag) | PASS |  |  |
