# UI outreach/admin - wizard results

PASS 11 · FAIL 1 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-WIZ-01 | Step 1 validation: empty name -> "Campaign name is required"; name without inbox -> "Select at least one inbox" | PASS |  |  |
| UIO-WIZ-02 | Step 1 saves identity, sender details, CTA, send window, distribution + start date to the campaign row | PASS |  |  |
| UIO-WIZ-03 | Step 2 sequence: add follow-up with wait days, saved as 2 steps (wait_days persisted); Back keeps step 1 values | PASS |  |  |
| UIO-WIZ-04 | Step 3 prospects: list selection, deselect one contact -> "3 of 4 contacts selected" | PASS |  |  |
| UIO-WIZ-05 | Process & Generate (LLM absent): templates generated for EVERY sequence step (2 emails) | FAIL | defect | 1 template(s) stored for a 2-step sequence (follow-up step has no content) |
| UIO-WIZ-06 | Edit subject (textarea) persists to DB; Cancel discards; single-brace variable triggers the warning dialog | PASS |  |  |
| UIO-WIZ-07 | Edit body in the rich-text editor persists to DB | PASS |  |  |
| UIO-WIZ-08 | Live Preview substitutes real prospect data (name/company), and prospect switcher works | PASS |  |  |
| UIO-WIZ-09 | Regenerate subject with LLM absent: sensible result or error, no crash / [object Object] | PASS |  |  |
| UIO-WIZ-10 | Click an undetected persona -> generates or shows a clear error (LLM absent) | PASS |  |  |
| UIO-WIZ-11 | Launch step shows correct specs; Back to Edit returns; EXECUTE launches: ACTIVE, 3 enrolled (excluded contact skipped), redirect to list | PASS |  |  |
| UIO-WIZ-12 | Launch without sender identity shows a clear message and does not activate the campaign | PASS |  |  |
