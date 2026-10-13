# ui_crm ab_subject_variants results

PASS 7 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIF-AB-01 | Subsequence tab: add variants A and B in the step editor -> rows shown, stored in the DB | PASS |  |  |
| UIF-AB-02 | Editor: a disallowed phrase is refused with the compliance message; nothing stored | PASS |  |  |
| UIF-AB-03 | Editor: edit variant B subject inline, add and delete a variant C (confirm) | PASS |  |  |
| UIF-AB-04 | Launch splits the step evenly (31 / 31); Schedule tab shows a variant badge per email | PASS |  |  |
| UIF-AB-05 | Analytics tab: per-variant stats card; below 30 sends the card says no winner yet and offers no Promote | PASS |  |  |
| UIF-AB-06 | Analytics: with 30 sends each the winner (A, highest open rate) is marked; "Show emails by variant" lists the variant column | PASS |  |  |
| UIF-AB-07 | Promote winner (confirm) -> remaining unsent emails use A, card shows promoted, editor locked | PASS |  |  |
