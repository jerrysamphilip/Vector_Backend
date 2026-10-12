# Reports & Platform: test case catalogue

**Scope (BRD v2.0):** §5.8 Dashboards & Reports, §5.9 Revenue Forecasting (financial year runs April to March), §5.10 Team Performance, §5.11 AI Contact Discovery, §5.12 Salesforce-style features (BR-SF-01 to BR-SF-20), the platform-wide NFRs in §6 and the measurable KPIs in §8.

**Automation:** `run_reports_platform.py` (python3 stdlib + `../common.py`), with UI checks in `ui_check.mjs` (Playwright, Chromium).

## Shared fixture (built by the script for every case)

### Workspace A, "rp"
A new tenant with this hierarchy:

- owner: SUPER_ADMIN, no level
- Hana (head): MANAGER, L1
- Bdan (bd1): MANAGER, L2, reports to head
- Bree (bd2): MANAGER, L2, reports to head
- Exa (ex1): AGENT, L3, reports to bd1
- Exb (ex2): AGENT, L3, reports to bd1
- Exc (ex3): AGENT, L3, reports to bd2
- Mira (mr1): AGENT, L4, reports to ex1

### Seeded data
- **Contacts and leads.** 7 contacts (P1–P7) with leads L1–L7:
  - Sources: Webinar, Referral, Cold email.
  - Final stages: SQL, CONVERTED, NEW, DISQUALIFIED ("No budget", recycled in 30 days), CONTACTED, ENGAGED, SQL.
- **Campaign.** One COMPLETED campaign emailed P1–P5, and P1 and P3 replied. Rows were inserted through SQL inside the tenant as test setup.
- **Opportunities.** 12 opportunities (D1–D12) across every default stage. Close dates sit on fiscal boundaries:
  - D1: 31 Mar 2026
  - D2: 1 Apr 2026
  - D3: 30 Jun 2026
  - D4: 1 Jul 2026 (forecast category set by hand to Commit)
  - D5: 30 Sep 2026
  - D6: 15 Oct 2026 (lost)
  - D7: 31 Dec 2026
  - D8: 1 Jan 2027
  - D9: 31 Mar 2027
  - D10: 1 Apr 2027
  - D11: no close date
  - D12: 15 Nov 2026

  Client types are NEW or EXISTING. Lead L2 is converted to deal CONV (2,500, 15 Aug 2026).
- **Activities.** CALL, MEETING and NOTE activities, and one completed task.
- **Targets.** FY 2026 targets: ex1 Q1/Q2 10k, ex2 Q1 20k, ex3 Q2 25k, bd1 Q3 5k.

### Workspace B, "rp-other"
A second tenant holding one won deal of 99,999 and a target of 77,777. These values are there to expose any cross-tenant leak.

### How expected values are computed
Every expected number is computed by the script from the seed list using its own fiscal arithmetic (`rp_helpers.py`). The script never derives expected values from API output.

**Type:** S = System (end-to-end business behaviour on the live stack); I = Integration (cross-module or cross-component: reports ↔ opportunity list, worker ↔ API, UI ↔ API, DB ↔ API).

**Automated?** Y means automated; N means not automated, with the reason given.

## Health and access (§6 Availability / Security)

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-001 | §6 Availability | S | High | Stack up | GET /health | 200 `status=healthy` | Y |
| RP-002 | §6 Availability | I | High | Stack up | GET /health/ready | 200 `database=ok` (API↔DB) | Y |
| RP-003 | §6 Availability | S | Med | Stack up | GET /metrics | 200 Prometheus text (`# TYPE`) | Y |
| RP-004 | §6 Security | S | High | none | GET each report, custom report, notifications, targets, rules, templates, products, connections, campaign report with no token | 401 everywhere | Y |
| RP-005 | §6 Security | S | High | none | Same reports with garbage bearer token | 401 | Y |

