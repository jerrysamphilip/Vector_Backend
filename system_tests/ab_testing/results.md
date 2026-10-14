# ab_testing results

PASS 33 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| AB-S001 | OpenAPI lists the subject-variant endpoints (CRUD, stats, promote) | PASS |  |  |
| AB-S002 | Owner adds variants A and B to step 1 | PASS |  |  |
| AB-S003 | Listing shows two variants and the test as active | PASS |  |  |
| AB-S004 | Out-of-bounds weight / subject length / empty subject are 422 | PASS |  |  |
| AB-S005 | A subject with a disallowed phrase fails the compliance check (400) | PASS |  |  |
| AB-S006 | A multi-line subject (header injection) is refused (422) | PASS |  |  |
| AB-S007 | At most four variants per step (fifth is 409) | PASS |  |  |
| AB-S008 | Unsent variants can be deleted (204) | PASS |  |  |
| AB-S010 | An agent without manage_campaigns cannot change variants (403) | PASS |  |  |
| AB-S011 | An agent can read the variant stats (200) | PASS |  |  |
| AB-S012 | Another workspace gets 404 on list / stats / create | PASS |  |  |
| AB-S020 | Campaign launches (start date in the future) | PASS |  |  |
| AB-S021 | Launch assigns every email a variant, split evenly (32 / 32) | PASS |  |  |
| AB-S022 | Each email carries its variant's subject, personalised | PASS |  |  |
| AB-S023 | Schedule view shows the variant per email | PASS |  |  |
| AB-S030 | Below 30 sends per variant no winner is named (insufficient_data) | PASS |  |  |
| AB-S031 | Promotion is refused before the sample size is reached (409) | PASS |  |  |
| AB-S032 | Variant A stats: sent 30, unique opened 12 (40%), clicked 3, scheduled 2 | PASS |  |  |
| AB-S033 | Variant B stats: sent 30, opened 6 (20%), replied 2 (6.67%) | PASS |  |  |
| AB-S034 | Winner by open rate is A once both variants have 30 sends | PASS |  |  |
| AB-S040 | A variant that was sent cannot change its subject (409) | PASS |  |  |
| AB-S041 | A variant that was sent cannot be deleted (409) | PASS |  |  |
| AB-S050 | A manager who does not own the campaign cannot promote (403) | PASS |  |  |
| AB-S051 | Promoting a variant that is not the winner is 409 | PASS |  |  |
| AB-S052 | Another workspace cannot promote (404) | PASS |  |  |
| AB-S053 | The owner promotes the winner; the two unsent B emails switch | PASS |  |  |
| AB-S054 | All remaining unsent emails now use the winner's subject | PASS |  |  |
| AB-S055 | Already-sent emails (sent history) are unchanged | PASS |  |  |
| AB-S056 | Stats report the promoted winner | PASS |  |  |
| AB-S057 | After promotion the test is closed: adding a variant is 409 | PASS |  |  |
| AB-S060 | Email list filtered to variant B shows its 30 sent emails with the B subject | PASS |  |  |
| AB-S061 | Email list page size is bounded (limit > 200 is 422) | PASS |  |  |
| AB-S062 | Contact email history carries the subject variant | PASS |  |  |
