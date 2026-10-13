# ui_crm template_library results

PASS 17 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIF-TL-01 | Sidebar Templates opens /app/templates: heading, New template button and the empty state on a new workspace | PASS |  |  |
| UIF-TL-02 | Validation: empty name, then empty subject / body -> server message in the modal; nothing stored | PASS |  |  |
| UIF-TL-03 | Create: merge-field buttons insert at the cursor of subject / body; row shows name, category, owner, Used 0; DB row shared | PASS |  |  |
| UIF-TL-04 | Edit: modal opens pre-filled; rename + new subject -> row and DB updated | PASS |  |  |
| UIF-TL-05 | Category filter lists the categories and narrows the list; search box filters by name / subject | PASS |  |  |
| UIF-TL-06 | Preview: modal with the template name and sample values substituted (no {{tokens}} left) | PASS |  |  |
| UIF-TL-07 | Render for a real contact (API): contact values substituted, sample=false; unknown contact -> 404 | PASS |  |  |
| UIF-TL-08 | Usage count: two renders with count_use -> Used 2 in the list and the template sorts first | PASS |  |  |
| UIF-TL-09 | AGENT sees shared templates with Preview only (no Edit / Delete); API edit / delete of an admin template -> 403 | PASS |  |  |
| UIF-TL-10 | AGENT creates a private template: "private" marker, own Edit / Delete; another agent cannot see or open it | PASS |  |  |
| UIF-TL-11 | Category dropdown only offers categories of templates the user can see (no category of another user's private template) | PASS |  |  |
| UIF-TL-12 | Admin can edit a template an AGENT shared (Edit shown, rename saved) | PASS |  |  |
| UIF-TL-13 | Save fails (API 500): modal stays open with the error, nothing stored | PASS |  |  |
| UIF-TL-14 | Delete fails (API 500): the row stays and the user is told the delete failed | PASS |  |  |
| UIF-TL-15 | List fails (API 500): an error is shown, not the "No templates yet" empty state | PASS |  |  |
| UIF-TL-16 | Delete (confirm): row gone, DB row removed; Preview / Edit of a deleted template id via API -> 404 | PASS |  |  |
| UIF-TL-17 | Deleting every visible template brings the empty state back | PASS |  |  |
