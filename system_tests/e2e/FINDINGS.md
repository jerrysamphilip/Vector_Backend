# E2E findings

Stack (first runs): tree at commit 68c8d7e in image `vector-api:e2e`, SES = moto, IMAP = fake server, worker = the real worker.
Evidence for each item is in `results.json` (case id given) and was reproduced through the API plus read-only DB queries.

## Status after the development round (re-run 13 Oct, Tuesday)
Run: image `vector-api:e2e` built 13 Oct 07:14 from the tree after commit b51812a. Result **183 pass / 4 fail / 1 blocked** of 188
(was 174 / 13 / 1). Fixed and now passing: E2E-J1-23, -31, -42, -43, -46, -47, -63, J4-12, J6-04.

| ID | Status | Evidence |
|---|---|---|
| D1 | **Open** | E2E-J1-15 fails: templates exist for step 1 only; `GET /campaigns/{id}/sequences/2/emails` -> `{"subject":"No subject","body":"No body content"}`. Same as UIO-WIZ-05. The launch refusal (E2E-J1-18) works. |
| D2 | Fixed + verified | E2E-J1-63 pass (reply "unsubscribe" -> workspace suppression, sequence stopped); pytest `test_unsubscribe_only_reply_is_recognised`. |
| D3 | Fixed + verified | E2E-J6-04 pass: step 2 goes only to the 3 non-paused contacts. |
| D4 | **Partly fixed.** Opened/sent now agree; *clicked* did not in the tested image | J1-42, -43, -45, -46, -47 pass. **E2E-J1-44 fails**: dashboard-summary `total_clicked: 5` (5 click events) vs 4 unique clickers (all other fields match). The tested image still summed click events (`reports_service.py` l.117 in the container). The source was changed at 07:39, after the image was built, to count unique clickers (`_unique_prospects_with(EVENT_CLICK)`), with pytest `tests/test_metrics_inbox_fixes.py::test_clicked_is_unique_prospects_on_every_screen`. E2E has not re-checked this: the stack was recreated without the E2E wiring before a re-run was possible. Status: fixed, not re-verified by E2E. |
| D5 | Fixed + verified | E2E-J4-12 pass: each tenant's `total_emails_sent` equals its own OUTBOUND sends (X 10, Y 16). |
| D6 | Fixed + verified | E2E-J1-31 pass: one sign-off with the campaign sender's name and no second signature after the footer. |
| D7 | Fixed, not re-verified by E2E | E2E-J1-23 passes, but this run launched on a weekday, so the shifted-step-1 path did not run. Covered by pytest `test_planned_follow_ups_follow_shifted_first_step` only. |
| D8 | Fixed, not re-verified by E2E | J3-06/-07/-10/-11 pass (links work), but they do not assert the link's origin when there is no `Origin` header. |
| D9 | Fixed (pytest) | pytest `test_accept_invite_completes_invited_account` and `test_invitation_lifecycle_and_repeat_deletes`. The E2E invite flow (J3-06..09, magic link) passes; E2E does not call `accept-invite`. |
| D10 | Fixed; passed in this run | E2E-J6-03 pass (1 of 1 runs; previously failed 1 of 3); pytest `test_automation_send_is_not_a_duplicate_of_step_one`. The two extra J6 repeats could not run: another agent recreated `vector-api-1`/`vector-worker-1` without the SES stand-in wiring (pre-flight refused). The run was not restarted, per the no-container-restart rule. |
| NI-1 | Not implemented | E2E-J1-87b: no built-in notification when a deal is won or changes stage (agent got only `ASSIGNED`, manager 0). |
| NI-2 | Not implemented | E2E-J2-09: auto-pause raises a reputation alert but no in-app notification (`items: []`). |
| NI-3 | Resolved (S-09) | E2E-J1-67/68: without `OPENAI_API_KEY` the rule-based reply classifier (app/services/reply_classifier.py) labels the reply POSITIVE (source RULES) and it is suggested as a lead; the cases now assert that. |

No new defects were found in this run.

## Defects (by severity) - original write-up (12 Oct)

