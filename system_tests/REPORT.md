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
