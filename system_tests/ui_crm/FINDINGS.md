# UI CRM/Sales functional tests - findings (snapshot; run still in progress)

Counts from the logs so far: PASS 66, FAIL 6, BLOCKED(not-implemented) 1 (results.md). Not finished when this was written:
contact_detail (UIC-CD-03..23), accounts (UIC-ACC), lists_import (UIC-LST/IMP) and sales (UIC-SAL-11..27) were still running
(the host was saturated; each case took minutes). Re-run: `node run_all.mjs` or `node <area>.mjs` (ONLY=<regex> filters cases).
Tenants are reused (state/, created by SQL because /auth/register is limited to 10/hour/IP); every run wipes tenant CRM data first.

## Frontend defects fixed (Vector_Frontend/src, built and deployed)
1. api/http.js normalizeError replaced structured `detail` objects with their message, losing prospect_id / lead_id.
   Effects: duplicate-email "Open the existing contact" link never appeared (ContactFormModal); "This contact already has an open lead. Open it"
   (NewLeadModal), reply-suggestion 409 redirect (Leads.jsx ReplySuggestions) and InboxTab "create lead" 409 redirect were all dead.
   Fix: normalizeError keeps `data.detail_data`; the four consumers read it. Verified by UIC-CON-26 (PASS after fix).
2. ContactFormModal: for a deleted contact with the same email the button pointed at a 404 record; it now says "Open Recently deleted" and links there.

## Defects (not fixed)
- defect-backend, low: POST /contacts/fields accepts duplicate property labels (stored as budget, budget_2), so forms show two identically
  labelled fields (contacts_router.py create_field_definition ~L1076). UIC-CON-30.
- not-implemented (UI): no @mention in notes; no "create deal" on the contact record (only lead); AccountDetail has no deals list;
  Recently deleted has no manual purge control; no "Remove" of list members from the Lists page (done via Contacts bulk bar).
- Observed risk (code reading, not yet tested): Connections.jsx / SalesTargets.jsx put `e.response.data.detail` straight into state; the http
  interceptor normalises objects to strings, so currently safe.

## Failures still to triage (probably test issues)
UIC-CD-02 (wrong property_changes column in my SQL), UIC-CON-31 (detail value text locator), UIC-SAL-05 (BANT checkbox re-renders on each save),
UIC-SAL-07, UIC-SAL-10 (selectors). No product defect confirmed for these.

## Environment notes
Registration 10/h/IP and login 60/5min/IP limits are shared with other agents; the suite avoids both (SQL tenants, cached cookies/tokens).