### High
**D1 - Without an LLM the campaign wizard cannot produce a launchable multi-step campaign** (E2E-J1-15, -18)
`POST /campaigns/{id}/generations` with a 3-step sequence and no `OPENAI_API_KEY` returns `error: "No LLM result available"` and creates
the fallback template for **step 1 only** (`app/services/batch_processor_service.py:286` raises, fallback covers one step). Steps 2-3 read back
`{"subject":"No subject","body":"No body content"}` and `POST /campaigns/{id}/launch` answers `400 Missing email templates. Defined: 3, Found: 2`.
The UI wizard therefore dead-ends offline. The launch gate itself is also weak: `app/services/campaign_email_service.py:390`
(`tmpl_count < step_count`) compares the number of template rows (one per persona) with the number of steps, not step coverage - 2 personas x
1 template satisfies a 2-step campaign and `_preschedule_all_emails` then silently skips the step that has no template.
The journey works around it with `POST /templates` for steps 2-3 (reported in E2E-J1-19).

**D2 - Reply that only says "unsubscribe" still does not unsubscribe** (E2E-J1-63)
Commit 68c8d7e added `strip_boilerplate=False` at `app/services/imap_sync_service.py:537`, but the body has already been reduced by
`body = extract_latest_message_text(body)` at `imap_sync_service.py:337` (default stripping), so for a reply consisting of "unsubscribe" the
variable is `''` before the check. Observed: stored INBOUND body is empty, no `global_unsubscribes` row, consent stays `OPT_IN`.
Fix: keep the unstripped text for the unsubscribe test (or call the extractor once with `strip_boilerplate=False` for that check at line 337).

### Medium
**D3 - A PAUSED prospect still receives the next sequence step** (E2E-J6-04)
The automation action "pause prospect" sets the enrolment to `PAUSED`, but the scheduler only stops for `REPLIED/BOUNCED/UNSUBSCRIBED`
(`app/services/email_scheduler_service.py:584`). Step 2 was sent to the paused contact.

**D4 - "Opened/sent" numbers disagree between screens** (E2E-J1-42, -43, -44, -46, -47)
After 10 contacts opened (11 open events incl. one re-open) and 4 clicked (5 click events), 16 sent, 1 hard bounce:
campaign detail = 10 opened (unique); `GET /campaigns/{id}/metrics` = **0 opened** (reads the `campaign_metrics` counters; `_handle_open` in `ses_webhook_router.py:572-625` never calls `MetricsService.process_event`, unlike the bounce handler);
dashboard-summary / global-analytics / campaign-comparison / daily series = **11 opened, 5 clicked** (event rows counted, `reports_service.py:100-118`);
analytics step 1 = `sent: 15` (bounced excluded) vs 16 everywhere else.

**D5 - Dashboard "emails sent" counts inbound messages** (E2E-J4-12)
`total_emails_sent` for a tenant with 10 outbound sends and 1 inbound reply is 11 (`reports_service.py:90`, no `direction` filter).

**D10 - Automation "send email" action can be cancelled by the duplicate-send guard** (E2E-J6-03, intermittent: failed 1 of 3 runs)
`automation_rule_service._action_send_email` (`app/services/automation_rule_service.py`, ~l.100-120) queues the rule's mail on the campaign's *first* sequence step;
the scheduler's duplicate guard (`email_scheduler_service.py` `_claim`/`_cancel_duplicate`, ~l.425-470) then marks it
`CANCELLED: Duplicate: this step was already sent to this address`, so the contact never receives the offer. It only works when the rule mail wins the race against the guard.

### Low
**D6 - Double / wrong signature** (E2E-J1-31). Every campaign email ends "Regards, Outreach AI <company>" (the `{{signature_block}}` placeholder is
resolved with the platform `SENDER_NAME`, not the campaign sender) followed by the postal-address footer and then a *second* "Regards, <sender name>"
block (`email_sender_service.py:684-686` appends it because the first sign-off is not recognised). The CAN-SPAM footer sits in the middle of the mail.

