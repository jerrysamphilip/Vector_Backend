# ui_crm contact_detail results

PASS 2 · FAIL 3 · BLOCKED 2

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-CD-02 | Change lifecycle stage inline -> persisted, appears as property change with old/new | PASS |  |  |
| UIC-CD-08 | Unsubscribe then resubscribe (with confirm) updates consent + global unsubscribe list | PASS |  |  |
| UIC-CD-12 | Edit a logged activity then delete it (confirm) | FAIL | unclassified | locator.click: Timeout 15000ms exceeded. |
| UIC-CD-15 | Create lead from the record -> redirected to lead, lead in DB, Sales card lists it | FAIL | unclassified | not equal: got "NEW\|Referral\|37d1adee-ff26-461c-8860-066b4e91d80d" expected "NEW\|Referral\|aaf790a9-4e4b-4f99-8e76-f0c4b796034f" |
| UIC-CD-16 | Create a deal directly from the contact record | BLOCKED | not-implemented | Record page only offers "+ Create lead"; deals are created from a lead conversion or the Deals page |
| UIC-CD-17 | @mention in notes | BLOCKED | not-implemented | Composer has no @mention support |
| UIC-CD-18 | Associations: company link opens the company page; list + campaign cards link out | FAIL | unclassified | locator.click: Timeout 15000ms exceeded. |
