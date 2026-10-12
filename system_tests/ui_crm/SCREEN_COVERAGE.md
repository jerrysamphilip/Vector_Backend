# Screen coverage (final; Y = automated and passing unless noted)
| Screen | Renders | Actions tested | Roles |
|---|---|---|---|
| Contacts | Y | sort, paginate, search, column chooser, filter builder (enum/text/tag/date/owner/nested AND-OR), saved views create/rename/delete, built-in views, board drag + group-by, create + validation + duplicate + deleted hint, custom properties, bulk (property/list/owner/tag/enroll with reasons/delete), CSV + XLSX export, merge with choices, API 500 | owner, AGENT |
| ContactDetail | Y | inline stage/status, edit/cancel, tags, owner, unsubscribe/resubscribe, note/call/email/meeting, edit/delete activity, task create/complete/reopen, timeline filters, create lead, associations, delete + restore, not found, save API failure; deal-from-record and @mention: not implemented | owner, AGENT, MANAGER |
| Accounts / AccountDetail | Y | list/sort/search/segments/paginate, create, domain uniqueness, edit/cancel/rename propagation, owner, add contact, backfill, delete/restore, API failure; deals list: not implemented | owner, AGENT |
| Lists | Y | static + active create, live preview, membership via bulk bar, auto-membership, edit, tabs/search, delete, AGENT read-only | owner, AGENT |
| ImportContacts | Y | preview, mapping, new property, owner, list new/existing, update on/off, blank cells, bad rows + error file, history, bad files, one-column, no-email warning, duplicate mapping warning, company-only, xlsx, 1k rows, cancel/again, preview failure | owner, AGENT (FAIL: API history 403) |
| Tasks | Y | counters, filters, create, complete/reopen/delete, link to contact, AGENT scope, API failure | owner, AGENT |
| RecentlyDeleted | Y | list/paginate/90-day window, search, restore selected/single, AGENT blocked; purge: not implemented | owner, AGENT |
| Leads / LeadDetail | Y | split/list/board, search/owner filter, create + duplicate, stage steps, BANT gate, next step, disqualify + recycle | owner, AGENT |
| SqlQueue | Y | tiles vs DB, convert | owner |
| Deals / DealDetail | Y | board/list totals, create, drag stage, won/lost reason required, inline edit, forecast category, activity + task, quote lines + discount + PDF, delete proposal, API failure | owner, AGENT |
| Pipeline | Y | revenue sums vs DB, proposals tab | owner |
| SalesDashboard | Y | tiles vs DB, period selector | owner |
| SalesReports | Y | all 7 tabs render, forecast FY quarters, custom report build/save/reopen/delete | owner |
| SalesTargets | Y | edit/persist, attainment on reports | owner |
| SalesTeam | Y | manager change + visibility effect | owner |
| SalesSettings | Y | save/reload, validation, reasons, product, workflow rule | owner |
| Connections | Y | "not set up" message, Connect disabled | owner |
| Amount hiding | Y | UI (deals, pipeline, dashboard, reports, deal) + API masked | AGENT |
| Notifications | Y | stale-deal job creates notification | owner |
| TemplateLibrary | N | not covered | - |
