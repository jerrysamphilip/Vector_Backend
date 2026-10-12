# UI outreach/admin - prospects results

PASS 10 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-PROS-01 | Prospect lists empty state: zero stats, New List button, no errors | PASS |  |  |
| UIO-PROS-02 | New List validation: missing name, missing file -> inline messages; unsupported file type rejected | PASS |  |  |
| UIO-PROS-03 | Upload CSV -> preview: 2 accepted / 2 need attention with per-row reasons (invalid email, duplicate) | PASS |  |  |
| UIO-PROS-04 | Opening the preview URL directly (no upload state) redirects back to Prospect lists, no crash | PASS |  |  |
| UIO-PROS-05 | Confirm Upload creates the list with exactly the accepted rows; list appears in the table without reload | PASS |  |  |
| UIO-PROS-06 | Search lists, sort by name; View opens the members modal with the 2 contacts | PASS |  |  |
| UIO-PROS-07 | Stats tiles match DB (total contacts, in campaigns) | PASS |  |  |
| UIO-PROS-08 | Delete list: row trash -> confirm dialog; Cancel keeps, Delete removes list (DB)  | PASS |  |  |
| UIO-PROS-09 | Revalidate Bounced (admin only): opens modal for owner; agent is redirected away from the screen | PASS |  |  |
| UIO-PROS-10 | Upload API 500 shows a readable message in the modal | PASS |  |  |
