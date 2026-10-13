# discount_approval results

PASS 30 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| DAS-001 | Threshold is off by default | PASS |  |  |
| DAS-002 | A rep cannot set the threshold (403) | PASS |  |  |
| DAS-003 | Out-of-range / non-numeric threshold is 422 | PASS |  |  |
| DAS-004 | Admin sets the threshold to 15% | PASS |  |  |
| DAS-005 | General sales settings refuse a threshold over 100 (400) | PASS |  |  |
| DAS-006 | A 15% discount at a 15% limit needs no approval | PASS |  |  |
| DAS-007 | That quote can be sent | PASS |  |  |
| DAS-008 | 25% discount: approval required, cannot send, owner can submit | PASS |  |  |
| DAS-009 | Sent / Under review / Accepted are refused with 409 before approval | PASS |  |  |
| DAS-010 | Submit note over 1000 characters is 422 | PASS |  |  |
| DAS-011 | Owner submits for approval -> PENDING | PASS |  |  |
| DAS-012 | The owner's manager is notified | PASS |  |  |
| DAS-013 | Submitting twice is 409 | PASS |  |  |
| DAS-014 | Manager's 'Awaiting my approval' list has the quote | PASS |  |  |
| DAS-015 | An unrelated manager does not see it | PASS |  |  |
| DAS-016 | Owner and unrelated manager cannot approve (403/404) | PASS |  |  |
| DAS-017 | Manager approves with a reason; approver and time recorded | PASS |  |  |
| DAS-018 | Owner is notified of the decision | PASS |  |  |
| DAS-019 | Approved quote can be sent | PASS |  |  |
| DAS-020 | Approval steps are in the deal history | PASS |  |  |
| DAS-021 | Lowering the discount keeps the approval | PASS |  |  |
| DAS-022 | Raising it above the approved discount resets to PENDING | PASS |  |  |
| DAS-023 | ...and the quote cannot be accepted | PASS |  |  |
| DAS-024 | Reject needs a reason (400) of at most 1000 characters (422) | PASS |  |  |
| DAS-025 | Manager rejects with a reason | PASS |  |  |
| DAS-026 | Owner resubmits; an admin can approve | PASS |  |  |
| DAS-027 | Another workspace gets 404 | PASS |  |  |
| DAS-028 | List filter / limit are bounded (422) | PASS |  |  |
| DAS-029 | Turning approval off lifts the gate | PASS |  |  |
| DAS-030 | Approval endpoints are published in the API spec | PASS |  |  |