## §5.8 Dashboards & reports

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-010 | BR-DR-01 | S | High | Fixture | Owner GET /sales-reports/dashboard | role_view SALES_HEAD; each KPI (new leads, SQLs, open SQLs, opps created, open count, open pipeline, weighted, won count/amount, lost, win rate, avg deal, contacts emailed/replied) = computed | Y |
| RP-011 | BR-DR-01 | S | High | Fixture | Same call, `teams` | One row per L2 head (bd1, bd2) with members count and exact team KPIs | Y |
| RP-012 | BR-DR-01, BR-SH-02 | S | High | Fixture | L1 head dashboard | SALES_HEAD, all-team KPIs identical to admin | Y |
| RP-013 | BR-DR-01 | S | High | Fixture | bd1 dashboard | BUSINESS_DEVELOPMENT; KPIs for {bd1, ex1, ex2, mr1} only; team rows = direct reports | Y |
| RP-014 | BR-DR-01 | S | High | Fixture | ex1 dashboard | BUSINESS_EXECUTIVE; KPIs for {ex1, mr1}; queue: own SQL lead L1, own open deals closing ≤ today+30 (incl. overdue), `my` = own figures | Y |
| RP-015 | BR-DR-01 | S | Med | Fixture | Owner dashboard `member=bd2` | KPIs = bd2 team only | Y |
| RP-016 | BR-SH-02 / §6 Security | S | High | Fixture | bd1 `member=ex3`; ex2 `member=ex1` | 404 | Y |
| RP-017 | BR-DR-02 (negative) | S | Low | none | date_from > date_to | 400 | Y |
| RP-018 | BR-DR-02 (edge) | S | Med | Fixture | 30-day period ending yesterday | Period counts 0; open pipeline still the point-in-time total | Y |
| RP-020 | BR-DR-02 | S | High | Fixture | Owner GET /sales-reports/leads | total, SQLs, lead→SQL rate, by stage, by source (leads/SQLs), by owner (leads/SQLs/converted/disqualified), disqualification reasons (No budget ×1, recycling 1) exact | Y |
| RP-021 | BR-DR-02, BR-SH-02 | S | High | Fixture | bd1 leads report | Same, limited to bd1's team | Y |
| RP-022 | BR-DR-02 (edge) | I | Med | 4 leads with created_at backdated (DB) to 31 Aug 23:59:59, 1 Sep 00:00, 30 Sep 23:59:59, 1 Oct 00:00 | Leads report 1–30 Sep | Exactly the 2 inside leads (inclusive whole days) | Y |
| RP-025 | BR-DR-02 | S | High | Fixture | GET /sales-reports/pipeline | Per owner: open count/amount, weighted, won count/amount, lost, win rate exact | Y |
| RP-026 | BR-DR-02 | S | Med | Fixture | Same, by_close_month | Open deals by YYYY-MM, undated in its own row; count/amount/weighted exact | Y |
| RP-027 | BR-DR-02, BR-SP-04 | S | Med | Fixture | `client_type=EXISTING` | Only existing-client open deals | Y |
| RP-028 | BR-DR-02, BR-SP-03 | I | High | Fixture | Compare pipeline report, /pipeline/revenue, /opportunities?status=OPEN | Same open amount, weighted and count | Y |
| RP-030 | BR-DR-03 | S | High | Fixture | Owner GET /sales-reports/funnel | Steps sent→replied→lead→SQL→opportunity→won = 5, 2, 7, 3, 13, 2 | Y |
| RP-031 | BR-DR-03 | S | Med | Fixture | Same | from_previous = count/prev×100, blank where >100 | Y |
| RP-032 | BR-DR-03 (edge) | S | Low | Fixture | Same | from_first also never >100% (consistency with the >100% rule) | Y |
| RP-033 | BR-DR-03, BR-SH-02 | S | Med | Fixture | bd1 funnel | Counts for bd1 team | Y |

## §5.9 Revenue forecasting (FY April–March)

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-040 | BR-FC-01 | S | High | Fixture | GET /sales-reports/forecast?fy=2026 | Each quarter: won, won_count, pipeline, weighted, open_count, lost, lost_count, forecast, best case, commit, categories exact | Y |
| RP-041 | BR-FC-01, §9 | S | High | Fixture | Same | Q1 1 Apr–30 Jun, Q2 1 Jul–30 Sep, Q3 1 Oct–31 Dec, Q4 1 Jan–31 Mar; labels "Qn FY 2026-27"; FY 2026-04-01..2027-03-31 | Y |
| RP-042 | BR-FC-01/02 (boundary) | S | High | D1 31-Mar-26, D2 1-Apr-26, D9 31-Mar-27, D10 1-Apr-27 | fy=2025/2026/2027 | 31 Mar → previous FY Q4; 1 Apr → new FY Q1 | Y |
| RP-043 | BR-FC-02 | S | High | Fixture | Same, `years` + `total` | FY 2025-26..2028-29 buckets and FY total exact | Y |
| RP-044 | BR-FC-01, BR-SP-03 | S | High | Fixture | Same | forecast = won + weighted open; best case = won + full open; won = 45,000 (won ≠ weighted) | Y |
| RP-045 | BR-SF-08 | S | Med | D4 manually set to Commit | Same | Commit 34,000 (incl. override), Best case 7,000, Pipeline 17,500; commit = won + commit deals | Y |
| RP-046 | BR-FC-01 (edge) | S | Med | D11 has no close date | Same | Not in any bucket; open_without_close_date = 1 / 6,000 | Y |
| RP-047 | §5.9 acceptance | I | High | Fixture | For each quarter + FY: GET /opportunities?close_from&close_to | Won/open/weighted/total amount reconcile with the forecast bucket | Y |
| RP-048 | BR-FC-01, BR-SP-04 | S | Med | Fixture | `client_type=EXISTING` | Exact; target null (targets are per rep) | Y |
| RP-049 | BR-FC-01, BR-SH-02 | S | Med | Fixture | Owner `member=bd1` | bd1 team only | Y |
| RP-050 | BR-SH-02 / §6 Security | S | High | Fixture | Forecast as bd1, ex2, ex1, head | Each exactly their hierarchy | Y |
| RP-051 | BR-SF-08/09 | S | Med | Targets | Same | Quarter/FY target and attainment = won/target×100 | Y |
| RP-052 | BR-FC-02 | S | Med | Today 10 Oct 2026 | Forecast without fy | fy = 2026 | Y |
| RP-053 | negative | S | Low | none | fy=abc | 422 | Y |

