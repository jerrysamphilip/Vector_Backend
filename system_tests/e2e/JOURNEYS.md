# Journeys

Case IDs are `E2E-J<n>-<nn>` in results.md/json. Every hop asserts concrete values (counts, headers, rows, amounts), not just status codes.

## J1 - Outreach to revenue (`j1_outreach_to_revenue.py`, 110 cases)
One workspace: owner (L1) -> manager (L2) -> two agents (L3).
1. **Setup (01-06)** sign-up, hierarchy via `/team/hierarchy`, sender identity, two mailboxes with IMAP settings pointing at the fake IMAP (secrets encrypted, IMAP login really performed by the API container, domain row created).
2. **Upload (07-10)** 21-row CSV: 16 accepted, 5 rejected with the specific reason (personal domain, invalid address, missing first name, in-file duplicate, previously unsubscribed); companies/accounts created and shared by two contacts.
3. **Campaign (11-23)** `POST /campaigns`, sequences 0/2/3, wizard `generations` with no LLM (fallback template content, placeholders, a template for every step), enrolment (+ idempotent re-enrol), launch gate, pre-scheduling (48 messages, 8/8 inbox split, rendered per contact, business day, wait-day spacing).
4. **Send (24-34)** the worker sends step 1 through moto: 16 captured, one per eligible contact, DB<->SES id correlation, per-inbox pacing, daily counters, MIME headers (From, Reply-To, List-Unsubscribe URL+mailto, List-Unsubscribe-Post, Message-ID), postal-address footer once, signature, unsubscribe link, plain-mode text.
5. **Events (35-47)** Delivery/Open/Click/Bounce through the webhook, each posted twice (dedupe), event rows, final_status, hard bounce suppression with no auto-pause (1/16), and the numbers on campaign detail, `/metrics`, analytics, dashboard, global analytics, comparison, time series must agree.
6. **Unsubscribe (48-57)** follow the link from a real captured email: GET shows a page and changes nothing, POST (form from the page) unsubscribes, RFC 8058 one-click POST, cancelled steps, event + counters, re-enrol/re-upload refused, forced-due follow-ups never sent, timeline entry.
7. **Replies (58-68, 69-70)** replies placed in the fake mailbox: ingestion by `/inboxes/{id}/sync`, threading by In-Reply-To, unified inbox, sequence stopped, OOO not stopping, reply-"unsubscribe", unknown sender, reply counters; positive classification without an LLM (BLOCKED, documented); the worker's own IMAP loop ingests a reply with no API call.
8. **Sales (71-87b)** reply -> `leads/from-message` -> hierarchy visibility -> BANT gate -> SQL queue -> convert (amount/close date, campaign attribution) -> stage moves with history -> won needs a reason -> lifecycle, contact history, deal timeline, notifications.
9. **Reports (88-94, 95)** a deal book across FY boundaries (Apr 1, Mar 31, Sep 30, Oct 1) with independently computed pipeline/weighted/forecast per quarter and FY, team performance, leaderboard, dashboard, scoping per role, and amount hiding for AGENT.
10. **Follow-ups / pause (96-103)** step-2 spacing from the real send (B01), dead contacts get nothing, a contact whose step 1 is unsent is held, step 3 re-planned, pause latency, nothing sent while paused, resume re-spreads (no burst).
11. **GDPR (104-108)** erase the converted contact: every `prospect_id` row gone, events/messages/conversations gone, lead gone, deal detached, audit row, suppression kept.

## J2 - Deliverability (`j2_deliverability.py`)
Tenant D and tenant E on the same domain. <50 sends: 4 hard bounces no pause, soft bounce ignored, 5th pauses (reason text); domain-health row per tenant; E unaffected; alerts job; notification; resume baseline. Complaints: 1 ok, 2nd pauses, message COMPLAINED + suppression. 54 sends: 2 bounces (3.7%) ok, 3rd (5.6%) pauses with the rate in the reason; resume then 1 complaint pauses again; SNS-envelope shape; hourly health cycle.

## J3 - Accounts and access (`j3_accounts_access.py`, 17 cases)
Register; MFA setup/enable with TOTP computed in the test; logout; login requires the code; recovery code once; invite (real email from SES: set-password link only, no password anywhere (I-13), password sign-in refused while invited, link once only, first password chosen without a current one); forgot-password (real email, generic answer for unknown address, single-use link, old password rejected, old refresh token revoked); role change and deactivation effective on existing tokens; reactivation; password change revokes refresh tokens; suspended tenant (platform admin) blocks login and live tokens; MFA disable.

## J4 - Cross-tenant (`j4_cross_tenant.py`)
Two tenants, same six recipient addresses, same sending domain, launched together. Each recipient gets two emails (one per tenant); events, unsubscribe, reply, bounce on one side never show on the other; step 2 goes to each tenant's own live contacts; independent daily limits; id probes (read/write/pause/erase 404); search, exports, dashboard totals, domain health per tenant.

## J5 - Import / CRM chain (`j5_crm_chain.py`)
Import with mapping and a new NUMBER custom field -> static + active lists -> bulk tag/assign -> duplicates -> merge with lead/deal/task/activity/list/campaign enrolment moved -> campaign has one pending mail for the survivor -> recycle bin -> restore -> 90-day purge job with audit row; nothing else touched.

## J6 - Workflows / automation (`j6_automation.py`)
Campaign rules driven by SES events (open -> pause prospect; click -> send template through the worker/SES), a paused prospect must not get the next step; sales workflow rules (lead->SQL task + manager notification; deal amount threshold sets COMMIT, fires once); round-robin assignment; stale-deal job (idle, overdue, fresh, dedupe); notification e-mail delivery via SES and opt-out; lead recycle; mark-read.
