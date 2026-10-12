# Screen coverage (Y = automated case exists; run status in results.md)
| Screen | Renders | Actions tested | Roles |
|---|---|---|---|
| Contacts | Y | sort, paginate, search, columns, filter builder (enum/text/tag/date/owner/nested AND-OR), views create/rename/delete + built-in, board drag, create+validation+duplicate+deleted hint, custom props, bulk (property/list/owner/tag/enroll w/ reasons/delete), CSV+XLSX export, merge | owner, AGENT |
| ContactDetail | Y | inline props+history, edit, tags, owner, unsubscribe, note/call/email/meeting, edit/delete activity, tasks, filters, create lead, associations, delete/restore (written; partly run) | owner, AGENT, MANAGER |
| Accounts / AccountDetail | Y | list/sort/search/paginate, create, domain uniqueness, edit/rename, owner, add contact, backfill, delete/restore (written, not yet run) | owner, AGENT |
| Lists | Y | static/active create, preview, membership, edit, delete, tabs (written, not yet run) | owner, AGENT |
| ImportContacts | Y | preview, mapping, custom property, options, update/blank, bad rows+error file, bad files, xlsx, 1k rows (written, not yet run) | owner, AGENT |
| Tasks, RecentlyDeleted | Y | filters, create, complete/reopen/delete, restore, role access (PASS) | owner, AGENT |
| Leads, LeadDetail, SqlQueue | Y | list/board, search, create, BANT, disqualify, convert (partly run) | owner, AGENT |
| Deals, DealDetail, Pipeline | Y | board/list totals, create, drag, won/lost reasons, quote lines/PDF, pipeline sums (partly run) | owner |
| SalesDashboard, SalesReports, Targets, Team, Settings, Connections | written | numbers vs DB, FY quarters, custom report, targets, hierarchy, settings, not-set-up message, amount hiding, stale notification job | owner, AGENT |
| TemplateLibrary | N | not covered (Outreach area) | - |
