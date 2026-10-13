# BRD system & integration test report

**Run:** 2026-10-10 (a Saturday) against the local stack on branch `claude/eager-gates-s6yb8g` at commit `310e431`. That commit includes cookie sessions, two-factor sign-in and tenant-owned sending domains.

**Overall: 401 test cases. 362 pass, 23 fail, 16 blocked.**

| Suite | Pass | Fail | Blocked |
|---|---|---|---|
| Contacts (§5.2) | 86 | 7 | 4 |
| Sales pipeline (§5.4–5.7) | 90 | 6 | 0 |
| Outreach & compliance (§5.1, §5.3, §7) | 93 | 7 | 3 |
| Reports & platform (§5.8–5.12, §6) | 93 | 3 | 9 |

The 23 failures break down as:

| Category | Count |
|---|---|
| Confirmed defects | 15 |
| Need triage | 3 |
| Weekend or hourly-job timing (re-run on a weekday) | 4 |
| Test-side issue | 1 (SP-140) |

## Confirmed defects

| # | Case | Severity | Defect | Evidence / suspected location |
|---|---|---|---|---|
| 1 | OUT-015 | **High** | Adding contacts to an **active** campaign via bulk list enrollment schedules no emails, so the contacts are silently never emailed | `prospect_list_router.enroll_prospects` adds CampaignProspect rows but never calls `_preschedule_all_emails` (unlike `CampaignEmailService.enroll_prospects`) |
| 2 | RP-114 | **High** (visibility) | A manager can read another team's campaign analytics by passing `user_id` | `/reports/dashboard-summary?user_id=…` and `campaign-comparison` return 200. `app/routers/reports_router.py` forces only AGENTs to their own id |
| 3 | CM-012 | Medium | Creating a contact on a soft-deleted company's domain gives a 500 | `POST /contacts` returns 500 INTERNAL_SERVER_ERROR. A unique-key clash with the deleted company, most likely |
| 4 | CM-013 | Medium | An import links a contact to a **soft-deleted** company | Contact's account has `deleted_at` set |
| 5 | CM-085 | Medium | A single-column CSV (just `Email`) is mis-parsed: headers come out as `['E','ail']` | The CSV delimiter sniffer picks `m` as the separator. Import fails with 400 |
| 6 | OUT-055 | Medium | A reply saying "unsubscribe" does not unsubscribe the contact (consent stays OPT_IN) | IMAP reply → consent path |
| 7 | CM-080b / OUT-102 | Medium (compliance) | GDPR erasure (`DELETE /prospects/{id}`) leaves no audit record of who erased what and when; BRD §7 requires deletion to be logged | No audit_logs row. `contact_service.delete_contacts` also deletes the contact's property history |
| 8 | OUT-011 | Medium | Upload accepts syntactically invalid emails (`broken@`, `has space@x.com`) | `utils/email_utils.py parse_email` only checks for `@` and a non-personal domain |
| 9 | CM-024b | Medium | Required custom properties are enforced in the form but not on import | The import accepted a row missing a required property |
| 10 | CM-060 | Low | BRD says admins **and managers** can export contacts; managers get 403 with the default permissions | Default permission map |
| 11 | CM-046b | Low | `GET /campaigns/lists` is unreachable: it returns 404 "Campaign not found" | Route order: shadowed by `GET /campaigns/{campaign_id}` |
| 12 | SP-131 | Low | When a manager enrolls an exec's contacts while the exec is at the daily new-contact limit, nobody is told the first emails will wait | 200 with no notice. BRD §5.5 asks for the user to be told |
| 13 | SP-066 | Low | SQL queue shows `sql_age_days = -1` for a lead qualified today | Date arithmetic mixes local and UTC dates. It passed on the first run, so it depends on the time of day |
| 14 | RP-032 | Low | Funnel "% from first step" can exceed 100% (lead 140%, opportunity 260%) | `sales_reports_router.funnel()` caps `from_previous` but not `from_first` |
| 15 | RP-142 | Low (config) | Calling the API **directly**, a client can dodge the per-IP login limit by sending its own `X-Forwarded-For` | `TRUSTED_PROXY_HOPS=1` trusts the right-most hop. That is correct behind a load balancer, which appends the real client IP, but wrong when the API port is reachable directly. Keep the API reachable only through the load balancer |

