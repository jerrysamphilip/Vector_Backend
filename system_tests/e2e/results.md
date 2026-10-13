# E2E journeys (real worker + SES stand-in + fake IMAP) results

PASS 187 · FAIL 0 · BLOCKED 1

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| E2E-J1-01 | Self sign-up creates a workspace; the owner is SUPER_ADMIN of a fresh tenant | PASS |  |  |
| E2E-J1-02 | Owner builds the hierarchy (owner L1 -> manager L2 -> two agents L3) | PASS |  |  |
| E2E-J1-03 | Sender identity (company + postal address) saved and read back complete | PASS |  |  |
| E2E-J1-04 | Two mailboxes connected with IMAP settings; secrets encrypted at rest and never returned | PASS |  |  |
| E2E-J1-05 | IMAP connection test: the API container really logs in to the (fake) mailbox | PASS |  |  |
| E2E-J1-06 | Mailbox domain was registered as an SES identity (domain-verification flow reached SES) | PASS |  |  |
| E2E-J1-07 | CSV validation: 21 rows -> 16 accepted, 5 rejected, each with its specific reason | PASS |  |  |
| E2E-J1-08 | Confirming the upload creates the list with exactly the 16 accepted contacts | PASS |  |  |
| E2E-J1-09 | Companies from the CSV exist as accounts (15 distinct names; Ann+Bo share 'Alpha Corp') | PASS |  |  |
| E2E-J1-10 | All 16 contacts are readable via the contacts API with names/companies from the CSV | PASS |  |  |
| E2E-J1-11 | Campaign created in DRAFT with both mailboxes, 24h UTC window, sender name/title | PASS |  |  |
| E2E-J1-12 | 3-step sequence saved with wait days 0 / 2 / 3 | PASS |  |  |
| E2E-J1-13 | Wizard 'generate templates' works without an LLM (fallback template per persona) | PASS | defect |  |
| E2E-J1-14 | Fallback step-1 template carries personalisation tokens and no unresolved internal placeholders | PASS |  |  |
| E2E-J1-15 | Fallback gives a usable 3-step sequence (a template for EVERY step) | PASS | defect |  |
| E2E-J1-16 | Enrolment: 16 enrolled, 0 rejected (wizard step 'launch' part 1) | PASS |  |  |
| E2E-J1-17 | Enrolling the same list again is idempotent: nobody enrolled twice | PASS |  |  |
| E2E-J1-18 | Every step already has a template, so no follow-up template has to be added by hand | PASS |  |  |
| E2E-J1-19 | Launch with the fallback templates (none added by hand) -> ACTIVE | PASS |  |  |
| E2E-J1-20 | 48 messages pre-scheduled (16 contacts x 3 steps), all SCHEDULED, inboxes alternate 8 / 8 | PASS |  |  |
| E2E-J1-21 | Step-1 subject/body are rendered per contact: first name + company substituted, no '{{' left | PASS |  |  |
| E2E-J1-22 | Pre-scheduling places step 1 on a business day (not weekend) and not before launch | PASS |  |  |
| E2E-J1-23 | Planned follow-ups are spaced by the configured wait days (step2 >= step1+2d, step3 >= step2+3d) | PASS |  |  |
| E2E-J1-24 | The worker sends all 16 step-1 emails through SES (captured by the stand-in), none to rejected rows | PASS |  |  |
| E2E-J1-25 | Exactly one email per eligible contact: no duplicates, count == eligible prospects (16) | PASS |  |  |
| E2E-J1-26 | DB <-> SES correlation: ses_message_id == SES id, provider_message_id == MIME Message-ID, per recipient | PASS |  |  |
| E2E-J1-27 | Per-inbox pacing: 8 per inbox, consecutive sends >= ~delay_between_emails (4s, jitter <= 25%) | PASS |  |  |
| E2E-J1-28 | Daily-limit counters on the mailboxes reflect the sends (8 + 8) | PASS |  |  |
| E2E-J1-29 | MIME headers: From=mailbox, Reply-To, List-Unsubscribe (URL with message id + mailto), List-Unsubscribe-Post one-click, Message-ID on the sender domain | PASS |  |  |
| E2E-J1-30 | Body: tenant's postal address exactly once (CAN-SPAM footer), contact first name + company resolved, no placeholders | PASS |  |  |
| E2E-J1-31 | Signature: single sign-off using the campaign's sender name; footer not followed by a second signature | PASS | defect |  |
| E2E-J1-32 | Every email carries a unique unsubscribe URL built from BASE_URL (/api/tracking/unsubscribe/<message id>) | PASS |  |  |
| E2E-J1-33 | Unsubscribe_mode 'plain': body explains how to unsubscribe (reply) and the link lives in List-Unsubscribe | PASS |  |  |
| E2E-J1-34 | Campaign counters after the send: sent_count == 16 | PASS |  |  |
| E2E-J1-35 | Delivery events for 15 messages accepted ('processed'); an immediate replay of each is 'duplicate' | PASS |  |  |
| E2E-J1-36 | Open (10 contacts) and Click (4 contacts, 5 links) events processed once; replays are 'duplicate' | PASS |  |  |
| E2E-J1-37 | Hard bounce event processed; replay ignored | PASS |  |  |
| E2E-J1-38 | Event rows exactly match the posts: 15 delivered, 11 open rows (10 contacts + 1 re-open), 5 click rows, 1 bounce | PASS |  |  |
| E2E-J1-39 | Message final_status: 15 DELIVERED, 1 BOUNCED (BR-DF-04) | PASS |  |  |
| E2E-J1-40 | Hard bounce: contact suppressed, enrolment BOUNCED, steps 2+3 not sendable; campaign (1/16) NOT auto-paused | PASS |  |  |
| E2E-J1-41 | Campaign detail metrics: sent 16, opened 10 (unique contacts), bounced 1 | PASS |  |  |
| E2E-J1-42 | GET /campaigns/{id}/metrics agrees with the campaign detail (opened 10, bounced 1, open rate 10/16) | PASS |  |  |
| E2E-J1-43 | Analytics step 1: sent 16, opened 10, bounced 1, open rate 62.5, click rate 40.0 (4 of 10 openers) | PASS |  |  |
| E2E-J1-44 | Dashboard summary (tenant total) matches: 16 sent, 10 opened, 4 clicked, 1 bounced, 16 contacted | PASS |  |  |
| E2E-J1-45 | Global analytics KPIs equal the dashboard summary | PASS |  |  |
| E2E-J1-46 | Campaign comparison row agrees (sent 16, opened 10, bounced 1) | PASS |  |  |
| E2E-J1-47 | Analytics time-series for today: sent 16 / opened 10 / bounced 1 | PASS |  |  |
| E2E-J1-48 | GET on the real unsubscribe link shows a confirmation page and changes nothing (link scanners are safe) | PASS |  |  |
| E2E-J1-49 | POST (the page's button) unsubscribes: suppression row, consent UNSUBSCRIBED, enrolment UNSUBSCRIBED | PASS |  |  |
| E2E-J1-50 | RFC 8058 one-click POST ('List-Unsubscribe=One-Click') unsubscribes without a confirmation step | PASS |  |  |
| E2E-J1-51 | Unsubscribed contact (unsub_link): steps 2 and 3 are CANCELLED, nothing left pending | PASS |  |  |
| E2E-J1-52 | Unsubscribed contact (unsub_oneclick): steps 2 and 3 are CANCELLED, nothing left pending | PASS |  |  |
| E2E-J1-53 | Unsubscribe is an event on the original message and counted in the campaign metrics (2) | PASS |  |  |
| E2E-J1-54 | Re-enrolling an unsubscribed contact is refused with an 'unsubscribed' reason | PASS |  |  |
| E2E-J1-55 | Uploading the unsubscribed address again is rejected as unsubscribed | PASS |  |  |
| E2E-J1-56 | Worker sends nothing further to unsubscribed contacts even when their follow-up is forced due (only the step-1 email exists) | PASS |  |  |
| E2E-J1-57 | Contact timeline records the unsubscribe | PASS |  |  |
| E2E-J1-58 | IMAP sync ingests the reply: one INBOUND message with the exact body, from the contact to the mailbox | PASS |  |  |
| E2E-J1-59 | Reply is threaded into the same conversation as the outbound step-1 email (matched by In-Reply-To) | PASS |  |  |
| E2E-J1-60 | Unified inbox lists the conversation, unread, last message INBOUND | PASS |  |  |
| E2E-J1-61 | Reply stops that contact's sequence: enrolment REPLIED, steps 2+3 CANCELLED ('contact replied') | PASS |  |  |
| E2E-J1-62 | Out-of-office auto-reply is stored but does NOT stop the sequence (still ACTIVE, steps 2+3 pending) | PASS |  |  |
| E2E-J1-63 | Reply 'unsubscribe' suppresses the contact workspace-wide and stops the sequence | PASS |  |  |
| E2E-J1-64 | Mail from an unknown sender is not turned into a prospect/conversation of this workspace | PASS |  |  |
| E2E-J1-65 | Reply analytics: 3 reply events (1 genuine, 1 out-of-office, 1 unsubscribe), 1 OOO; campaign counters match | PASS |  |  |
| E2E-J1-66 | Step-1 analytics and dashboard summary show the same reply numbers | PASS |  |  |
| E2E-J1-67 | Positive-reply classification for 'looks interesting... pricing for 40 seats' | BLOCKED | environment | OPENAI_API_KEY is not set: classify_reply_intent() returns None, so no POSITIVE_REPLY event (0) and /leads/reply-suggestions is 0 item(s). The app has no keyword fallback; the reply is ingested normally. |
| E2E-J1-68 | Without an LLM the reply is still ingested and the rest of the chain is unaffected (no POSITIVE event is invented) | PASS |  |  |
| E2E-J1-69 | Worker's background IMAP loop (no sync API call) logs in and ingests the reply within 10 minutes (BR-DF-05) | PASS |  |  |
| E2E-J1-70 | Background-ingested reply also stops the sequence (enrolment REPLIED) | PASS |  |  |
| E2E-J1-71 | Manager turns the reply into a lead owned by agent 1 (one click): ENGAGED, source 'Campaign reply', campaign attached | PASS |  |  |
| E2E-J1-72 | Second lead for the same open contact is refused (409 with the existing lead id) | PASS |  |  |
| E2E-J1-73 | Lead visibility follows the hierarchy: owner/manager/agent1 see it, agent2 gets 404 | PASS |  |  |
| E2E-J1-74 | Agent 1 is notified of the assignment ('New lead: <contact>') | PASS |  |  |
| E2E-J1-75 | Cannot mark SQL before BANT is confirmed (400 lists the missing criteria) | PASS |  |  |
| E2E-J1-76 | After BANT the agent qualifies the lead: stage SQL with qualified_at set | PASS |  |  |
| E2E-J1-77 | SQL queue: manager and agent 1 see the lead, agent 2 does not | PASS |  |  |
| E2E-J1-78 | Convert SQL -> opportunity: amount 24000, close date, owner agent 1, first open stage, attributed to the campaign | PASS |  |  |
| E2E-J1-79 | Lead becomes CONVERTED linked to the deal; contact lifecycle moves to OPPORTUNITY | PASS |  |  |
| E2E-J1-80 | Deal moves Qualification -> Needs analysis -> Proposal (amount 30000) -> Negotiation (new close date) | PASS |  |  |
| E2E-J1-81 | Deal history records stage_id x3, amount and close_date changes with the author's name | PASS |  |  |
| E2E-J1-82 | Closing as won without a reason is refused (400) | PASS |  |  |
| E2E-J1-83 | Won with a reason: status WON, closed_at set, reason stored, probability 100 | PASS |  |  |
| E2E-J1-84 | Contact lifecycle is CUSTOMER after the win | PASS |  |  |
| E2E-J1-85 | Contact history shows lifecycle changes through the funnel (MQL -> SQL -> Opportunity -> Customer) | PASS |  |  |
| E2E-J1-86 | Deal timeline is available to its owner and not to agent 2 | PASS |  |  |
| E2E-J1-87 | Agent 1 holds the assignment notification created on the way (ASSIGNED 'New lead: Ed Egan') | PASS |  |  |
| E2E-J1-87b | Notifications for the deal moving to Won / for the manager (stage changes, win) | PASS | not-implemented |  |
| E2E-J1-88 | Pipeline revenue totals = independently computed open amount 55000 / weighted 17400 (+ won excluded) | PASS |  |  |
| E2E-J1-89 | Forecast by FY quarter (April-March) matches independently bucketed deals incl. boundary dates (Apr 1, Mar 31, Sep 30, Oct 1) | PASS |  |  |
| E2E-J1-90 | A deal closing 1 April next year lands in the next financial year (5000), not this one | PASS |  |  |
| E2E-J1-91 | Team performance: agent1 won 1 deal / 30000, 1 lead, 1 SQL, 1 deal created+converted, open pipeline 30000; agent2 lost 1, open 25000 | PASS |  |  |
| E2E-J1-92 | Leaderboard ranks agent 1 first (30000 won) ahead of agent 2 (0) | PASS |  |  |
| E2E-J1-93 | Sales dashboard KPIs: won 1 / 30000, lost 1, open pipeline 55000, weighted 17400, new leads 1, SQLs 1 | PASS |  |  |
| E2E-J1-94 | Scoping: manager sees the team's numbers (same as owner), agent 2 sees only own (open 25000, won 0) | PASS |  |  |
| E2E-J1-95 | Amount hiding for AGENT role: deal, list, forecast, dashboard and team scorecard show no amounts; writing one is 403; manager still sees 30000 | PASS |  |  |
| E2E-J1-96 | B01: every live contact's step 2 is scheduled >= step-1 SENT time + 2 days (spacing counts from the real send) | PASS |  |  |
| E2E-J1-97 | No follow-up is pending for dead contacts (unsubscribed / replied / bounced): 0 SCHEDULED rows | PASS |  |  |
| E2E-J1-98 | Step 2 goes only to the live contacts (13 minus the held one = 8): none to dead contacts, none to the contact whose step 1 is unsent | PASS |  |  |
| E2E-J1-99 | Order and spacing: each step 2 was sent after its step 1, and step 3 is re-planned >= step-2 sent + 3 days | PASS |  |  |
| E2E-J1-100 | Follow-up email content: its own step-2 template ('Following up ...'), contact personalised, footer + unsubscribe header present | PASS |  |  |
| E2E-J1-101 | Paused campaign: step 3 forced due, yet nothing is sent while PAUSED | PASS |  |  |
| E2E-J1-101a | Pause answers promptly (< 20 s) while the scheduler is working the campaign | PASS | defect |  |
| E2E-J1-102 | Resume does not burst: overdue step-3 messages are re-spread from now, one per slot per mailbox lane (>= campaign min gap of 120 s apart) | PASS |  |  |
| E2E-J1-103 | After resume every step-3 email goes out exactly once, spread over time (not a burst) | PASS |  |  |
| E2E-J1-104 | GDPR erase (DELETE /prospects/{id}) removes the contact and every row keyed by prospect_id | PASS |  |  |
| E2E-J1-105 | Erased contact: no email events, no messages/conversations with their address remain | PASS |  |  |
| E2E-J1-106 | Linked lead and deal follow the erasure (gone, or no longer reference the contact) | PASS |  |  |
| E2E-J1-107 | An audit row records the erasure | PASS |  |  |
| E2E-J1-108 | Suppression list survives the erasure of another contact (unsubscribed x2 and bounced addresses still blocked) | PASS |  |  |
| E2E-J2-01 | Two tenants send from the same domain concurrently: 12 + 4 emails delivered to SES, each on its own tenant | PASS |  |  |
| E2E-J2-02 | 4 hard bounces in 12 sends (< 5): campaign stays ACTIVE | PASS |  |  |
| E2E-J2-03 | A soft (Transient) bounce is recorded but does NOT suppress, fail the message, stop the contact or count towards auto-pause | PASS |  |  |
| E2E-J2-04 | Domain health row (tenant D): 12 sends in 24h, 4 hard bounces = 33.3% bounce rate, reputation score fell | PASS |  |  |
| E2E-J2-05 | 5th hard bounce auto-pauses the campaign: PAUSED, auto_paused, reason names '5 hard bounces in the first 12 sends' | PASS |  |  |
| E2E-J2-06 | Tenant E on the SAME domain is unaffected: campaign ACTIVE, own domain row with 4 sends / 0% bounce; D's row shows its own 12 / 41.7% | PASS |  |  |
| E2E-J2-07 | Suppressions created for D's 5 hard-bounced addresses only (not the soft one), none for tenant E | PASS |  |  |
| E2E-J2-08 | Alert raised for tenant D (auto-pause) after the deliverability job; tenant E sees none of it | PASS |  |  |
| E2E-J2-09 | Owner is notified that the campaign was auto-paused (notification centre) | PASS | not-implemented |  |
| E2E-J2-10 | Resuming an auto-paused campaign works (ACTIVE) and a baseline stops the old bounces re-pausing it at once | PASS |  |  |
| E2E-J2-11 | 1 complaint in 10 sends (< 2): contact suppressed + message COMPLAINED, campaign still ACTIVE | PASS |  |  |
| E2E-J2-12 | 2nd complaint auto-pauses ('2 spam complaints in the first 10 sends') | PASS |  |  |
| E2E-J2-13 | Non-raw SNS envelope ({Message: json}) with the token is accepted and processed | PASS |  |  |
| E2E-J2-14 | 54 sends, 2 hard bounces (3.7% < 5%): still ACTIVE (the '5 bounces' rule no longer applies, the rate does) | PASS |  |  |
| E2E-J2-15 | 3rd hard bounce = 5.6% >= 5% of 54 sends: auto-paused with the rate in the reason | PASS |  |  |
| E2E-J2-16 | Resume of the rate-paused campaign works; its health is then judged on sends AFTER the resume (baseline), so the old bounces do not re-pause it | PASS |  |  |
| E2E-J2-16b | A complaint about an email sent BEFORE the resume does not count against the resumed campaign (stays ACTIVE), but the contact is suppressed | PASS |  |  |
| E2E-J2-17 | Hourly health cycle (send_safety) leaves tenant E's campaign ACTIVE and keeps D's paused campaigns paused | PASS |  |  |
| E2E-J3-01 | Register: owner is SUPER_ADMIN, ACTIVE, MFA off | PASS |  |  |
| E2E-J3-02 | MFA enrolment: wrong code rejected (401), correct TOTP enables it and returns 10 one-time recovery codes | PASS |  |  |
| E2E-J3-03 | After logout, password login does NOT give a session: it asks for the second factor (mfa_token, no access token) | PASS |  |  |
| E2E-J3-04 | MFA verify: wrong code 401; right TOTP code issues access + refresh tokens usable on /me | PASS |  |  |
| E2E-J3-05 | Recovery code signs in once; the same code is refused the second time | PASS |  |  |
| E2E-J3-06 | Invite creates an INVITED user and sends a real email (captured in SES) with a magic link + temporary password; the API response does not leak either | PASS |  |  |
| E2E-J3-07 | Invitee follows the emailed magic link: status INVITED -> ACTIVE, session issued; the link works once only | PASS |  |  |
| E2E-J3-08 | Invitee sets their own password using the emailed temporary one; new password logs in, temporary no longer does | PASS |  |  |
| E2E-J3-09 | Invitee lands in the inviter's tenant with the invited role | PASS |  |  |
| E2E-J3-10 | Forgot-password: real email with a reset link is captured; unknown address gets the same generic 200 and no email | PASS |  |  |
| E2E-J3-11 | Reset: new password works, old password rejected, link is single-use, refresh token issued before the reset is revoked | PASS |  |  |
| E2E-J3-12 | Role change takes effect immediately on the user's existing token (AGENT cannot list users, ADMIN can) | PASS |  |  |
| E2E-J3-13 | Deactivation is immediate: the user's still-unexpired access token is rejected, login refused | PASS |  |  |
| E2E-J3-14 | Reactivation restores sign-in | PASS |  |  |
| E2E-J3-15 | Changing the password revokes outstanding refresh tokens | PASS |  |  |
| E2E-J3-16 | Suspended tenant: sign-in refused (403 'Tenant suspended') and already-issued tokens stop working; reactivation restores access | PASS |  |  |
| E2E-J3-17 | MFA can be turned off with password + code; login then returns a session directly | PASS |  |  |
| E2E-J4-01 | Same 6 recipients, same domain, two tenants launched together: each recipient gets exactly 2 emails (one per tenant), each from its own mailbox | PASS |  |  |
| E2E-J4-02 | Each tenant's emails carry its own identity: distinct unsubscribe URLs (12), tenant Y's footer address is Y's, X's is X's | PASS |  |  |
| E2E-J4-03 | SES events for X's messages move only X's numbers (X opened 3 / Y opened 0), Y's metrics untouched | PASS |  |  |
| E2E-J4-04 | X's recipient unsubscribes: suppressed in X only; Y still has them subscribed with step 2 pending | PASS |  |  |
| E2E-J4-05 | Reply lands in X only: X's conversation with that address ends INBOUND (REPLIED), Y's own conversation with the same address stays OUTBOUND-only, enrolment ACTIVE, step 2 pending | PASS |  |  |
| E2E-J4-06 | Bounce on Y's message: Y suppresses r2 and Y's domain health shows it; X's suppression and domain row are clean | PASS |  |  |
| E2E-J4-07 | Step 2 goes to exactly each tenant's own live contacts: X skips its unsubscribed+replied (4 sent), Y skips its bounced (5 sent) | PASS |  |  |
| E2E-J4-08 | Independent daily limits: X's mailbox is capped at 10/day so its step 3 is held, while Y (same domain) sends all 5 | PASS |  |  |
| E2E-J4-09 | Cross-tenant object access by id is refused (404) for read, write, pause and erase | PASS |  |  |
| E2E-J4-10 | Global search for the shared address domain returns each tenant only its own records (no foreign ids) | PASS |  |  |
| E2E-J4-11 | Contact exports contain only the caller's tenant (6 data rows each, no cross ids, own sender-specific data) | PASS |  |  |
| E2E-J4-12 | Reports: each tenant's total_emails_sent equals its own OUTBOUND sends (X 10, Y 16 incl. the bounced one); inbound replies/bounce notices are not 'sent' | PASS |  |  |
| E2E-J4-13 | Domain health is per tenant: after the health cycle each has one row for the shared domain with its own 24h counters (X 10 sends / 0% bounce; Y 16 sends / bounce > 0) | PASS |  |  |
| E2E-J5-01 | Import preview: 12 rows detected and columns auto-mapped (email, name, company, domain, title) | PASS |  |  |
| E2E-J5-02 | Import: 10 contacts + 2 companies created, 2 rejected rows with reasons (invalid email, in-file duplicate) | PASS |  |  |
| E2E-J5-03 | Mapped custom field created (NUMBER) and populated (w2 -> 3); contacts linked to the company by domain; tags split | PASS |  |  |
| E2E-J5-04 | Static list holds the 6 Wayne contacts; active list (tag imp-<run>) resolves to all 10 from its filter | PASS |  |  |
| E2E-J5-05 | Bulk edit: tag added to 4 contacts, 2 reassigned to the agent; history shows both changes; active list still resolves 10 | PASS |  |  |
| E2E-J5-06 | Duplicate detection lists the two 'Dee Dupe' contacts as one group | PASS |  |  |
| E2E-J5-07 | Merge: duplicate's lead, deal, task, activity, list membership and campaign enrolment are re-pointed to the surviving contact; chosen field (CTO) wins | PASS |  |  |
| E2E-J5-08 | After merge the campaign has exactly one pending email, addressed to the surviving contact (no double send, none to the merged address) | PASS |  |  |
| E2E-J5-09 | Merged duplicate no longer exists (404, row removed - the survivor keeps the data) | PASS |  |  |
| E2E-J5-10 | Soft delete: contact hidden (404), appears in the recycle bin with purge date + who deleted; active list drops it (9), nothing else removed | PASS |  |  |
| E2E-J5-11 | Restore brings the contact back with its lead, task and list memberships; active list returns to 10 | PASS |  |  |
| E2E-J5-12 | After 90 days restore is refused and the purge job removes the contact with its lead, task, list memberships and history; an audit row remains | PASS |  |  |
| E2E-J5-13 | Purge touched nothing else: 9 imported contacts + the company still exist | PASS |  |  |
| E2E-J6-01 | Two campaign automation rules created (open -> pause prospect, click -> send template) | PASS |  |  |
| E2E-J6-02 | SES Open event fires the 'pause prospect' rule: enrolment becomes PAUSED | PASS |  |  |
| E2E-J6-03 | SES Click event fires the 'send email' rule: the worker sends the rule's template to that contact through SES (personalised subject) | PASS |  |  |
| E2E-J6-04 | A PAUSED prospect receives no further sequence step (step 2 goes only to the other 3 contacts) | PASS |  |  |
| E2E-J6-05 | Workflow rules created (lead->SQL: task + notify manager; deal amount > 20000: set COMMIT + notify) | PASS |  |  |
| E2E-J6-06 | Lead -> SQL runs the rule: owner gets a task 'Prepare handover for Wf0 Lead' due in 2 days | PASS |  |  |
| E2E-J6-07 | Manager is notified ('Wf0 Lead reached SQL'); rule run_count = 1; a non-matching change (SQL -> same) does not re-run it | PASS |  |  |
| E2E-J6-08 | Deal amount raised 15000 -> 25000 crosses the threshold: forecast category set to COMMIT by the rule and manager notified once | PASS |  |  |
| E2E-J6-09 | Raising it further (25000 -> 30000, already above) does not fire the rule again | PASS |  |  |
| E2E-J6-10 | Round-robin assignment: 6 unowned contacts -> leads cycle through the 3-agent pool in order (a1,a2,a3,a1,a2,a3) and the agents are notified | PASS |  |  |
| E2E-J6-11 | Assigned agent gets an 'ASSIGNED' notification per lead (agent 2 received 2) | PASS |  |  |
| E2E-J6-12 | Stale-deal job: idle 20 days -> owner alerted ('no activity for 20 days'); past close date -> alerted; fresh deal -> nothing | PASS |  |  |
| E2E-J6-13 | Running the job again does not duplicate the alert (one per deal per week) | PASS |  |  |
| E2E-J6-14 | Notification e-mail delivery: the stale-deal alert is emailed to the owner through SES ('[Vector] Stale deal: ...' with a link) | PASS |  |  |
| E2E-J6-15 | A user who switched e-mail notifications off still gets the in-app alert but no e-mail (agent 3: alert yes, mail none; agent 2 mailed) | PASS |  |  |
| E2E-J6-16 | Lead recycle: a disqualified lead whose recycle date has passed returns to NEW, owner notified ('Lead back in play'), recycle date cleared | PASS |  |  |
| E2E-J6-17 | Marking notifications read clears the unread counter | PASS |  |  |