## §5.10 Team performance

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-060 | BR-TP-01 | S | High | Fixture incl. activities | Owner GET /sales-reports/team-performance | Per rep: activities, calls, meetings, tasks done, emails sent, leads, SQLs, opps, won count/amount, lost, open pipeline, lead→SQL, SQL→opp, win rate exact | Y |
| RP-061 | BR-TP-01 | S | High | Fixture | Same, totals | Sums and conversion rates exact | Y |
| RP-062 | §5.10 acceptance | S | High | Fixture | bd1 | Reps = {bd1, ex1, ex2, mr1} | Y |
| RP-063 | BR-SH-02 | S | Med | Fixture | ex1 (has a report) | {ex1, mr1} | Y |
| RP-064 | §5.10 acceptance | S | High | Fixture | L1 head | All reps | Y |
| RP-065 | §5.10 acceptance (negative) | S | High | Fixture | bd2 `member=ex1`; ex1 `member=bd1` | 404 | Y |
| RP-066 | BR-TP-01 | I | Med | ex1 logged CALL + MEETING, completed a task | Same | calls 1, meetings 1, tasks_done 1, activities 3 | Y |

## §5.11 AI contact discovery

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-070 | BR-AI-01 (C) | S | Low | none | Look for a discovery/enrichment API in /openapi.json | Endpoint exists (else: not implemented) | Y |