**Fixed by the latest changes:** OUT-076 and OUT-077 failed before the tenant-owned domains change and pass now. One workspace's bounces no longer pause another's campaign on a shared domain, and the domain list no longer shows another workspace's mailboxes. OUT-122 (SES quota gave a 500) also passes now.

## Need triage (evidence unclear)

| Case | What the test saw |
|---|---|
| OUT-042 | A duplicate SES delivery event is correctly answered "duplicate", but the first delivery left no stored delivery event (`delivered_events=0`). It may be a defect, or the test counting the wrong event type |
| OUT-119 | Campaign analytics totals differ from the counts the test computed itself (5 sent, 1 open, 1 click, 1 bounce, 1 unsubscribe). Needs a field-by-field comparison |
| OUT-132 | Browser check: the launched campaign's detail page didn't show the campaign name; the captured detail is empty |

## Not a product failure (re-run on a weekday / allow for hourly jobs)

- **Weekend:** OUT-034, OUT-037, OUT-049 (blocked) and SP-139, SP-140. The scheduler correctly holds every send outside US business days, so these sending paths can't be reached at a weekend.
- **Hourly jobs:** SP-141 (lead recycling) and SP-142 (stale-deal alert) run in the worker's hourly job; the tests waited only about 3 minutes. RP-107 is the same.
- **Needs external accounts:** RP-087b (email notification delivery: no mail sink locally) and RP-102b (Google/Microsoft calendar and email sync: no OAuth client configured).

## BRD requirements not implemented

| Case | Requirement | BRD priority |
|---|---|---|
| CM-014 | Contact linked to several companies with association labels | BR-CM |
| CM-033 | @mention a colleague in a note notifies them | BR-CM-16 |
| CM-082 | Company data enrichment from the domain | BR-CM-42 (Could) |
| CM-083 | Contact scoring on fit and engagement | BR-CM-43 (Could) |
| RP-070 | AI contact discovery / enrichment | §5.11 (optional) |
| RP-103 | Quote discount above a threshold routed for approval | BR-SF-17 |
| RP-104 | A/B testing of subject lines | BR-SF-18 |
| RP-105 | Single sign-on with Microsoft 365 | BR-SF-19 |
| RP-106b | Outbound webhooks for other systems | BR-SF-20 |
| RP-161 | KPI reports: SQL-to-win rate, forecast accuracy | §8 |

## Non-functional measurements

| Measure | Result | BRD target |
|---|---|---|
| 10,000-row import (contacts + 800 companies) | **68.7 s**, all 10,000 created (145 rows/s) | 10k-row imports supported ✅ |
| Contact search with 10k contacts in the workspace (169k in the DB) | p50 **74 ms**, p95 109 ms | < 1 s ✅ |
| Contact search at 100k (extrapolated, not measured) | ~590 ms p50 | < 1 s ✅ (estimate; `search_text` has no index, so measure before relying on it) |
| 25 concurrent users, mixed reads, 60 s | 2,342 requests, 39 req/s, p50 409 ms, p95 778 ms, **0 errors** | 20+ concurrent users ✅ |
| Sales report endpoints (single user) | 13–90 ms | n/a |
| Login lockout | 10 wrong passwords → 429 for about 15 min | n/a |
| Health check | 71 ms | n/a |

## Coverage notes

- **Browser checks** ran for the contacts, sales and reports screens, with screenshots in each suite's `shots/` or `ui_shots/` folder. No page errors were recorded except OUT-132.
- **Sending stops at SES:** sending tests check scheduling, suppression, pacing and state changes. Real delivery can't be tested without AWS credentials.
- **The new two-factor sign-in** was checked separately in a browser:
  - cookies only, with no tokens in browser storage
  - writes protected against forged requests
  - silent refresh when the session cookie expires
  - enable with QR code and recovery codes
  - the code step at login, with wrong and right codes

---

# Final status after development round (13 Oct)

The first report (above) covered only the BRD-driven suites. On 12 Oct five more test layers were added: a contract check, an API sweep, end-to-end journeys and two browser suites. Defects found in all of them were then fixed in a development round (backend commit `b51812a`, frontend commits `cf6d02a`, `5b0c605` and `6abb136`). Every layer was then re-run on 13 Oct. The E2E run was on a Tuesday, against image `vector-api:e2e` built at 07:14.

