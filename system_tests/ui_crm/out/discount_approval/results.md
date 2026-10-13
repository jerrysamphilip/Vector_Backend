# ui_crm discount_approval results

PASS 6 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIF-DA-01 | Sales settings: admin sets the discount approval threshold to 15% (saved in the workspace settings) | PASS |  |  |
| UIF-DA-02 | Quote above the limit: status change to Sent is refused with a message; DB status stays DRAFT | PASS |  |  |
| UIF-DA-03 | Quote modal shows "Approval needed"; owner submits with a note -> "Awaiting approval", DB PENDING, manager notified | PASS |  |  |
| UIF-DA-04 | Pipeline -> Discount approvals: manager sees the quote under "Awaiting my approval"; Reject is disabled without a reason; Approve -> DB APPROVED | PASS |  |  |
| UIF-DA-05 | Deal page shows the "Discount approved" badge and the quote can now be sent | PASS |  |  |
| UIF-DA-06 | Manager rejects from the quote modal with a reason; owner sees "Discount rejected" with the reason | PASS |  |  |
