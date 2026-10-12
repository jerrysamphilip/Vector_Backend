# E2E findings

Stack: current tree (commit 68c8d7e) in image `vector-api:e2e`, SES = moto, IMAP = fake server, worker = the real worker.
Evidence for each item is in `results.json` (case id given) and was reproduced through the API plus read-only DB queries.

## Defects (by severity)

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
- No automatic reply -> lead: leads are created by the one-click `/leads/from-message`; "positive reply" suggestions need an LLM (no heuristic fallback) (E2E-J1-67).
- No notification when a deal is won / changes stage, and none to the owner/manager when a campaign is auto-paused (alert only) (E2E-J1-87b, E2E-J2-09). Workflow rules can add them (E2E-J6-07).

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
