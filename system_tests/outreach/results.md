# Outreach (BR-DF / §5.3 / §7) results

PASS 93 · FAIL 7 · BLOCKED 3

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| OUT-001 | Token signed with the public default JWT secret is rejected | PASS |  |  |
| OUT-002 | Wrapped SNS notification with invalid signature is rejected (403) | PASS |  |  |
| OUT-003 | Raw SES event without the webhook token is rejected (403) | PASS |  |  |
| OUT-004 | Raw SES event with a wrong token is rejected (403) | PASS |  |  |
| OUT-005 | Raw SES event with the configured token is accepted (200) | PASS |  |  |
| OUT-009 | Unsigned SubscriptionConfirmation (internal SubscribeURL) is refused, URL not fetched | PASS |  |  |
| OUT-006 | Mailbox SMTP/IMAP passwords encrypted at rest and not returned by the API | PASS |  |  |
| OUT-007 | After 10 wrong passwords the account is locked (429) even with the right password | PASS |  |  |
| OUT-008 | Password-reset requests per email are limited (6th -> 429) | PASS |  |  |
| OUT-102 | Permanent erasure (DELETE /prospects/{id}) removes the contact and logs the deletion | FAIL | defect | DELETE -> 200 {'status': 'deleted', 'prospect_id': '8ec03eec-978d-4e8e-9af3-cd9dc0a13dd0'}; row gone=True; audit_logs+property_changes rows for id=0 (contact_service.delete_contacts also deletes the contact's property_changes; nothing writes audit_logs) |
| OUT-103a | Suppression entry survives erasure (contact cannot be re-emailed) | PASS |  |  |
| OUT-010 | Upload: valid rows accepted, each invalid row rejected with a reason | PASS |  |  |
| OUT-011 | Upload: syntactically invalid email addresses are rejected with a reason | FAIL | defect | accepted as valid: ["row7 'broken-48797b@' -> ACCEPTED", "row8 'has space-48797b@acme-48797b.example' -> ACCEPTED"] (utils/email_utils.py parse_email only checks for '@' and a non-personal domain) |
| OUT-103 | Re-import of an erased, unsubscribed address is rejected (suppression kept) | PASS |  |  |
| OUT-012 | Confirming the upload stores every accepted row in the new list | PASS |  |  |
| OUT-013 | Enrolling the uploaded list enrolls every valid row | PASS |  |  |
| OUT-014 | Enrollment returns a reason for every contact not enrolled | PASS |  |  |
| OUT-089 | Expired voluntary suppression still blocks enrollment (contact stays unsubscribed) | PASS |  |  |
| OUT-024b | Another workspace's contact cannot be enrolled (not_found) | PASS |  |  |
| OUT-015 | Bulk list enrollment into an ACTIVE campaign schedules emails for the new contacts | FAIL | defect | POST /campaigns/{id}/enrollments/bulk -> 200 enrolled=2; email_messages for them=0 (prospect_list_router.enroll_prospects adds CampaignProspect rows but never calls _preschedule_all_emails, unlike CampaignEmailService.enroll_prospects_with_report) |
| OUT-083b | Unsubscribed contact rejected by the bulk list enrollment path | PASS |  |  |
| OUT-016 | Enrolling via the campaign page into an ACTIVE campaign schedules the new contact | PASS |  |  |
| OUT-083c | Unsubscribed contact rejected by the contacts bulk-enroll path | PASS |  |  |
| OUT-020 | Create a new static list and add contacts to it | PASS |  |  |
| OUT-021 | Add contacts to an existing list; re-adding is idempotent | PASS |  |  |
| OUT-022 | Prospects-tab bulk action creates a new list with the selected contacts | PASS |  |  |
| OUT-023 | Contacts can be added to a list created by an upload | PASS |  |  |
| OUT-024 | Another workspace's contacts are not added to a list | PASS |  |  |
| OUT-061 | Database enforces one send per campaign step and address (unique send_key) | PASS |  |  |
| OUT-063 | Contact in two enrolled lists is enrolled once (one email per step) | PASS |  |  |
| OUT-062 | Re-enrolling an enrolled contact is rejected (already_enrolled) and adds no emails | PASS |  |  |
| OUT-064 | Enrolling a new contact after launch adds only that contact's emails | PASS |  |  |
| OUT-110 | Campaign with mailboxes on two domains spreads contacts across both | PASS |  |  |
| OUT-080 | Unsubscribe link (GET) shows a confirmation page and changes nothing | PASS |  |  |
| OUT-081 | Confirmed unsubscribe: suppressed workspace-wide, pending emails in every campaign cancelled | PASS |  |  |
| OUT-105 | Consent change is recorded with source and timestamp | PASS |  |  |
| OUT-083 | Unsubscribed contact is rejected when enrolled in a new campaign (campaign page) | PASS |  |  |
| OUT-082 | One-click List-Unsubscribe POST unsubscribes | PASS |  |  |
| OUT-091 | Unsubscribe for an unknown message id returns 404 | PASS |  |  |
| OUT-090 | Unsubscribe in one workspace does not unsubscribe the same address in another | PASS |  |  |
| OUT-085 | A contact whose address matches a suppression (any case) cannot be emailed | PASS |  |  |
| OUT-086 | Manual unsubscribe on the contact record suppresses and cancels pending emails | PASS |  |  |
| OUT-087 | Creating a contact whose address is suppressed marks it UNSUBSCRIBED | PASS |  |  |
| OUT-100 | Another user cannot create a contact with an existing email (any case) | PASS |  |  |
| OUT-113 | An AGENT cannot launch a campaign | PASS |  |  |
| OUT-101 | Deleting a contact on request is logged, listed in Recently deleted, and cancels pending emails | PASS |  |  |
| OUT-104 | Legal basis for processing is stored; invalid values are refused | PASS |  |  |
| OUT-111 | Launch is refused with a reason when the sender postal address is missing (CAN-SPAM) | PASS |  |  |
| OUT-112 | Launch validation: an active campaign can't be relaunched; a campaign without steps can't launch | PASS |  |  |
| OUT-114 | AI email generation returns a personalised subject and body | PASS |  |  |
| OUT-114b | AI generation for another workspace's contact is refused (404) | PASS |  |  |
| OUT-115 | Sequence generation (no LLM) returns a 3-email sequence | PASS |  |  |
| OUT-116 | Compliance check flags spam trigger words | PASS |  |  |
| OUT-122 | SES quota endpoint answers with a clear error (not 500) when SES is unreachable | PASS |  |  |
| OUT-050 | Microsoft 365 OAuth connection: status reported, start gives a clear message when not set up | PASS |  |  |
| OUT-070 | 4 hard bounces in the first sends (below the 5-bounce limit) do not pause the campaign | PASS |  |  |
| OUT-071 | 5th hard bounce auto-pauses the campaign within 1 minute, with the reason, queue frozen | PASS |  |  |
| OUT-073 | Domain health is recomputed at the risk event (24h sends / bounce rate stored) | PASS |  |  |
| OUT-076 | Another workspace's campaign on the same sending domain is not paused by this workspace's bounces | PASS |  |  |
| OUT-077 | Domain list does not show another workspace's mailboxes or campaigns | PASS |  |  |
| OUT-075 | After the user resumes an auto-paused campaign it is judged on new sends (one bounce doesn't re-pause) | PASS |  |  |
| OUT-072 | Two spam complaints auto-pause the campaign within 1 minute | PASS |  |  |
| OUT-074 | Every domain that sent in the last 24h had its health recomputed within the last hour | PASS |  |  |
| OUT-078 | Pause freezes pending emails (PAUSED_BY_CAMPAIGN); resume releases them | PASS |  |  |
| OUT-033 | Batch mode: batches of 2, 30 minutes apart | PASS |  |  |
| OUT-030 | 500-contact campaign: all 500 enrolled and scheduled at launch (no stall near 330) | PASS |  |  |
| OUT-031 | Configured pace (100 new contacts/day) spreads 500 contacts over 5 business days | PASS |  |  |
| OUT-032 | Launching the 500-contact campaign completes within 30 seconds | PASS |  |  |
| OUT-036 | Inbox pace (60 s between emails) can reach the 500/day cap within a day | PASS |  |  |
| OUT-051 | IMAP connection test succeeds against a reachable mailbox | PASS |  |  |
| OUT-052 | Prospect reply is fetched and shown in the unified inbox thread | PASS |  |  |
| OUT-053 | Reply stops the sequence: enrollment REPLIED, follow-up cancelled | PASS |  |  |
| OUT-054 | Out-of-office reply is visible but does not stop the sequence | PASS |  |  |
| OUT-055 | Reply 'unsubscribe' unsubscribes the contact from every campaign | FAIL | defect | consent=OPT_IN |
| OUT-059 | Mail from another workspace's contact is not filed into either workspace via this mailbox | PASS |  |  |
| OUT-121 | Reply from the unified inbox creates an outbound message in the thread | PASS |  |  |
| OUT-056 | Mailbox sign-in failure is reported (sync fails with a reason stored on the inbox) | PASS |  |  |
| OUT-040 | Delivery event sets final status DELIVERED | PASS |  |  |
| OUT-041 | Delivery event without our tag is matched by the SES message id | PASS |  |  |
| OUT-042 | A redelivered (duplicate) SES event is processed once | FAIL | defect | first=(200, {'status': 'processed', 'type': 'delivery', 'emails': ['evt2-48797b@acme-48797b.example']}) second=(200, {'status': 'duplicate', 'type': 'Delivery'}) delivered_events=0 |
| OUT-043 | Hard bounce after delivery: final status BOUNCED, address suppressed | PASS |  |  |
| OUT-044 | Soft (transient) bounce does not suppress the contact or change the message status | PASS |  |  |
| OUT-045 | Complaint: final status COMPLAINED, contact unsubscribed and suppressed | PASS |  |  |
| OUT-048 | Event for an unknown message is accepted without side effects | PASS |  |  |
| OUT-046 | Sent email with no SES outcome after 15 minutes gets final status UNCONFIRMED (worker) | PASS |  |  |
| OUT-047 | A late delivery event overrides UNCONFIRMED | PASS |  |  |
| OUT-117 | Open pixel returns an image and records an open event | PASS |  |  |
| OUT-118a | Click tracking does not redirect to an unrecognised destination (no open redirect) | PASS |  |  |
| OUT-118b | Click on a link that is in the email is recorded and redirected (302) | PASS |  |  |
| OUT-119 | Campaign analytics reflect sent emails and events | FAIL | defect | 200 total_sent=5 metrics={"sent_count": 5, "opened_count": 1, "clicked_count": 1, "replied_count": 0, "positive_replied_count": 0, "ooo_count": 0, "bounced_count": 1, "sender_bounced_count": 1, "unsubscribed_count": 1, "open_ |
| OUT-120 | Prospect report export for a campaign returns a file with the campaign's contacts | PASS |  |  |
| OUT-123 | Campaign audit trail records the launch | PASS |  |  |
| OUT-130 | Owner signs in; campaigns, inboxes, unified inbox, prospects, lists, domain-health render without errors | PASS |  |  |
| OUT-132 | Launched campaign detail page renders with the campaign name | FAIL | defect | {} |
| OUT-131 | Recipient unsubscribes in the browser (confirm page -> success) and is suppressed | PASS |  |  |
| OUT-058 | After a manual 'mark replied' the scheduler cancels the due follow-up | PASS |  |  |
| OUT-088 | Send-time gate: a suppression added out-of-band stops the email | PASS |  |  |
| OUT-060 | A second email for an already-sent step to the same address is cancelled, not sent | PASS |  |  |
| OUT-035 | Due email outside the campaign send window is held and re-queued, not sent | PASS |  |  |
| OUT-034 | Inbox daily cap reached: email re-queued for later | BLOCKED | blocked | no time zone is on a US business day right now (weekend/US holiday); the scheduler's business-day send-window guard holds every send, so this path can't be reached; observed ['QUEUED', ''] |
| OUT-037 | Personal-address recipient blocked before SES with a reason | BLOCKED | blocked | no time zone is on a US business day right now (weekend/US holiday); the scheduler's business-day send-window guard holds every send, so this path can't be reached; observed ['QUEUED', ''] |
| OUT-049 | SES unavailable: retried with backoff, then FAILED with a final status | BLOCKED | blocked | no time zone is on a US business day right now (weekend/US holiday); the scheduler's business-day send-window guard holds every send, so this path can't be reached; observed ['QUEUED', ''] |
| OUT-057 | Worker syncs a reply into the inbox within 10 minutes without a manual sync | PASS |  |  |