**Status words used below:**
- **Verified** means a test in this repo proves the fix: a system test case id, or a named pytest.
- **Not re-verified** means the code was changed but no system test has re-checked it since.

## (a) Results by test layer

| Layer | What it checks | Before the round | Latest | Notes |
|---|---|---|---|---|
| Frontend-backend contract (`contract/check_contract.py`) | Every API call the React app makes exists in the backend OpenAPI | 11 broken of 298 calls | **307 of 307 match** | Manual step; needs both repos checked out |
| API sweep (`api_sweep/`) | All endpoints: authentication, roles, hierarchy, tenant isolation, validation, idempotency, response schema | 5,526 pass / 114 fail / 196 blocked | **5,630 / 12 / 201** | See the breakdown of the 12 below |
| BRD Contacts (`contacts/`) | §5.2 | 86 / 7 / 4 | **93 / 0 / 4** | All 4 blocked are features that were not built at run time (see (c)) |
| BRD Sales pipeline (`sales_pipeline/`) | §5.4–5.7 | 90 / 6 / 0 | **93 / 3 / 0** | SP-140 to SP-142 are environment or timing failures, not defects |
| BRD Outreach and compliance (`outreach/`) | §5.1, §5.3, §7 | 93 / 7 / 3 | **100 / 3 / 0** | OUT-052, OUT-074, OUT-132 (see below) |
| BRD Reports and platform (`reports_platform/`) | §5.8–5.12, §6 | 93 / 3 / 9 | **93 / 0 / 12** | Blocked: 6 features not built, 3 rate-limit cases that are opt-in, RP-087b, RP-102b, RP-107 |
| **BRD total** | 401 cases | 362 / 23 / 16 | **379 / 6 / 16** | |
| New features (`new_features/`) | Multi-company contacts, @mentions, SQL-to-win and forecast-accuracy KPIs, purge, plus the hardening fixes | n/a | **40 / 0 / 1** | NF-048 blocked: an SQL-to-win check against a built dataset |
| End-to-end journeys (`e2e/`) | Real worker, SES stand-in, fake IMAP, real links; outreach to revenue, deliverability, accounts, cross-tenant, CRM, automation | 174 / 13 / 1 | **183 / 4 / 1** (188 cases) | Details in `e2e/FINDINGS.md` |
| UI outreach and admin (`ui_outreach/`) | All outreach and admin screens in a real browser | 193 / 3 / 0 | **192 / 4 / 0** (196 cases) | See the UI notes below |
| UI CRM and sales (`ui_crm/`) | Contacts, accounts, lists and import, tasks, recently deleted, sales | 150 / 3 / 4 | **150 / 3 / 4** (157 cases) | Run at 07:13, before the tests were updated for frontend commit `6abb136`. No final numbers yet |
| CI pytest (`tests/`) | Unit and integration tests in CI | n/a | **84 passed** | A later pytest file, `tests/test_metrics_inbox_fixes.py` (2 tests, 13 Oct 07:38), is not in this count |

**The 12 remaining API sweep failures:**

| Count | Classification | Cause |
|---|---|---|
| 6 | Intentional change | `GET /imports` now requires `manage_prospects` |
| 2 | By design | Upload revalidation endpoints are stateless |
| 1 | Test fixture | Mark-opened fixture |
| 3 | Test side | Template regeneration now succeeds and replaces the template the sweep had saved |

None of the 12 is a product defect.

**Remaining BRD failures:**

- **SP-140:** environment. The worker tried real SES and got `SSL validation failed for https://email.us-east-1…`.
- **SP-141, SP-142 and OUT-074:** these run in hourly worker jobs, and the tests do not wait an hour.
- **OUT-052:** a defect. The unified inbox shows an HTML snippet, and the reply is not listed as the last message. The source was changed afterwards (`conversation_service.py`, 13 Oct 07:37) and pytest `test_unified_inbox_lists_reply_last_with_plain_text_snippet` was added. Not re-verified by OUT-052.
- **OUT-132:** a browser capture of the campaign detail page came back empty (`{}`). Needs triage; the UI suites render the same page correctly.

**Remaining E2E failures:**

- **E2E-J1-15:** defect D1, still open.
- **E2E-J1-44:** defect D4, the "clicked" part.
- **E2E-J1-87b and E2E-J2-09:** notifications that are not implemented.
- **E2E-J1-67 (blocked):** needs an LLM.

