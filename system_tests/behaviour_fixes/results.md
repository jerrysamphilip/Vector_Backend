# behaviour_fixes results

PASS 19 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| BF-001 | Stage change notifies the deal owner and their manager, linking to the deal, not the actor | PASS |  |  |
| BF-002 | Deal won notifies owner and manager with the reason | PASS |  |  |
| BF-003 | Owner marking their own deal lost notifies only their manager | PASS |  |  |
| BF-004 | Deal notifications appear in the owner's in-app notifications | PASS |  |  |
| BF-005 | Auto-paused campaign notifies its owner and the workspace admins (not agents) | PASS |  |  |
| BF-006 | The auto-pause notification gives the reason | PASS |  |  |
| BF-007 | Without AI the wizard creates a template for every one of the 3 steps | PASS |  |  |
| BF-008 | The response tells the UI starter templates were used | PASS |  |  |
| BF-009 | Starter templates differ per step and are marked not AI-generated | PASS |  |  |
| BF-010 | Re-inviting an email whose invitation was revoked reissues it (201, same account) | PASS |  |  |
| BF-011 | Re-inviting an invited account that was deactivated works too | PASS |  |  |
| BF-012 | Inviting someone who already joined is still 409 | PASS |  |  |
| BF-013 | Send Now handles at most 25 messages per request and says how many remain | PASS |  |  |
| BF-014 | Continuing with the cursor finishes the rest and stops | PASS |  |  |
| BF-015 | Every queued message was handled exactly once; a malformed cursor is 400 | PASS |  |  |
| BF-016 | Response fields backed by nullable columns are nullable in the OpenAPI | PASS |  |  |
| BF-017 | Picker with prospect_id lists only people who can see the contact | PASS |  |  |
| BF-018 | Someone who cannot see the contact gets 404 from the scoped picker | PASS |  |  |
| BF-019 | Deal note picker lists only people who can see the deal | PASS |  |  |