## §5.12 Salesforce-style features

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-080 | BR-SF-01 | I | Med | Inbound reply message for a contact | POST /leads/from-message twice | 201 lead linked to contact, contact owner and campaign; second call 409 | Y |
| RP-081 | BR-SF-02 | S | Med | L4 disqualified "No budget", recycle 30 days | GET lead; leads report | Reason kept, recycle_at ≈ +30 days, shown in report | Y |
| RP-082 | BR-SF-03 | S | Med | Round robin over [ex2, ex3] | Create 3 leads on unowned contacts | Owners ex2, ex3, ex2 | Y |
| RP-083 | BR-SF-04 | S | Med | Deal moved Qualification→Negotiation→Proposal | GET opportunity | stage_history in order with times | Y |
| RP-084 | BR-SF-05 | S | Med | none | Create won deal / move to lost without reason | 400 | Y |
| RP-084b | BR-SF-05 | I | Med | stale_deal_days=5; D7 updated_at backdated 10 days (DB) | GET /opportunities?stale=true | D7 (idle) and D2 (past close date) flagged; D8 not | Y |
| RP-107 | BR-SF-05 | I | Med | Stale deal exists | Wait for the hourly worker job | Owner gets one STALE_DEAL notification per week | N (job is hourly; too slow for an automated run; run manually or trigger `sales_jobs.stale_deals`) |
| RP-085 | BR-SF-06 | S | Med | Activities + task on D2 | GET /opportunities/D2/timeline | Shows CALL, MEETING, task | Y |
| RP-086 | BR-SF-07 | S | Med | Leads assigned to ex2 by owner | ex2 GET /notifications; POST read | ASSIGNED alerts present; unread drops by 1 | Y |
| RP-087 | BR-SF-07 | I | Med | ex2 email preference on | Poll DB ≤150 s | Worker email loop stamps emailed_at | Y |
| RP-087b | BR-SF-07 | I | Med | SENDER_EMAIL + mail sink | Check mailbox | Email received | N (no sender/mail sink on local stack; UAT) |
| RP-088 | BR-SF-08 | S | Med | Targets set | ex1 GET /sales/targets | Only ex1 + mr1, quarter amounts | Y |
| RP-089 | BR-SF-09 | S | High | Targets | GET /sales-reports/targets (FY, Q1, Q2) | Per rep target, won, commit, best case, attainment, gap exact; ranked by attainment (ex3 120% > ex2 75%) | Y |
| RP-090 | BR-SF-09 | S | Med | Fixture | GET /sales-reports/leaderboard | Order ex3, ex2, ex1; top won 30,000 | Y |
| RP-091 | BR-SF-10 | S | Med | bd1 template with {{first_name}}, {{full_name}}, {{company_name}}, {{your_name}} | Render for P1; ex3 lists; other tenant renders; ex3 edits | Fields filled; shared in tenant; 404 cross-tenant; 403 edit by non-owner | Y |
| RP-092 | BR-SF-11 | I | Med | Campaign + messages + leads from campaign | GET /sales-reports/campaign-roi | emailed 5, replies 2, leads 2, SQLs 2, opps 1, pipeline 2,500, weighted 250, reply rate 40% | Y |
| RP-093 | BR-SF-12, §7 | S | High | Settings amount_hidden_levels=[4] | mr1 GETs every report, /opportunities, board, /pipeline/revenue, /proposals, /products, deal, targets | No numeric amount/revenue/target/attainment anywhere | Y |
| RP-093b | BR-SF-12 | S | Med | Same | mr1 forecast | Counts still present | Y |
| RP-093c | BR-SF-12 | S | Med | Same | ex1 forecast | Amounts visible | Y |
| RP-094 | BR-SF-12 | S | High | Same | mr1 PATCH amount; run shared money report; quote PDF | 403 each | Y |
| RP-095 | BR-SF-13 | I | High | Rule: DEAL amount > 50,000 → notify owner + manager, task for owner | PATCH amount 60,000 | ex1 and bd1 WORKFLOW alerts; task on deal for ex1 | Y |
| RP-096 | BR-SF-13 | I | Med | Rule: stage equals Negotiation → set next_step | PATCH stage | next_step "Send contract" | Y |
| RP-097 | BR-SF-13 | S | Med | Inactive close-date rule | PATCH close date; bad rule payloads | No alert; run counts 1/1/0; 400 on bad field/threshold | Y |
| RP-098 | BR-SF-14 | S | High | Fixture | POST /reports/custom/run deals sum_amount by stage | Equals independent sums | Y |
| RP-098b | BR-SF-14 | S | Med | Fixture | Matrix owner × status, close date in FY | Exact counts | Y |
| RP-099 | BR-SF-14, BR-SH-02 | S | High | bd1 shares "Deals by owner" | Run as bd1, ex2, ex1; ex1 builds | Each sees own hierarchy only; ex1 (L3 agent) cannot build (403) | Y |
| RP-099b | BR-SF-14 (negative) | S | Low | none | Unknown group/object | 400 | Y |
| RP-100 | BR-SF-15 | S | High | Product 1,200 | Proposal on D8; lines 3×1,200 −10% + 500 | subtotal 4,100, discount 360, amount 3,740; line totals 3,240/500 | Y |
| RP-101 | BR-SF-15 | I | Med | Proposal | GET /proposals/{id}/pdf | Valid PDF (%PDF…%%EOF) | Y |
| RP-101b | BR-SF-15 (negative) | S | Med | none | PDF from other tenant; negative price | 404; 400 | Y |
| RP-102 | BR-SF-16 | S | Low | none | GET /connections; start unknown provider | 200; 404 | Y |
| RP-102b | BR-SF-16 | I | Med | Google/M365 OAuth app + accounts | Connect, sync calendar and mail | Events/emails appear | N (OAuth not configured locally; needs real accounts) |
| RP-103 | BR-SF-17 (C) | S | Low | none | Look for discount approval | Approval route exists | Y (absence recorded) |
| RP-104 | BR-SF-18 (C) | S | Low | none | Look for A/B subject API | Exists | Y (absence recorded) |
| RP-105 | BR-SF-19 (C) | S | Low | none | Look for M365 SSO | Exists | Y (absence recorded) |
| RP-106 | BR-SF-20 (C) | S | Low | none | GET /openapi.json | REST API documented | Y |
| RP-106b | BR-SF-20 (C) | S | Low | none | Look for outbound webhook API | Exists | Y (absence recorded) |

