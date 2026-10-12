# Outreach, defect fixes & compliance — test cases

BRD scope: §5.1 existing-system defect fixes (BR-DF-*), §5.3 existing outreach features (campaigns, AI emails, sending, inbox), §7 data & compliance.

Generated from the automated suite `run_outreach.py` (each case is automated; preconditions, steps and expected results are in the function that records the case ID). Status column = latest run.

| ID | Test case (expected result) | Type | Latest |
|---|---|---|---|
| OUT-001 | Token signed with the public default JWT secret is rejected | System | PASS |
| OUT-002 | Wrapped SNS notification with invalid signature is rejected (403) | System | PASS |
| OUT-003 | Raw SES event without the webhook token is rejected (403) | Integration | PASS |
| OUT-004 | Raw SES event with a wrong token is rejected (403) | Integration | PASS |
| OUT-005 | Raw SES event with the configured token is accepted (200) | Integration | PASS |
| OUT-006 | Mailbox SMTP/IMAP passwords encrypted at rest and not returned by the API | Integration | PASS |
| OUT-007 | After 10 wrong passwords the account is locked (429) even with the right password | System | PASS |
| OUT-008 | Password-reset requests per email are limited (6th -> 429) | System | PASS |
| OUT-009 | Unsigned SubscriptionConfirmation (internal SubscribeURL) is refused, URL not fetched | System | PASS |
| OUT-010 | Upload: valid rows accepted, each invalid row rejected with a reason | System | PASS |
| OUT-011 | Upload: syntactically invalid email addresses are rejected with a reason | Integration | FAIL |
| OUT-012 | Confirming the upload stores every accepted row in the new list | System | PASS |
| OUT-013 | Enrolling the uploaded list enrolls every valid row | System | PASS |
| OUT-014 | Enrollment returns a reason for every contact not enrolled | System | PASS |
| OUT-015 | Bulk list enrollment into an ACTIVE campaign schedules emails for the new contacts | System | FAIL |
| OUT-016 | Enrolling via the campaign page into an ACTIVE campaign schedules the new contact | System | PASS |
| OUT-020 | Create a new static list and add contacts to it | System | PASS |
| OUT-021 | Add contacts to an existing list; re-adding is idempotent | System | PASS |
| OUT-022 | Prospects-tab bulk action creates a new list with the selected contacts | System | PASS |
| OUT-023 | Contacts can be added to a list created by an upload | System | PASS |
| OUT-024 | Another workspace's contacts are not added to a list | System | PASS |
| OUT-024b | Another workspace's contact cannot be enrolled (not_found) | System | PASS |
| OUT-030 | 500-contact campaign: all 500 enrolled and scheduled at launch (no stall near 330) | System | PASS |
| OUT-031 | Configured pace (100 new contacts/day) spreads 500 contacts over 5 business days | System | PASS |
| OUT-032 | Launching the 500-contact campaign completes within 30 seconds | System | PASS |
| OUT-033 | Batch mode: batches of 2, 30 minutes apart | System | PASS |
| OUT-034 | Inbox daily cap reached: email re-queued for later | System | BLOCKED |
| OUT-035 | Due email outside the campaign send window is held and re-queued, not sent | System | PASS |
| OUT-036 | Inbox pace (60 s between emails) can reach the 500/day cap within a day | System | PASS |
| OUT-037 | Personal-address recipient blocked before SES with a reason | Integration | BLOCKED |
| OUT-040 | Delivery event sets final status DELIVERED | System | PASS |
| OUT-041 | Delivery event without our tag is matched by the SES message id | Integration | PASS |
| OUT-042 | A redelivered (duplicate) SES event is processed once | Integration | FAIL |
| OUT-043 | Hard bounce after delivery: final status BOUNCED, address suppressed | Integration | PASS |
| OUT-044 | Soft (transient) bounce does not suppress the contact or change the message status | Integration | PASS |
| OUT-045 | Complaint: final status COMPLAINED, contact unsubscribed and suppressed | System | PASS |
| OUT-046 | Sent email with no SES outcome after 15 minutes gets final status UNCONFIRMED (worker) | Integration | PASS |
| OUT-047 | A late delivery event overrides UNCONFIRMED | System | PASS |
| OUT-048 | Event for an unknown message is accepted without side effects | System | PASS |
| OUT-049 | SES unavailable: retried with backoff, then FAILED with a final status | Integration | BLOCKED |
| OUT-050 | Microsoft 365 OAuth connection: status reported, start gives a clear message when not set up | System | PASS |
| OUT-051 | IMAP connection test succeeds against a reachable mailbox | Integration | PASS |
| OUT-052 | Prospect reply is fetched and shown in the unified inbox thread | System | PASS |
| OUT-053 | Reply stops the sequence: enrollment REPLIED, follow-up cancelled | System | PASS |
| OUT-054 | Out-of-office reply is visible but does not stop the sequence | System | PASS |
| OUT-055 | Reply 'unsubscribe' unsubscribes the contact from every campaign | System | FAIL |
| OUT-056 | Mailbox sign-in failure is reported (sync fails with a reason stored on the inbox) | Integration | PASS |
| OUT-057 | Worker syncs a reply into the inbox within 10 minutes without a manual sync | Integration | PASS |
| OUT-058 | After a manual 'mark replied' the scheduler cancels the due follow-up | Integration | PASS |
| OUT-059 | Mail from another workspace's contact is not filed into either workspace via this mailbox | System | PASS |
| OUT-060 | A second email for an already-sent step to the same address is cancelled, not sent | System | PASS |
| OUT-061 | Database enforces one send per campaign step and address (unique send_key) | System | PASS |
| OUT-062 | Re-enrolling an enrolled contact is rejected (already_enrolled) and adds no emails | System | PASS |
| OUT-063 | Contact in two enrolled lists is enrolled once (one email per step) | System | PASS |
| OUT-064 | Enrolling a new contact after launch adds only that contact's emails | System | PASS |
| OUT-070 | 4 hard bounces in the first sends (below the 5-bounce limit) do not pause the campaign | Integration | PASS |
| OUT-071 | 5th hard bounce auto-pauses the campaign within 1 minute, with the reason, queue frozen | Integration | PASS |
| OUT-072 | Two spam complaints auto-pause the campaign within 1 minute | Integration | PASS |
| OUT-073 | Domain health is recomputed at the risk event (24h sends / bounce rate stored) | Integration | PASS |
| OUT-074 | Every domain that sent in the last 24h had its health recomputed within the last hour | System | PASS |
| OUT-075 | After the user resumes an auto-paused campaign it is judged on new sends (one bounce doesn't re-pause) | Integration | PASS |
| OUT-076 | Another workspace's campaign on the same sending domain is not paused by this workspace's bounces | Integration | PASS |
| OUT-077 | Domain list does not show another workspace's mailboxes or campaigns | System | PASS |
| OUT-078 | Pause freezes pending emails (PAUSED_BY_CAMPAIGN); resume releases them | Integration | PASS |
| OUT-080 | Unsubscribe link (GET) shows a confirmation page and changes nothing | System | PASS |
| OUT-081 | Confirmed unsubscribe: suppressed workspace-wide, pending emails in every campaign cancelled | System | PASS |
| OUT-082 | One-click List-Unsubscribe POST unsubscribes | System | PASS |
| OUT-083 | Unsubscribed contact is rejected when enrolled in a new campaign (campaign page) | System | PASS |
| OUT-083b | Unsubscribed contact rejected by the bulk list enrollment path | System | PASS |
| OUT-083c | Unsubscribed contact rejected by the contacts bulk-enroll path | System | PASS |
| OUT-085 | A contact whose address matches a suppression (any case) cannot be emailed | System | PASS |
| OUT-086 | Manual unsubscribe on the contact record suppresses and cancels pending emails | Integration | PASS |
| OUT-087 | Creating a contact whose address is suppressed marks it UNSUBSCRIBED | System | PASS |
| OUT-088 | Send-time gate: a suppression added out-of-band stops the email | System | PASS |
| OUT-089 | Expired voluntary suppression still blocks enrollment (contact stays unsubscribed) | System | PASS |
| OUT-090 | Unsubscribe in one workspace does not unsubscribe the same address in another | System | PASS |
| OUT-091 | Unsubscribe for an unknown message id returns 404 | System | PASS |
| OUT-100 | Another user cannot create a contact with an existing email (any case) | System | PASS |
| OUT-101 | Deleting a contact on request is logged, listed in Recently deleted, and cancels pending emails | System | PASS |
| OUT-102 | Permanent erasure (DELETE /prospects/{id}) removes the contact and logs the deletion | System | FAIL |
| OUT-103 | Re-import of an erased, unsubscribed address is rejected (suppression kept) | System | PASS |
| OUT-103a | Suppression entry survives erasure (contact cannot be re-emailed) | System | PASS |
| OUT-104 | Legal basis for processing is stored; invalid values are refused | System | PASS |
| OUT-105 | Consent change is recorded with source and timestamp | System | PASS |
| OUT-110 | Campaign with mailboxes on two domains spreads contacts across both | System | PASS |
| OUT-111 | Launch is refused with a reason when the sender postal address is missing (CAN-SPAM) | System | PASS |
| OUT-112 | Launch validation: an active campaign can't be relaunched; a campaign without steps can't launch | System | PASS |
| OUT-113 | An AGENT cannot launch a campaign | System | PASS |
| OUT-114 | AI email generation returns a personalised subject and body | System | PASS |
| OUT-114b | AI generation for another workspace's contact is refused (404) | System | PASS |
| OUT-115 | Sequence generation (no LLM) returns a 3-email sequence | System | PASS |
| OUT-116 | Compliance check flags spam trigger words | System | PASS |
| OUT-117 | Open pixel returns an image and records an open event | System | PASS |
| OUT-118a | Click tracking does not redirect to an unrecognised destination (no open redirect) | System | PASS |
| OUT-118b | Click on a link that is in the email is recorded and redirected (302) | System | PASS |
| OUT-119 | Campaign analytics reflect sent emails and events | System | FAIL |
| OUT-120 | Prospect report export for a campaign returns a file with the campaign's contacts | System | PASS |
| OUT-121 | Reply from the unified inbox creates an outbound message in the thread | System | PASS |
| OUT-122 | SES quota endpoint answers with a clear error (not 500) when SES is unreachable | Integration | PASS |
| OUT-123 | Campaign audit trail records the launch | System | PASS |
| OUT-130 | Owner signs in; campaigns, inboxes, unified inbox, prospects, lists, domain-health render without errors | System (UI) | PASS |
| OUT-131 | Recipient unsubscribes in the browser (confirm page -> success) and is suppressed | System (UI) | PASS |
| OUT-132 | Launched campaign detail page renders with the campaign name | System (UI) | FAIL |
