# UI CRM/Sales functional tests - findings (final)

Totals: PASS 150 / FAIL 3 / BLOCKED (not-implemented) 4  (results.md, results.json; case IDs UIC-<area>-NN).
Areas: contacts (46), contact_detail (23), accounts (16), lists + import (34), tasks + recently deleted (11), sales (27).
Re-run: `node run_all.mjs` (all) or `node <area>.mjs`; `ONLY=<regex>` filters cases. Tenants are reused (SQL-created, data wiped per run) because
/auth/register (10/h/IP) and login (60/5min/IP) limits are shared with other agents.

## Defects
### Backend (report only)
1. medium - GET /imports returns 200 for a user without manage_prospects (UIC-IMP-20). Preview/run are 403, but import history (file names, row/error counts)
   is readable by an AGENT; the UI route is blocked, the API is not. Guard in app/routers/import_router.py (list endpoint; the 403 checks are at ~L166/L193).
2. low - import result counters wrong when update_existing=false (UIC-IMP-04B): POST /imports with {"update_existing":false} on an existing email leaves the
   contact untouched but returns contacts_updated=1, companies_updated=1; the result card then says "Contacts updated 12".
3. low - POST /contacts/fields accepts duplicate property labels (UIC-CON-30): stored as budget / budget_2, forms show two identical labels
   (contacts_router.py create_field_definition ~L1076).
4. low - company-only import: the "Industry" header auto-maps to the contact property, the Import button stays enabled and the run fails with
   "Contact properties need an Email column to match contacts" (UX; mapping suggestion should use company.industry when no email column is mapped).

### Frontend defects found AND fixed (Vector_Frontend/src, built + deployed)
1. api/http.js normalizeError dropped structured `detail` (prospect_id / lead_id): duplicate-email "Open the existing contact" link, "already has an open lead - Open it",
   reply-suggestion and InboxTab 409 redirects were all dead. Now kept as `data.detail_data`; consumers: ContactFormModal.jsx, pages/Leads.jsx (x2), InboxTab.jsx.
2. ContactFormModal: duplicate of a DELETED contact linked to a 404 record; now "Open Recently deleted".
3. pages/Accounts.jsx AccountFormModal: editing any company that has an annual revenue sent a numeric value; the API requires a string -> 422, and the error
   sits below the fold so Save looked dead (UIC-ACC-07). Now sends String(value).
4. components/contacts/shared.jsx CustomFieldInput: NUMBER custom fields were stored as strings ("125000"), so the record page showed 125000 instead of 125,000
   and numeric semantics were lost (UIC-CON-31). Now emits numbers.

### Frontend, not fixed
- Price list / quote unit prices are shown with 0 decimals (money() uses maximumFractionDigits 0): $49.50 displays as "$50" (Sales settings > Products).
- Accessibility: Email subscription (Unsubscribe/Resubscribe) buttons sit inside a <label>, so their accessible name is the whole label text.

### Not implemented (UI)
No @mention in notes (UIC-CD-17); no create-deal on the contact record, only create-lead (UIC-CD-16); AccountDetail has no deals list (UIC-ACC-12);
Recently deleted has no purge control (UIC-RD-03); Pipeline "kanban" is the Deals board (drag verified in UIC-SAL-10).

## Verified OK (selection)
Filter builder AND/OR/nested vs SQL counts; saved views create/rename/delete; board drag; bulk actions incl. enrollment rejection reasons; CSV/XLSX export rows;
merge with per-property choice; custom properties; permissions (AGENT UI + API 403); import wizard (preview, mapping, new property, owner/list, update/blank,
bad rows + error file, xlsx, 1k rows); lists static/active; tasks; recently deleted; leads BANT/disqualify/convert; deals board/list totals, won/lost reasons, quotes + PDF;
pipeline/dashboard/forecast numbers vs DB (FY Apr-Mar); custom report build/save/delete; targets; hierarchy visibility effect; settings persist; Calendar sync "not set up";
amount hiding (UI + API); stale-deal notification job.

## Coverage gaps
TemplateLibrary not covered (Outreach area). Sales: board drag for Leads (leads board has no drag); mobile layouts; Google/Microsoft OAuth flow (no credentials).
Triaged test-side issues (fixed): ambiguous selectors (labels wrapping inputs, duplicate mobile/desktop elements), wrong SQL column names, FY label assertions, order dependencies.