**D7 - Planned follow-up dates ignore a shifted step 1** (E2E-J1-23). When launch happens on a non-business day step 1 is planned on the next
business day, but steps 2-3 stay anchored to the launch instant (`campaign_email_service.py:581`): step 2 planned ~13 h after step 1 instead of 2 days.
Self-corrects at send time (B01 re-planning, E2E-J1-96/99 pass) but the schedule preview is wrong. Date dependent (passes on a weekday launch).

**D8 - Email links fall back to the raw FRONTEND_URL list.** When a request carries no `Origin` header the reset/magic links are built from
`FRONTEND_URL` as is: `http://localhost/vector,http://localhost:8190,http://localhost:5173/reset-password?token=...` (seen in the captured
"Reset your password" mail; `auth_router.py:237`). Browsers send Origin, so the UI is fine; API clients get a broken link.

**D9 - Invitation accept endpoint is unreachable by design.** `POST /api/users/invite` stores the `Invitation` row as `ACCEPTED`
(`user_router.py:533`) while `POST /api/users/accept-invite` requires `PENDING`; the real flow is magic link + emailed temporary password (works, E2E-J3-06..08).
The temporary password is e-mailed in clear text (by design of that flow).

### Not implemented (expected by the journeys, absent in the product)
- No automatic reply -> lead: leads are created by the one-click `/leads/from-message`; "positive reply" suggestions come from the reply classifier, which falls back to rules without an LLM (E2E-J1-67).
- No notification when a deal is won / changes stage, and none to the owner/manager when a campaign is auto-paused (alert only) (E2E-J1-87b, E2E-J2-09). Workflow rules can add them (E2E-J6-07).

### Observations
- Worker IMAP latency: with ~400 mailboxes on the stack (most pointing at dead hosts) a reply sat unseen for ~5.6 minutes before the worker's loop reached the
  mailbox (E2E-J1-69 asserts the BR-DF-05 limit of 10 minutes and passes). Each dead mailbox costs a connect attempt in the same loop.
- Planned follow-up dates (D7) after a weekend launch also pushed step 3 to the following Monday in the last run (Oct 19 00:22) - same root cause.

### Environment observations (not product verdicts)
- The shared API container (single uvicorn) spun at 100 % CPU and stopped answering `/health` for minutes right after another agent's `PATCH /sales/stages/<id>` calls
  (last access-log lines); it recovered only after `docker restart vector-api-1`. Worth a look by whoever owns the sales-stage code (possible infinite loop).
- The prebuilt `vector-api:latest` image predated `SEND_ON_ANY_DAY`; the e2e stack uses `vector-api:e2e`.

## Design notes verified (pass)
Tenant-owned domains (E2E-J2-06, J4-13), soft bounce ignored (J2-03), thresholds 5 bounces / 2 complaints under 50 sends and 5 % / 0.3 % above (J2-02..16),
resume baseline, unsubscribe GET-confirm/POST, RFC 8058 one-click, SNS-event dedupe, B01 follow-up spacing and hold, pause/resume re-spread, GDPR erase with audit row,
MFA/recovery/invite/reset/suspension flows (J3 all pass), FY April-March attribution including 31 Mar / 1 Apr boundaries (J1-89/90), amount hiding.

## What the stand-ins cannot prove
- **SES**: moto accepts any raw MIME from a verified identity; no real DKIM/SPF/DMARC, no real bounces, complaints, opens or clicks, no sandbox limits, no
  throttling. Every Delivery/Open/Click/Bounce/Complaint event is synthesised by the test and posted to the webhook (raw SES shape and the unsigned
  `{Message: json}` envelope). Real SNS **signature verification** (and subscription confirmation) cannot be exercised locally.
- **Clock**: step 1 is moved to "now" with a per-campaign `scheduled_at` update (the planner puts it on the next business day on weekends/holidays);
  follow-up waits of days are shortened the same way. Real long-running timers (hourly jobs aside, which are run on demand) are not waited for.
- **IMAP**: a minimal fake (INBOX only, UID SEARCH/FETCH); no Sent folder sync, IDLE, OAuth (Google/Microsoft) mailboxes or TLS-verification behaviour.
- **LLM**: OpenAI is absent, so AI templates, persona classification quality and positive-reply detection are not tested (only the fallbacks).
- **Frontend**: these journeys are API-level; the UI agents cover the screens.