## §6 Security, isolation, roles, audit

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-110 | §6 Security (tenant isolation) | S | Critical | Workspace B | B's admin calls all 9 sales reports with `member=<A user>` | 404 each | Y |
| RP-111 | §6 Security | S | Critical | Both tenants seeded | B's forecast, dashboard, team, ROI, targets, custom report; A's forecast | B sees exactly 99,999 / 77,777 and its own user; A never includes 99,999 | Y |
| RP-112 | §6 Security | S | Critical | Both | B reads A's deal, timeline, saved report, writes A's target; A reads B's deal | 404 | Y |
| RP-113 | §6 Security (hierarchy) | S | High | Fixture | ex2→ex1, ex3→bd2, mr1→ex1 via `member` on every report | 404 | Y |
| RP-114 | §6 Security ("every screen, API and export") | S | High | Fixture | bd2 (L2 MANAGER) GET /reports/dashboard-summary and /reports/campaign-comparison `user_id=<ex1>` | 403/404 | Y |
| RP-115 | §6 Security (roles) | S | High | Fixture | Agent: PUT settings, POST rule, GET rules, POST product, PUT own target; bd1 target for ex3 / ex2 | 403 ×6; bd1→ex2 200 | Y |
| RP-120 | §6 Auditability | I | High | Workflow deal W | PATCH owner, amount, close date, stage in one call | 4 history rows with old/new, user name, timestamp | Y |
| RP-121 | §6 Auditability | S | Med | L3 | Change lead owner | History row with user | Y |

## §6 Performance, scale, rate limiting

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-130 | §6 Performance (dashboards < 3 s) | S | High | Fixture | Each report ×3 | Worst < 3 s | Y |
| RP-131 | §6 Scale (20+ concurrent users) | S | High | Fixture | 25 threads × 60 s mixed reads (12 endpoints, 8 users), 0.2 s think time, each thread its own client address | 0% errors, p95 < 3 s; report p50/p95 | Y |
| RP-140 | §6 Security (login rate limit) | S | High | Dedicated user | 10 wrong passwords then the right one | 11th → 429, right password also 429, Retry-After ≈ 900 s | Y (dedicated user, isolated client address) |
| RP-141 | §6 Security | S | Med | none | 61 logins (unknown emails) from one client address | 60× 401 then 429 | Y (isolated address) |
| RP-142 | §6 Security | S | Med | After RP-141 | Same client, new X-Forwarded-For | Still limited (429) | Y |

## §8 KPIs (measurable behaviour)

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-160 | §8 Lead-to-SQL | S | Med | Fixture | Leads report | lead_to_sql_rate = SQLs/leads×100 | Y |
| RP-161 | §8 SQL-to-win, forecast accuracy | S | Low | Fixture | Look for these ratios in reports | Reported (else derivable only) | Y (absence recorded) |

## UI (Playwright, Chromium)

| ID | BRD ref | Type | Pri | Preconditions | Steps | Expected | Auto |
|---|---|---|---|---|---|---|---|
| RP-150 | §5.8 | I | High | Fixture | Log in as owner via /vector/login | Lands in /app/ | Y |
| RP-151 | BR-DR-01 | I | High | Fixture | /app/sales | Tiles show API open pipeline, won, counts, SQLs | Y |
| RP-152 | BR-FC-01/02 | I | High | Fixture | /app/sales-reports?tab=forecast | Quarter labels, quarter forecasts, FY forecast/won/weighted, undated count = API | Y |
| RP-153 | BR-TP-01 | I | Med | Fixture | tab=team | Every rep name, won amounts, total won, total leads = API | Y |
| RP-154 | BR-SF-09 | I | Med | Fixture | tab=targets | Targets and top-ranked rep = API | Y |
| RP-155 | §6 | I | Med | Fixture | Visit all tabs (leads, pipeline, roi, custom too) | No page errors, no 5xx | Y |

## Manual-only cases

| ID | BRD ref | Type | Pri | Steps | Expected | Why manual |
|---|---|---|---|---|---|---|
| RP-M01 | §6 Usability (≤3 clicks) | S | Med | From a deal, move stage, log activity, convert an SQL | ≤ 3 clicks each | Click-count judgement in the real UI |
| RP-M02 | §6 Availability 99.5% in IST/US business hours | S | Med | Synthetic monitor on /health/ready for a month | ≥ 99.5% | Needs production monitoring |
| RP-M03 | §6 Performance, 100k contacts | S | Med | Load 100k contacts, time list/search | < 1 s | Volume test owned by the contacts suite |