**Remaining UI failures:**

- **UIO-WIZ-05:** same as D1.
- **UIO-AUTO-02b:** the "Pause Campaign" action option. The test was not updated after frontend commit `cf6d02a` changed the dropdown; needs a test update or triage.
- **UIO-SHELL-404:** the test expects a redirect home. Frontend commit `6abb136` added a 404 page that keeps the URL, so this is most likely a test that needs updating.
- **UIO-DET-08:** the "Send Now" result dialog did not appear. Not triaged.
- **UIC-CD-01, UIC-CD-12, UIC-LST-08:** not triaged. They appeared after the multi-company and contact-record UI changes; the likely cause is selectors.
- **UIC-CD-16, UIC-CD-17, UIC-ACC-12, UIC-RD-03 (blocked):** these features now exist in frontend commit `6abb136`, but the tests still mark them blocked.

## (b) Defect register

**Severity:** H = high, M = medium, L = low.

**Found by:**
- **BRD** = the BRD suites
- **SWP** = the API sweep
- **CTR** = the contract check
- **E2E** = the end-to-end journeys
- **UIO** = the UI outreach suite
- **UIC** = the UI CRM suite

| ID | Description | Sev | Found by | Status |
|---|---|---|---|---|
| B1 | Bulk list enrollment into an active campaign scheduled no emails | H | BRD OUT-015 | Fixed + verified (OUT-015) |
| B2 | A manager could read another team's campaign analytics through `user_id` | H | BRD RP-114 | Fixed + verified (RP-114) |
| B3 | Creating a contact on a soft-deleted company's domain gave a 500 | M | BRD CM-012 | Fixed + verified (CM-012) |
| B4 | An import linked contacts to a soft-deleted company | M | BRD CM-013 | Fixed + verified (CM-013) |
| B5 | A single-column CSV was mis-parsed (delimiter sniffer) | M | BRD CM-085 | Fixed + verified (CM-085) |
| B6 | A reply saying "unsubscribe" did not unsubscribe (two causes; see also D2) | M | BRD OUT-055 | Fixed + verified (OUT-055, E2E-J1-63) |
| B7 | GDPR erasure was not audit-logged | M | BRD CM-080b / OUT-102 | Fixed + verified (CM-080b, OUT-102, NF-063) |
| B8 | Upload accepted syntactically invalid emails | M | BRD OUT-011 | Fixed + verified (OUT-011) |
| B9 | Required custom properties were not enforced on import | M | BRD CM-024b | Fixed + verified (CM-024b) |
| B10 | Managers could not export contacts | L | BRD CM-060 | Fixed + verified (CM-060) |
| B11 | `GET /campaigns/lists` was shadowed by a route and returned 404 | L | BRD CM-046b | Fixed + verified (CM-046b) |
| B12 | No notice when enrolling for a user who is at the daily new-contact limit | L | BRD SP-131 | Fixed + verified (SP-131) |
| B13 | SQL queue showed `sql_age_days = -1` (local vs UTC date) | L | BRD SP-066 | Fixed + verified (SP-066; depends on time of day) |
| B14 | Funnel "% from first step" could exceed 100% | L | BRD RP-032 | Fixed + verified (RP-032) |
| B15 | `X-Forwarded-For` spoofing bypasses the per-IP login limit when the API port is reached directly | L (config) | BRD RP-142 | By design / deployment: expose the API only behind the load balancer. RP-142 is opt-in and was not run |
| T1 | Duplicate SES delivery event stored no delivery row (triage item) | – | BRD OUT-042 | Verified OK (OUT-042 passes) |
| T2 | Campaign analytics totals differed from computed counts (triage item) | – | BRD OUT-119 | Fixed + verified (OUT-119, E2E-J1-42/-43) |
| T3 | Campaign detail page empty in the browser check | – | BRD OUT-132 | Open, needs triage (still fails; the UI suites render the page) |
| S1 | Campaign wizard: `GET /campaigns/lists`, create, execute, template regenerate and re-activate returned 500 | H | SWP | Fixed + verified (sweep) |
| S2 | About 35 endpoints returned 500 for oversize or out-of-range input; now 422 or 409 | M | SWP | Fixed + verified (sweep, NF-067) |
| S3 | NUL characters gave 500 | M | SWP | Fixed + verified (sweep, NF-068) |
| S4 | Negative or huge `limit` and `fy` values were not rejected | L | SWP | Fixed + verified (sweep) |
| S5 | Upload had no row cap (now 20,000 rows) | M | SWP | Fixed + verified (sweep) |
| S6 | `POST /templates` accepted another tenant's sequence | H | SWP | Fixed + verified (sweep) |
| S7 | IMAP sync to an unreachable host returned 502 or 500 | L | SWP | Fixed + verified (sweep) |
| S8 | Upload filename not sanitised or validated | L | SWP | Fixed + verified (sweep) |
| S9 | Repeated DELETE returned 400 or 200 instead of 404 on three endpoints | L | SWP | Fixed + verified (NF-065; pytest `test_repeat_remove_from_campaign_is_404`) |
| S10 | 33 response schemas declare non-nullable fields that return null | L | SWP | Open (schema documentation only; not re-checked) |
| C1 | 11 frontend calls to routes that do not exist (AI email module, automation update) | H | CTR | Fixed + verified (contract 307/307) |
| D1 | Without an LLM the wizard creates a template for step 1 only, so a multi-step campaign cannot launch | H | E2E J1-15 / UIO-WIZ-05 | **Open** (E2E-J1-15: step 2 reads `No subject` / `No body content`; UIO-WIZ-05: 1 template for 2 steps). The launch gate itself works (E2E-J1-18) |
| D2 | A reply that only says "unsubscribe" was stripped to empty before the check | M | E2E J1-63 | Fixed + verified (E2E-J1-63; pytest `test_unsubscribe_only_reply_is_recognised`) |
| D3 | A PAUSED prospect still received the next step | M | E2E J6-04 | Fixed + verified (E2E-J6-04) |
| D4 | Opened, sent and clicked counts disagree between screens | M | E2E J1-42..47 | Sent and opened: Fixed + verified (J1-42, -43, -45, -46, -47). **Clicked: fixed, not re-verified.** E2E-J1-44 failed: `total_clicked` 5 (events) vs 4 unique clickers, because the tested image predates the fix (`reports_service.py`, 07:39). pytest `test_clicked_is_unique_prospects_on_every_screen` covers the fix |
| D5 | Dashboard "emails sent" counted inbound mail | M | E2E J4-12 | Fixed + verified (E2E-J4-12) |
| D6 | Double or wrong sign-off ("Outreach AI" plus a second signature after the footer) | L | E2E J1-31 | Fixed + verified (E2E-J1-31; pytest) |
| D7 | Planned step 2 and 3 dates stay anchored to a weekend launch | L | E2E J1-23 | Fixed, verified by pytest only (`test_planned_follow_ups_follow_shifted_first_step`); the E2E run was on a weekday, so J1-23 did not cover the weekend path |
| D8 | Reset, magic and invite links were built from a comma-separated `FRONTEND_URL` when there was no Origin header | L | E2E (J3 mail capture) | Fixed, not re-verified (J3-06/-10 pass but do not assert the link origin) |
| D9 | Invites were stored as ACCEPTED, so `accept-invite` could not be used | L | E2E J3 | Fixed + verified by pytest (`test_accept_invite_completes_invited_account`); the E2E magic-link flow J3-06..09 passes |
| D10 | Automation "send email" cancelled by the duplicate-send guard (intermittent) | M | E2E J6-03 | Fixed; pytest `test_automation_send_is_not_a_duplicate_of_step_one`; E2E-J6-03 passed in the final run (1 of 1). Extra repeats were not possible because the stack was recreated without the E2E wiring |
| D11 | Unified inbox: reply not shown as the last message, and the snippet is HTML | M | BRD OUT-052 | Fixed, not re-verified (source and pytest changed after the run) |
| U1 | `GET /imports` readable without `manage_prospects` | M | UIC-IMP-20 | Fixed + verified (NF-064, UIC-IMP-20) |
| U2 | Import counters wrong when `update_existing=false` | L | UIC-IMP-04B | Fixed + verified (UIC-IMP-04B) |
| U3 | Duplicate contact property labels accepted | L | UIC-CON-30 | Fixed + verified (NF-066, UIC-CON-30) |
| U4 | A company-only import suggested a contact property for "Industry" | L | UIC | Fixed + verified by pytest (`test_company_only_import_suggests_company_fields`) |
| U5 | Negative inbox daily limits accepted | L | UIO-MBX-06 | Fixed + verified (UIO-MBX-06) |
| U6 | Automation "Pause Campaign" action sent an enum value the backend does not have | M | UIO-AUTO-02b | Fixed in frontend `cf6d02a` (now uses the backend's pause action); UIO-AUTO-02b still fails, so a test update or triage is needed |
| U7 | Frontend: structured error detail dropped, which broke the "open existing contact/lead" links | M | UIC | Fixed + verified (UIC) |
| U8 | Frontend: company revenue could not be saved (422) | M | UIC-ACC-07 | Fixed + verified |
| U9 | Frontend: NUMBER custom fields stored as text | L | UIC-CON-31 | Fixed + verified |
| U10 | Frontend: a duplicate of a deleted contact linked to a 404 record | L | UIC | Fixed + verified |
| U11 | Frontend: campaign pause, resume and delete failed silently | M | UIO-LIST-08 | Fixed + verified |
| U12 | Frontend: bulk campaign actions sent only one request | H | UIO-LIST-07 | Fixed + verified |
| U13 | Frontend: lead list "Export" did nothing | M | UIO-DET-02 | Fixed + verified |
| U14 | Frontend: the Automation Rules page crashed (`templates.map`) | H | UIO-AUTO | Fixed + verified |
| U15 | Frontend: AI classification failure was silent | L | UIO-AIGEN-03 | Fixed + verified |
| U16 | Frontend: a new upload list was missing until reload | L | UIO-PROS-05 | Fixed + verified |
| U17 | Frontend: Inbox tab triggered a 403 for managers and agents, and hid existing conversations behind the IMAP guide | M | UIO-INBOX | Fixed + verified |
| U18 | Frontend: Campaign analytics was visible without `view_analytics` | M | UIO-SHELL | Fixed + verified |
| U19 | Frontend: the Change Password modal was clipped and unreachable | M | UIO-SHELL-PWD | Fixed + verified |
| U20 | Frontend: unit prices rounded to whole dollars | L | UIC | Fixed in frontend `6abb136`, not re-verified |
| U21 | Frontend: Unsubscribe and Resubscribe buttons inside a `<label>` (accessible name) | L | UIC | Open |
| U22 | No 404 page (unknown URLs redirected home) | L | UIO | Fixed in `6abb136`, not re-verified (UIO-SHELL-404 still expects the old redirect) |
| N1 | Contact linked to several companies with labels (CM-014) | – | BRD | Built; verified (NF-001..NF-013); UI not re-verified |
| N2 | @mention a colleague in a note notifies them (CM-033) | – | BRD | Built; verified (NF-021..NF-027); UI not re-verified |
| N3 | KPI reports: SQL-to-win and forecast accuracy (RP-161) | – | BRD | Built; verified (NF-040..NF-047); NF-048 (value against a built dataset) blocked |
| N4 | Purge in Recently deleted | – | UIC-RD-03 | Built; API verified (NF-060..NF-063); UI not re-verified |
| N5 | Create a deal from a contact, and deals on the company page | – | UIC-CD-16, UIC-ACC-12 | Built in frontend `6abb136`; not re-verified |
| E1 | API at 100% CPU after `PATCH /sales/stages/<id>` under load | – | E2E (environment) | Not reproduced; no loop found in the code |

## (c) What remains

**Not implemented (BRD), each confirmed absent by a blocked case:**

| Case | Requirement |
|---|---|
| RP-103 | Quote discount above a threshold routed for approval (BR-SF-17) |
| RP-104 | A/B test of email subject lines (BR-SF-18) |
| RP-105 | Single sign-on with Microsoft 365 (BR-SF-19) |
| RP-106b | Outbound webhooks for other systems (BR-SF-20) |
| CM-082 | Company data enrichment from the domain (Could) |
| CM-083 | Contact scoring on fit and engagement (Could) |
| RP-070 | AI contact discovery (optional) |

**Also not implemented:**
- Built-in notifications when a deal is won or changes stage (E2E-J1-87b).
- An in-app notification when a campaign is auto-paused (E2E-J2-09). Workflow rules can add both.
- Positive-reply detection without an LLM (E2E-J1-67).

**Still open:**
- D1: an offline wizard cannot produce a multi-step campaign.
- OUT-132: needs triage.
- S10: nullable response schemas.
- U21: the subscription buttons' accessible name.

**UI gaps:**
- No UI for: list rename, domain add, invite resend, hierarchy fields.
- Not covered by the browser suites: the Template Library screen, platform-admin screens, mobile layouts.
- Test updates are pending for the screens changed by frontend commit `6abb136`, so the UI CRM numbers above predate them.

**Re-verification still owed:**
- D4 clicked (E2E-J1-44)
- D7 on a weekend launch
- D8 link origin
- D10 repeat runs
- D11 (OUT-052)
- U6, U20, U22 and N1 to N5 in the UI

**Cannot be tested locally** (each needs real accounts or keys):
- Real SES delivery, bounces, complaints, opens and clicks; DKIM, SPF and DMARC; SNS signature verification and sandbox throttling. The E2E runs use moto and synthesised events.
- Real IMAP providers, OAuth mailboxes, IDLE and Sent-folder sync.
- AI-generated content and classification (OpenAI).
- Google and Microsoft sign-in, and calendar and email sync (RP-102b).
- Email notification delivery (RP-087b; no mail sink).
- Hourly worker jobs at their real cadence (SP-141, SP-142, RP-107, OUT-074).
- The per-IP login limit and lockout on the shared stack (RP-140 to RP-142 are opt-in).

---

# Final status, 13 Oct (after the build-and-fix round)

Everything below was run on 13 Oct against the rebuilt stack (alembic head `0013_revoked_access_tokens`, OpenAI key empty so nothing is spent). One runner covers all layers: `python3 scripts/test.py run <layer>` (see `docs/TESTING.md`).

| Layer | Suites | Result (pass / fail / blocked) |
|---|---|---|
| Unit | `tests/unit` | 693 / 0 / 0 |
| Functional | `tests/` (TestClient + MySQL, full migration chain) | 195 / 0 / 0 |
| System: contract | frontend calls vs OpenAPI | 325 / 325 matched |
| System: BRD suites | contacts, sales pipeline, outreach, reports | 96/0/1, 96/0/0, 103/0/0, 98/0/7 |
| System: feature suites | new features, discount approval, A/B tests, webhooks, contact scoring, behaviour fixes, admin controls | 41/0/1, 30/0/0, 33/0/0, 23/0/0, 28/0/0, 19/0/0, run in UI layer |
| System: API sweep | every endpoint: auth, roles, hierarchy, isolation, validation | 5,650 / 0 / 203 |
| UI (Playwright) | outreach, CRM, a11y, mobile, sessions, platform admin, template library, admin controls, discount approval, A/B, scoring, webhooks, behaviour fixes | 196/0, 165/0, 81/0, 58/0, 21/0, 18/0, 17/0, 11/0, 6/0, 7/0, 8/0, 9/0, 6/0 |
| End-to-end | journeys J1-J6 (real worker, SES stand-in, fake IMAP) | 187 / 0 / 1; J6 re-run twice, no duplicate-send flake |

**Built in this round:** quote discount approval (RP-103), A/B subject tests (RP-104), outbound webhooks (RP-106b), contact scoring (CM-083), deal and auto-pause notifications, starter templates when AI is unavailable, list rename, invite resend, hierarchy fields, add sending domain, responsive layout with a mobile drawer, accessible dialogs everywhere, return-to-page after session expiry, suspended-workspace handling, platform-admin tenant/user views and scheduler control, logout revokes the access token, OpenAI spend cap of $0.20 per day.

**Still BLOCKED (need outside accounts or are optional BRD items):** CM-082 company enrichment, RP-070 AI contact discovery, RP-105 Microsoft 365 sign-in, RP-087b email delivery to a real mail sink, RP-102b Google/Microsoft OAuth, RP-140..142 rate-limit lockout (opt-in), E2E-J1-67 positive-reply detection (needs an LLM), NF-048 (covered by E2E and pytest), API sweep cases needing a platform admin, OpenAI, AWS or OAuth.

**Cannot be proven locally:** real SES delivery and events, DKIM/SPF/DMARC, SNS signatures, real IMAP/OAuth mailboxes, AI content, hourly jobs at their real cadence (they are triggered on demand in tests).
