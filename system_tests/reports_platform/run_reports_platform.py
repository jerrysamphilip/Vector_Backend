#!/usr/bin/env python3
"""BRD v2.0 system / integration suite: 5.8 Dashboards & Reports, 5.9 Forecasting, 5.10 Team performance,
5.11 AI discovery, 5.12 Salesforce-style features, 6 platform NFRs, 8 KPIs.

Runs against the live local stack (API_URL, WEB_URL). Creates its own workspaces; every expected number is
computed here from the data this script seeds, then compared with what the API returns.

    python3 system_tests/reports_platform/run_reports_platform.py [--skip-ui] [--skip-load] [--skip-ratelimit]
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from common import API, WEB, Results, req, sql  # noqa: E402
from rp_helpers import (add_user, diff_dict, feq, fake_ip, fq_of, fy_of, leaked_money, make_workspace,  # noqa: E402
                        pct, quarter_range, xff_headers)

R = Results("reports_platform", HERE)
ARGS = set(sys.argv[1:])
TODAY = date.today()
CUR_FY = fy_of(TODAY)
CTX = {}


def j(x):
    return json.dumps(x, default=str)[:400]


def get(tok, path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return req("GET", path + (("?" + q) if q else ""), token=tok)


def safe(case_id, title):
    """Decorator: an unexpected exception in a block is recorded against its first case, not fatal."""
    def wrap(fn):
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                R.record(case_id, title + " (block error)", False, f"{type(e).__name__}: {e}", "test-issue")
        return inner
    return wrap


# ═══════════════════════════════════════════════════════════════
# Setup and seed
# ═══════════════════════════════════════════════════════════════

def setup():
    ws = make_workspace("rp")
    CTX["ws"] = ws
    U = {"owner": {"user_id": ws.user_id, "token": ws.token, "email": ws.email}}
    U["head"] = add_user(ws, "MANAGER", "Hana", sales_level=1)
    U["bd1"] = add_user(ws, "MANAGER", "Bdan", sales_level=2, manager_id=U["head"]["user_id"])
    U["bd2"] = add_user(ws, "MANAGER", "Bree", sales_level=2, manager_id=U["head"]["user_id"])
    U["ex1"] = add_user(ws, "AGENT", "Exa", sales_level=3, manager_id=U["bd1"]["user_id"])
    U["ex2"] = add_user(ws, "AGENT", "Exb", sales_level=3, manager_id=U["bd1"]["user_id"])
    U["ex3"] = add_user(ws, "AGENT", "Exc", sales_level=3, manager_id=U["bd2"]["user_id"])
    U["mr1"] = add_user(ws, "AGENT", "Mira", sales_level=4, manager_id=U["ex1"]["user_id"])
    CTX["U"] = U
    CTX["uid"] = {k: v["user_id"] for k, v in U.items()}
    CTX["name_of"] = {v["user_id"]: k for k, v in U.items()}
    uid = CTX["uid"]
    CTX["team"] = {  # owner keys each viewer may see (BR-SH-02), written out by hand
        "owner": None, "head": None,
        "bd1": {"bd1", "ex1", "ex2", "mr1"}, "bd2": {"bd2", "ex3"},
        "ex1": {"ex1", "mr1"}, "ex2": {"ex2"}, "ex3": {"ex3"}, "mr1": {"mr1"},
    }
    CTX["team_ids"] = {k: (None if v is None else {uid[x] for x in v}) for k, v in CTX["team"].items()}
    # Second tenant for isolation checks
    ws2 = make_workspace("rp-other")
    CTX["ws2"] = ws2


def seed():
    ws, U, uid = CTX["ws"], CTX["U"], CTX["uid"]
    tok = ws.token
    s, stages = get(tok, "/sales/stages")
    assert s == 200, (s, stages)
    stages = stages if isinstance(stages, list) else stages.get("stages") or stages.get("items")
    ST = {x["name"]: x for x in stages}
    CTX["ST"] = ST
    PROB = {n: x["probability"] for n, x in ST.items()}
    CTX["PROB"] = PROB

    # Contacts P1..P7 (owners match the leads they will carry)
    contacts = {}
    for i, owner in enumerate(["ex1", "ex1", "ex2", "ex2", "ex3", "bd1", "ex3"], 1):
        s, b = req("POST", "/contacts", {"email": f"p{i}-{ws.rid}@acme{i}.example", "first_name": f"Pat{i}",
                                         "last_name": "Contact", "company_name": f"Acme{i} Ltd",
                                         "owner_id": uid[owner]}, tok)
        assert s == 201, (s, b)
        contacts[f"P{i}"] = b["prospect_id"]
    CTX["P"] = contacts

    # A finished campaign that emailed P1..P5; P1 and P3 replied (funnel + campaign ROI)
    cid = sql("SELECT UUID()")
    sql(f"INSERT INTO campaigns (campaign_id, tenant_id, campaign_name, created_by, status, created_at) VALUES "
        f"('{cid}', '{ws.tenant_id}', 'RP ROI campaign {ws.rid}', '{ws.user_id}', 'COMPLETED', NOW())")
    CTX["campaign_id"] = cid
    msg_ids = {}
    for i in range(1, 6):
        mid = sql("SELECT UUID()")
        sql(f"INSERT INTO email_messages (message_id, campaign_id, prospect_id, to_email, direction, status, sent_at) "
            f"VALUES ('{mid}', '{cid}', '{contacts[f'P{i}']}', 'p{i}-{ws.rid}@acme{i}.example', 'OUTBOUND', 'SENT', NOW())")
    for i in (1, 3):
        mid = sql("SELECT UUID()")
        msg_ids[f"P{i}"] = mid
        sql(f"INSERT INTO email_messages (message_id, campaign_id, prospect_id, to_email, direction, status, sent_at) "
            f"VALUES ('{mid}', '{cid}', '{contacts[f'P{i}']}', 'rep@systest.example', 'INBOUND', 'RECEIVED', NOW())")
    CTX["inbound"] = msg_ids

    # Leads L1..L7: (contact, owner, source, final stage)
    plan = [("P1", "ex1", "Webinar", "SQL", cid), ("P2", "ex1", "Webinar", "CONVERTED", cid),
            ("P3", "ex2", "Referral", "NEW", None), ("P4", "ex2", "Referral", "DISQUALIFIED", None),
            ("P5", "ex3", "Webinar", "CONTACTED", None), ("P6", "bd1", "Cold email", "ENGAGED", None),
            ("P7", "ex3", "Cold email", "SQL", None)]
    leads = {}
    for n, (p, owner, source, final, camp) in enumerate(plan, 1):
        body = {"prospect_id": contacts[p], "owner_id": uid[owner], "source": source}
        if camp:
            body["campaign_id"] = camp
        s, b = req("POST", "/leads", body, tok)
        assert s == 201, (s, b)
        lid = b["lead_id"]
        leads[f"L{n}"] = lid
        if final in ("SQL", "CONVERTED"):
            s, b = req("PATCH", f"/leads/{lid}", {"qualification": {"budget": True, "authority": True, "need": True,
                                                                    "timeline": True}, "stage": "SQL"}, tok)
            assert s == 200 and b["stage"] == "SQL", (s, b)
        elif final == "DISQUALIFIED":
            s, b = req("PATCH", f"/leads/{lid}", {"disqualified_reason": "No budget", "stage": "DISQUALIFIED",
                                                  "recycle_in_days": 30}, tok)
            assert s == 200, (s, b)
        elif final != "NEW":
            s, b = req("PATCH", f"/leads/{lid}", {"stage": final}, tok)
            assert s == 200, (s, b)
    CTX["L"] = leads
    CTX["lead_plan"] = plan

    # Opportunities: (key, owner, amount, stage, close_date, client_type)
    D = [("D1", "ex1", 10000, "Proposal", date(2026, 3, 31), "NEW"),
         ("D2", "ex1", 20000, "Negotiation", date(2026, 4, 1), "NEW"),
         ("D3", "ex2", 15000, "Closed won", date(2026, 6, 30), "EXISTING"),
         ("D4", "ex2", 5000, "Qualification", date(2026, 7, 1), "NEW"),
         ("D5", "ex3", 30000, "Closed won", date(2026, 9, 30), "NEW"),
         ("D6", "ex3", 8000, "Closed lost", date(2026, 10, 15), "NEW"),
         ("D7", "bd1", 12000, "Needs analysis", date(2026, 12, 31), "NEW"),
         ("D8", "ex1", 7000, "Proposal", date(2027, 1, 1), "NEW"),
         ("D9", "bd2", 9000, "Negotiation", date(2027, 3, 31), "EXISTING"),
         ("D10", "ex3", 4000, "Proposal", date(2027, 4, 1), "NEW"),
         ("D11", "ex2", 6000, "Needs analysis", None, "NEW"),
         ("D12", "mr1", 3000, "Qualification", date(2026, 11, 15), "EXISTING")]
    deals = []
    ids = {}
    for key, owner, amount, stage, close, ctype in D:
        body = {"name": f"RP {key} {ws.rid}", "owner_id": uid[owner], "amount": amount,
                "stage_id": ST[stage]["stage_id"], "client_type": ctype}
        if close:
            body["close_date"] = close.isoformat()
        if stage.startswith("Closed"):
            body["closed_reason"] = "Price" if stage == "Closed lost" else "Best product fit"
        s, b = req("POST", "/opportunities", body, tok)
        assert s == 201, (key, s, b)
        ids[key] = b["opportunity_id"]
        deals.append({"key": key, "owner": owner, "amount": float(amount), "stage": stage, "close": close,
                      "ctype": ctype, "manual_cat": None})
    # Manual forecast category on D4 (Qualification -> Commit) (BR-SF-08)
    s, b = req("PATCH", f"/opportunities/{ids['D4']}", {"forecast_category": "COMMIT"}, tok)
    assert s == 200, (s, b)
    deals[3]["manual_cat"] = "COMMIT"

    # Convert L2 (SQL) -> opportunity CONV (ex1, 2500, Qualification, 2026-08-15)
    s, b = req("POST", f"/leads/{leads['L2']}/convert", {"amount": 2500, "close_date": "2026-08-15",
                                                          "stage_id": ST["Qualification"]["stage_id"]}, tok)
    assert s == 201, (s, b)
    ids["CONV"] = b["opportunity_id"]
    CTX["conv_campaign"] = b.get("campaign_id")
    deals.append({"key": "CONV", "owner": "ex1", "amount": 2500.0, "stage": "Qualification",
                  "close": date(2026, 8, 15), "ctype": b.get("client_type") or "NEW", "manual_cat": None})
    for d in deals:
        d["status"] = "WON" if d["stage"] == "Closed won" else "LOST" if d["stage"] == "Closed lost" else "OPEN"
        d["prob"] = PROB[d["stage"]]
        p = d["prob"]
        d["cat"] = d["manual_cat"] or ("CLOSED" if d["status"] == "WON" else "OMITTED" if d["status"] == "LOST"
                                       else "COMMIT" if p >= 70 else "BEST_CASE" if p >= 40 else "PIPELINE")
        d["id"] = ids[d["key"]]
    CTX["deals"] = deals
    CTX["D"] = ids

    # Activities: ex1 logs a CALL and a MEETING on D2, ex2 a NOTE on D3; ex1 completes one task
    for who, oid, typ in (("ex1", "D2", "CALL"), ("ex1", "D2", "MEETING"), ("ex2", "D3", "NOTE")):
        s, b = req("POST", f"/opportunities/{ids[oid]}/activities", {"activity_type": typ, "subject": f"{typ} log"},
                   U[who]["token"])
        assert s == 201, (who, s, b)
    s, b = req("POST", "/tasks", {"title": "Send deck", "opportunity_id": ids["D2"], "owner_id": uid["ex1"]},
               U["ex1"]["token"])
    assert s in (200, 201), (s, b)
    s2, b2 = req("PATCH", f"/tasks/{b['task_id']}", {"status": "DONE"}, U["ex1"]["token"])
    assert s2 == 200, (s2, b2)
    CTX["activity"] = {"ex1": {"activities": 3, "calls": 1, "meetings": 1, "tasks_done": 1},
                       "ex2": {"activities": 1, "calls": 0, "meetings": 0, "tasks_done": 0}}

    # Targets FY current: (who, quarter, amount)
    T = [("ex1", 1, 10000), ("ex1", 2, 10000), ("ex2", 1, 20000), ("ex3", 2, 25000), ("bd1", 3, 5000)]
    for who, q, amt in T:
        s, b = req("PUT", "/sales/targets", {"user_id": uid[who], "fy": 2026, "quarter": q, "amount": amt}, tok)
        assert s == 200, (s, b)
    CTX["targets"] = T

    # Other tenant: one won deal of 99,999 and a target, so leaks would show up in exact totals
    ws2 = CTX["ws2"]
    s, st2 = get(ws2.token, "/sales/stages")
    st2 = st2 if isinstance(st2, list) else st2.get("stages") or st2.get("items")
    won2 = next(x for x in st2 if x["is_won"])
    s, b = req("POST", "/opportunities", {"name": "Other tenant deal", "amount": 99999, "stage_id": won2["stage_id"],
                                          "close_date": "2026-05-05", "closed_reason": "Price"}, ws2.token)
    assert s == 201, (s, b)
    CTX["other_opp"] = b["opportunity_id"]
    req("PUT", "/sales/targets", {"user_id": ws2.user_id, "fy": 2026, "quarter": 1, "amount": 77777}, ws2.token)


# ═══════════════════════════════════════════════════════════════
# Independent expectations
# ═══════════════════════════════════════════════════════════════

def visible_deals(viewer, member=None):
    team = CTX["team"][viewer]
    rows = [d for d in CTX["deals"] if team is None or d["owner"] in team]
    if member:
        mt = CTX["team"].get(member) or {member}
        if member in ("owner", "head"):
            mt = set(CTX["team"]["bd1"]) | set(CTX["team"]["bd2"]) | {"head", "owner"}
        rows = [d for d in rows if d["owner"] in mt]
    return rows


def exp_bucket(deals, start, end):
    b = {"won": 0.0, "won_count": 0, "pipeline": 0.0, "weighted": 0.0, "open_count": 0, "lost": 0.0, "lost_count": 0,
         "categories": {"COMMIT": 0.0, "BEST_CASE": 0.0, "PIPELINE": 0.0, "OMITTED": 0.0}}
    for d in deals:
        if not d["close"] or not (start <= d["close"] <= end):
            continue
        if d["status"] == "WON":
            b["won"] += d["amount"]
            b["won_count"] += 1
        elif d["status"] == "LOST":
            b["lost"] += d["amount"]
            b["lost_count"] += 1
        else:
            b["pipeline"] += d["amount"]
            b["weighted"] += d["amount"] * d["prob"] / 100
            b["open_count"] += 1
            b["categories"][d["cat"] if d["cat"] in b["categories"] else "PIPELINE"] += d["amount"]
    b["forecast"] = b["won"] + b["weighted"]
    b["best_case"] = b["won"] + b["pipeline"]
    b["commit"] = b["won"] + b["categories"]["COMMIT"]
    b["best_case_category"] = b["commit"] + b["categories"]["BEST_CASE"]
    b = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in b.items()}
    b["categories"] = {k: round(v, 2) for k, v in b["categories"].items()}
    return b


def exp_measures(owner_keys):
    """Dashboard KPIs for owners in owner_keys (None = all); every record was created today."""
    deals = [d for d in CTX["deals"] if owner_keys is None or d["owner"] in owner_keys]
    plan = [p for p in CTX["lead_plan"] if owner_keys is None or p[1] in owner_keys]
    won = [d for d in deals if d["status"] == "WON"]
    lost = [d for d in deals if d["status"] == "LOST"]
    open_ = [d for d in deals if d["status"] == "OPEN"]
    won_amt = sum(d["amount"] for d in won)
    return {"new_leads": len(plan), "sqls": sum(1 for p in plan if p[3] in ("SQL", "CONVERTED")),
            "open_sqls": sum(1 for p in plan if p[3] == "SQL"), "opportunities_created": len(deals),
            "open_opportunities": len(open_), "open_pipeline": round(sum(d["amount"] for d in open_), 2),
            "weighted_pipeline": round(sum(d["amount"] * d["prob"] / 100 for d in open_), 2),
            "won_count": len(won), "won_amount": round(won_amt, 2), "lost_count": len(lost),
            "win_rate": pct(len(won), len(won) + len(lost)),
            "average_deal": round(won_amt / len(won), 2) if won else None}


def exp_email(owner_keys):
    owners = {"P1": "ex1", "P2": "ex1", "P3": "ex2", "P4": "ex2", "P5": "ex3"}
    sent = [p for p, o in owners.items() if owner_keys is None or o in owner_keys]
    replied = [p for p in ("P1", "P3") if owner_keys is None or owners[p] in owner_keys]
    return {"contacts_emailed": len(sent), "contacts_replied": len(replied)}


def ids_of(keys):
    return {CTX["uid"][k] for k in keys}


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

REPORTS = ["/sales-reports/dashboard", "/sales-reports/funnel", "/sales-reports/leads", "/sales-reports/pipeline",
           "/sales-reports/forecast", "/sales-reports/team-performance", "/sales-reports/targets",
           "/sales-reports/leaderboard", "/sales-reports/campaign-roi"]


@safe("RP-001", "Health")
def t_health():
    t0 = time.time()
    s, b = req("GET", "/health")
    R.check("RP-001", "GET /health returns 200 healthy", s == 200 and isinstance(b, dict) and b.get("status") == "healthy",
            f"{s} {j(b)}")
    s, b = req("GET", "/health/ready")
    R.check("RP-002", "GET /health/ready reports database ok", s == 200 and b.get("database") == "ok", f"{s} {j(b)}")
    s, b = req("GET", "/metrics", raw=True)
    txt = b.decode(errors="ignore") if isinstance(b, bytes) else str(b)
    R.check("RP-003", "GET /metrics serves Prometheus exposition", s == 200 and "# TYPE" in txt, f"{s} {txt[:120]}")
    CTX["health_ms"] = round((time.time() - t0) * 1000)


@safe("RP-004", "Auth required")
def t_auth_required():
    paths = REPORTS + ["/reports/custom", "/reports/custom/meta", "/notifications", "/sales/targets", "/workflow/rules",
                       "/template-library", "/products", "/connections", "/reports/dashboard-summary"]
    bad = [f"{p}:{s}" for p in paths for s, _ in [req("GET", p)] if s != 401]
    R.check("RP-004", "Every report/platform endpoint rejects anonymous calls with 401", not bad, ", ".join(bad))
    bad = [f"{p}:{s}" for p in REPORTS for s, _ in [req("GET", p, token="not.a.jwt")] if s != 401]
    R.check("RP-005", "A forged/garbage bearer token is rejected (401) on every report", not bad, ", ".join(bad))


@safe("RP-010", "Dashboards")
def t_dashboard():
    U = CTX["U"]
    expected_all = {**exp_measures(None), **exp_email(None)}
    s, b = get(U["owner"]["token"], "/sales-reports/dashboard")
    ok = s == 200
    CTX["dash_owner"] = b
    d = diff_dict(b.get("kpis", {}), expected_all) if ok else [f"{s} {j(b)}"]
    R.check("RP-010", "Owner dashboard: role view SALES_HEAD and every KPI equals independently computed value",
            ok and b.get("role_view") == "SALES_HEAD" and not d, f"role_view={b.get('role_view') if ok else ''} {d}")
    if ok:
        teams = {t["user_id"]: t for t in b.get("teams", [])}
        errs = []
        for head in ("bd1", "bd2"):
            t = teams.get(CTX["uid"][head])
            if not t:
                errs.append(f"missing team {head}")
                continue
            errs += [f"{head}.{e}" for e in diff_dict(t, {"members": len(CTX["team"][head]), **exp_measures(CTX["team"][head])})]
        if set(teams) != ids_of(["bd1", "bd2"]):
            errs.append(f"team heads {[CTX['name_of'].get(x) for x in teams]}")
        R.check("RP-011", "Sales-head dashboard breaks down by Business Development teams with exact per-team KPIs",
                not errs, "; ".join(errs))
    s, b = get(U["head"]["token"], "/sales-reports/dashboard")
    d = diff_dict(b.get("kpis", {}), expected_all) if s == 200 else [f"{s}"]
    R.check("RP-012", "Level 1 (Sales Head) dashboard covers all teams (same KPIs as admin)",
            s == 200 and b.get("role_view") == "SALES_HEAD" and not d, f"{b.get('role_view')} {d}")
    exp = {**exp_measures(CTX["team"]["bd1"]), **exp_email(CTX["team"]["bd1"])}
    s, b = get(U["bd1"]["token"], "/sales-reports/dashboard")
    d = diff_dict(b.get("kpis", {}), exp) if s == 200 else [f"{s}"]
    teams = {t["user_id"] for t in b.get("teams", [])} if s == 200 else set()
    R.check("RP-013", "Business Development dashboard: role view BUSINESS_DEVELOPMENT, KPIs limited to own team",
            s == 200 and b.get("role_view") == "BUSINESS_DEVELOPMENT" and not d and teams == ids_of(["ex1", "ex2"]),
            f"{b.get('role_view')} {d} teams={[CTX['name_of'].get(x) for x in teams]}")
    s, b = get(U["ex1"]["token"], "/sales-reports/dashboard")
    exp = {**exp_measures(CTX["team"]["ex1"]), **exp_email(CTX["team"]["ex1"])}
    d = diff_dict(b.get("kpis", {}), exp) if s == 200 else [f"{s}"]
    errs = list(d)
    if s == 200:
        q = b.get("queue", {})
        closing = {x["opportunity_id"] for x in q.get("deals_closing", [])}
        exp_closing = {x["id"] for x in CTX["deals"] if x["owner"] == "ex1" and x["status"] == "OPEN" and x["close"]
                       and x["close"] <= TODAY + timedelta(days=30)}
        if closing != exp_closing:
            errs.append(f"deals_closing {len(closing)} vs {len(exp_closing)}")
        sqlq = {x["lead_id"] for x in q.get("sql_leads", [])}
        if sqlq != {CTX["L"]["L1"]}:
            errs.append(f"sql_leads {sqlq}")
        errs += [f"my.{e}" for e in diff_dict(q.get("my", {}), exp_measures({"ex1"}))]
    R.check("RP-014", "Business Executive dashboard: own+report KPIs and work queue (SQLs, deals closing in 30 days)",
            s == 200 and b.get("role_view") == "BUSINESS_EXECUTIVE" and not errs, f"{b.get('role_view')} {errs}")
    s, b = get(U["owner"]["token"], "/sales-reports/dashboard", member=CTX["uid"]["bd2"])
    d = diff_dict(b.get("kpis", {}), {**exp_measures(CTX["team"]["bd2"]), **exp_email(CTX["team"]["bd2"])}) if s == 200 else [s]
    R.check("RP-015", "Dashboard member filter narrows to that person and their team", s == 200 and not d, d)
    s1, _ = get(U["bd1"]["token"], "/sales-reports/dashboard", member=CTX["uid"]["ex3"])
    s2, _ = get(U["ex2"]["token"], "/sales-reports/dashboard", member=CTX["uid"]["ex1"])
    R.check("RP-016", "Member filter outside the caller's hierarchy is refused (404)", s1 == 404 and s2 == 404,
            f"bd1->ex3 {s1}, ex2->ex1 {s2}")
    s, b = get(U["owner"]["token"], "/sales-reports/dashboard", date_from="2026-10-05", date_to="2026-10-01")
    R.check("RP-017", "date_from after date_to is rejected with 400", s == 400, f"{s} {j(b)}")
    y = TODAY - timedelta(days=1)
    s, b = get(U["owner"]["token"], "/sales-reports/dashboard", date_from=(y - timedelta(days=30)).isoformat(),
               date_to=y.isoformat())
    k = b.get("kpis", {}) if s == 200 else {}
    zero = all(k.get(x) == 0 for x in ("new_leads", "sqls", "opportunities_created", "won_count", "lost_count",
                                       "contacts_emailed", "contacts_replied"))
    # Open pipeline is a point-in-time figure, not period bound
    R.check("RP-018", "A period before any activity shows zero period counts (open pipeline stays point-in-time)",
            s == 200 and zero and feq(k.get("open_pipeline"), exp_measures(None)["open_pipeline"]), j(k))


@safe("RP-020", "Leads report")
def t_leads_report():
    U = CTX["U"]

    def expected(keys):
        plan = [p for p in CTX["lead_plan"] if keys is None or p[1] in keys]
        by_stage = {s: 0 for s in ("NEW", "CONTACTED", "ENGAGED", "SQL", "CONVERTED", "DISQUALIFIED")}
        src, own = {}, {}
        for p in plan:
            by_stage[p[3]] += 1
            q = p[3] in ("SQL", "CONVERTED")
            src.setdefault(p[2], [0, 0])
            src[p[2]][0] += 1
            src[p[2]][1] += q
            o = own.setdefault(p[1], [0, 0, 0, 0])
            o[0] += 1
            o[1] += q
            o[2] += p[3] == "CONVERTED"
            o[3] += p[3] == "DISQUALIFIED"
        sqls = sum(1 for p in plan if p[3] in ("SQL", "CONVERTED"))
        return len(plan), sqls, by_stage, src, own

    for case, viewer in (("RP-020", "owner"), ("RP-021", "bd1")):
        total, sqls, by_stage, src, own = expected(CTX["team"][viewer])
        s, b = get(U[viewer]["token"], "/sales-reports/leads")
        errs = [] if s == 200 else [f"{s} {j(b)}"]
        if s == 200:
            if b.get("total") != total or b.get("sqls") != sqls:
                errs.append(f"total {b.get('total')}/{total} sqls {b.get('sqls')}/{sqls}")
            if not feq(b.get("lead_to_sql_rate"), pct(sqls, total)):
                errs.append(f"rate {b.get('lead_to_sql_rate')} vs {pct(sqls, total)}")
            got_stage = {x["stage"]: x["count"] for x in b.get("by_stage", [])}
            if got_stage != by_stage:
                errs.append(f"by_stage {got_stage}")
            got_src = {x["source"]: [x["leads"], x["sqls"]] for x in b.get("by_source", [])}
            if got_src != {k: v for k, v in src.items()}:
                errs.append(f"by_source {got_src} vs {src}")
            got_own = {CTX["name_of"].get(x["owner_id"]): [x["leads"], x["sqls"], x["converted"], x["disqualified"]]
                       for x in b.get("by_owner", [])}
            if got_own != own:
                errs.append(f"by_owner {got_own} vs {own}")
            if viewer == "owner":
                dq = b.get("disqualified_reasons", [])
                if dq != [{"reason": "No budget", "count": 1, "recycling": 1}]:
                    errs.append(f"disqualified_reasons {dq}")
        R.check(case, f"Leads & SQL report for {viewer}: totals, stage, source, owner and lead-to-SQL rate exact",
                not errs, "; ".join(map(str, errs)))
    # KPI (8): Lead-to-SQL conversion = SQLs / leads x 100
    total, sqls, *_ = expected(None)
    s, b = get(U["owner"]["token"], "/sales-reports/leads")
    R.check("RP-160", "KPI Lead-to-SQL conversion = SQLs / leads x 100 (reported value matches formula)",
            s == 200 and feq(b.get("lead_to_sql_rate"), round(100.0 * sqls / total, 1)),
            f"{b.get('lead_to_sql_rate')} vs {round(100.0 * sqls / total, 1)}")


@safe("RP-025", "Pipeline report")
def t_pipeline_report():
    U = CTX["U"]
    s, b = get(U["owner"]["token"], "/sales-reports/pipeline")
    errs = [] if s == 200 else [f"{s}"]
    exp_owner = {}
    months = {}
    for d in CTX["deals"]:
        o = exp_owner.setdefault(d["owner"], {"open_count": 0, "open_amount": 0.0, "weighted": 0.0, "won_count": 0,
                                              "won_amount": 0.0, "lost_count": 0})
        if d["status"] == "OPEN":
            o["open_count"] += 1
            o["open_amount"] += d["amount"]
            o["weighted"] += d["amount"] * d["prob"] / 100
            m = months.setdefault(d["close"].strftime("%Y-%m") if d["close"] else None,
                                  {"count": 0, "amount": 0.0, "weighted": 0.0})
            m["count"] += 1
            m["amount"] += d["amount"]
            m["weighted"] += d["amount"] * d["prob"] / 100
        elif d["status"] == "WON":
            o["won_count"] += 1
            o["won_amount"] += d["amount"]
        else:
            o["lost_count"] += 1
    for o in exp_owner.values():
        o["win_rate"] = pct(o["won_count"], o["won_count"] + o["lost_count"])
    if s == 200:
        got = {CTX["name_of"].get(r["owner_id"]): r for r in b.get("by_owner", [])}
        if set(got) != set(exp_owner):
            errs.append(f"owners {sorted(got)} vs {sorted(exp_owner)}")
        for k, v in exp_owner.items():
            errs += [f"{k}.{e}" for e in diff_dict(got.get(k, {}), {kk: (round(vv, 2) if isinstance(vv, float) else vv)
                                                                    for kk, vv in v.items()})]
    R.check("RP-025", "Pipeline report by owner: open count/amount, weighted, won, lost, win rate exact", not errs,
            "; ".join(errs))
    errs2 = []
    if s == 200:
        got_m = {r["month"]: r for r in b.get("by_close_month", [])}
        if set(got_m) != set(months):
            errs2.append(f"months {sorted(map(str, got_m))} vs {sorted(map(str, months))}")
        for m, v in months.items():
            errs2 += [f"{m}.{e}" for e in diff_dict(got_m.get(m, {}), {k: (round(x, 2) if isinstance(x, float) else x)
                                                                      for k, x in v.items()})]
    R.check("RP-026", "Pipeline report by expected close month (open deals; undated grouped separately) exact",
            s == 200 and not errs2, "; ".join(errs2))
    s, b = get(U["owner"]["token"], "/sales-reports/pipeline", client_type="EXISTING")
    exp_n = sum(1 for d in CTX["deals"] if d["ctype"] == "EXISTING" and d["status"] == "OPEN")
    exp_a = sum(d["amount"] for d in CTX["deals"] if d["ctype"] == "EXISTING" and d["status"] == "OPEN")
    got_n = sum(r["open_count"] for r in b.get("by_owner", [])) if s == 200 else None
    got_a = sum(r["open_amount"] for r in b.get("by_owner", [])) if s == 200 else None
    R.check("RP-027", "Pipeline report client-type filter (existing clients) exact", s == 200 and got_n == exp_n and
            feq(got_a, exp_a), f"{got_n}/{exp_n} {got_a}/{exp_a}")
    # Reconcile with /pipeline/revenue and /opportunities (BR-SP-03 acceptance, same data)
    s1, rev = get(U["owner"]["token"], "/pipeline/revenue")
    s2, lst = get(U["owner"]["token"], "/opportunities", status="OPEN", page_size=500)
    open_amt = sum(d["amount"] for d in CTX["deals"] if d["status"] == "OPEN")
    open_w = sum(d["amount"] * d["prob"] / 100 for d in CTX["deals"] if d["status"] == "OPEN")
    tot = (rev or {}).get("totals", {}) if s1 == 200 else {}
    R.check("RP-028", "Pipeline totals reconcile: report = /pipeline/revenue = sum of /opportunities (open)",
            s1 == 200 and s2 == 200 and feq(tot.get("open_amount"), open_amt) and feq(tot.get("weighted_amount"), open_w)
            and feq(lst.get("total_amount"), open_amt) and lst.get("total") == sum(1 for d in CTX["deals"] if d["status"] == "OPEN"),
            f"rev={j(tot)} list={lst.get('total') if isinstance(lst, dict) else lst}/{lst.get('total_amount') if isinstance(lst, dict) else ''} exp {open_amt}/{open_w}")


@safe("RP-030", "Funnel")
def t_funnel():
    U = CTX["U"]
    for case, viewer in (("RP-030", "owner"), ("RP-033", "bd1")):
        keys = CTX["team"][viewer]
        m, e = exp_measures(keys), exp_email(keys)
        counts = [e["contacts_emailed"], e["contacts_replied"], m["new_leads"], m["sqls"], m["opportunities_created"],
                  m["won_count"]]
        s, b = get(U[viewer]["token"], "/sales-reports/funnel")
        got = [x["count"] for x in b.get("stages", [])] if s == 200 else None
        keys_ok = s == 200 and [x["key"] for x in b["stages"]] == ["sent", "replied", "lead", "sql", "opportunity", "won"]
        R.check(case, f"Funnel sent>replied>lead>SQL>opportunity>won counts exact ({viewer})", keys_ok and got == counts,
                f"{got} vs {counts}")
        if viewer == "owner" and s == 200:
            CTX["funnel_owner"] = b
            errs = []
            for i, st in enumerate(b["stages"]):
                prev = counts[i - 1] if i else None
                exp = None if i == 0 or not prev else pct(counts[i], prev)
                exp = exp if exp is not None and exp <= 100 else None
                if st.get("from_previous") != exp:
                    errs.append(f"{st['key']} from_previous {st.get('from_previous')} vs {exp}")
            if not feq(b.get("won_amount"), m["won_amount"]):
                errs.append(f"won_amount {b.get('won_amount')}")
            R.check("RP-031", "Funnel step conversion = count / previous step x 100 (none shown above 100%)", not errs,
                    "; ".join(errs))
            over = [f"{st['key']}={st.get('from_first')}" for st in b["stages"] if (st.get("from_first") or 0) > 100]
            R.check("RP-032", "Funnel 'from first step' percentages stay within 0-100% (same rule as step ratios)",
                    not over, "from_first above 100%: " + ", ".join(over) +
                    " (app/routers/sales_reports_router.py funnel(): from_previous is capped at 100 but from_first is not)")


@safe("RP-040", "Forecast")
def t_forecast():
    U = CTX["U"]
    tok = U["owner"]["token"]
    s, b = get(tok, "/sales-reports/forecast", fy=2026)
    assert s == 200, (s, b)
    CTX["forecast_owner"] = b
    deals = visible_deals("owner")
    errs, rng_errs = [], []
    for q in range(1, 5):
        start, end = quarter_range(2026, q)
        got = b["quarters"][q - 1]
        exp = exp_bucket(deals, start, end)
        errs += [f"Q{q}.{e}" for e in diff_dict(got, exp)]
        if (str(got.get("close_from")), str(got.get("close_to"))) != (start.isoformat(), end.isoformat()):
            rng_errs.append(f"Q{q} {got.get('close_from')}..{got.get('close_to')} vs {start}..{end}")
        if got.get("label") != f"Q{q} FY 2026-27":
            rng_errs.append(f"label {got.get('label')}")
    R.check("RP-040", "Quarter-wise forecast FY 2026-27: won, open, weighted, lost, forecast per quarter exact", not errs,
            "; ".join(errs))
    R.check("RP-041", "Fiscal quarters are Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar with labels 'Qn FY 2026-27'",
            not rng_errs and b.get("label") == "FY 2026-27" and str(b.get("start")) == "2026-04-01" and
            str(b.get("end")) == "2027-03-31", "; ".join(rng_errs) + f" {b.get('label')} {b.get('start')} {b.get('end')}")
    # Boundaries: D1 closes 31-Mar-2026 (FY2025 Q4), D2 1-Apr-2026 (FY2026 Q1), D9 31-Mar-2027 (FY2026 Q4), D10 1-Apr-2027
    q1, q4 = b["quarters"][0], b["quarters"][3]
    s25, b25 = get(tok, "/sales-reports/forecast", fy=2025)
    s27, b27 = get(tok, "/sales-reports/forecast", fy=2027)
    cond = (feq(q1["categories"]["COMMIT"], 20000) and q1["open_count"] == 1      # D2 only (D1 excluded)
            and feq(q4["pipeline"], 7000 + 9000)                                     # D8 + D9 (31 Mar 2027 in)
            and s25 == 200 and feq(b25["quarters"][3]["pipeline"], 10000)            # D1 in FY2025 Q4
            and s27 == 200 and feq(b27["quarters"][0]["pipeline"], 4000))            # D10 in FY2027 Q1
    R.check("RP-042", "FY boundaries: 31 Mar falls in previous FY Q4, 1 Apr starts the new FY Q1", cond,
            f"Q1 open {q1['open_count']} commit {q1['categories']}; Q4 pipeline {q4['pipeline']}; "
            f"FY25Q4 {b25['quarters'][3]['pipeline'] if s25 == 200 else s25}; FY27Q1 {b27['quarters'][0]['pipeline'] if s27 == 200 else s27}")
    errs = []
    years = {y["fy"]: y for y in b.get("years", [])}
    for fy in (2025, 2026, 2027, 2028):
        s0, _ = quarter_range(fy, 1)
        _, e0 = quarter_range(fy, 4)
        exp = exp_bucket(deals, s0, e0)
        errs += [f"FY{fy}.{e}" for e in diff_dict(years.get(fy, {}), exp)]
        if years.get(fy, {}).get("label") != f"FY {fy}-{str(fy + 1)[-2:]}":
            errs.append(f"label {years.get(fy, {}).get('label')}")
    errs += [f"total.{e}" for e in diff_dict(b["total"], exp_bucket(deals, date(2026, 4, 1), date(2027, 3, 31)))]
    R.check("RP-043", "Financial-year-wise forecast (FY 2025-26 .. 2028-29) and FY total exact", not errs, "; ".join(errs))
    t = b["total"]
    R.check("RP-044", "Won vs weighted: forecast = won + weighted open; best case = won + full open pipeline",
            feq(t["forecast"], t["won"] + t["weighted"]) and feq(t["best_case"], t["won"] + t["pipeline"]) and
            feq(t["won"], 45000) and feq(t["weighted"], 20000 * .75 + 5000 * .10 + 12000 * .25 + 7000 * .5 + 9000 * .75 + 3000 * .10 + 2500 * .10),
            j(t))
    cats = t["categories"]
    exp_c = {"COMMIT": 20000 + 9000 + 5000, "BEST_CASE": 7000, "PIPELINE": 12000 + 3000 + 2500, "OMITTED": 0}
    R.check("RP-045", "Forecast categories: Commit (incl. manual override), Best case, Pipeline; commit = won + commit deals",
            all(feq(cats.get(k), v) for k, v in exp_c.items()) and feq(t["commit"], 45000 + 34000) and
            feq(t["best_case_category"], 45000 + 34000 + 7000), f"{cats} commit={t['commit']} bcc={t['best_case_category']}")
    und = b.get("open_without_close_date", {})
    R.check("RP-046", "Open deals without a close date are excluded from buckets and reported separately",
            und.get("count") == 1 and feq(und.get("amount"), 6000), j(und))
    # Reconciliation with the opportunity list for the same period (BRD 5.9 acceptance)
    errs = []
    for q in b["quarters"] + [dict(b["total"], label="FY")]:
        s1, lst = get(tok, "/opportunities", close_from=q["close_from"], close_to=q["close_to"], page_size=500)
        items = lst.get("items", []) if s1 == 200 else []
        won = sum(i["amount"] or 0 for i in items if i["status"] == "WON")
        opn = sum(i["amount"] or 0 for i in items if i["status"] == "OPEN")
        wtd = sum(i["weighted_amount"] or 0 for i in items if i["status"] == "OPEN")
        if not (feq(won, q["won"]) and feq(opn, q["pipeline"]) and abs(wtd - q["weighted"]) < 0.05 and
                feq(lst.get("total_amount"), q["won"] + q["pipeline"] + q["lost"])):
            errs.append(f"{q['label']}: list won {won}/{q['won']} open {opn}/{q['pipeline']} w {wtd}/{q['weighted']}")
    R.check("RP-047", "Forecast totals reconcile with GET /opportunities for the same close-date range", not errs,
            "; ".join(errs))
    s, bx = get(tok, "/sales-reports/forecast", fy=2026, client_type="EXISTING")
    ex_deals = [d for d in deals if d["ctype"] == "EXISTING"]
    d = diff_dict(bx.get("total", {}), exp_bucket(ex_deals, date(2026, 4, 1), date(2027, 3, 31))) if s == 200 else [s]
    R.check("RP-048", "Forecast filtered to existing clients is exact (and carries no per-rep target)",
            s == 200 and not d and bx["total"].get("target") is None, d)
    s, bm = get(tok, "/sales-reports/forecast", fy=2026, member=CTX["uid"]["bd1"])
    d = diff_dict(bm.get("total", {}), exp_bucket(visible_deals("bd1"), date(2026, 4, 1), date(2027, 3, 31))) if s == 200 else [s]
    R.check("RP-049", "Forecast member filter (BD head) = that head's team only", s == 200 and not d, d)
    errs = []
    for v in ("bd1", "ex2", "ex1", "head"):
        s, bv = get(U[v]["token"], "/sales-reports/forecast", fy=2026)
        if s != 200:
            errs.append(f"{v}: {s}")
            continue
        errs += [f"{v}.{e}" for e in diff_dict(bv["total"], exp_bucket(visible_deals(v), date(2026, 4, 1), date(2027, 3, 31)))]
    R.check("RP-050", "Forecast is scoped to the caller's hierarchy (BD, executive, report, L1)", not errs, "; ".join(errs))
    tq = {q: sum(a for _, qq, a in CTX["targets"] if qq == q) for q in (1, 2, 3, 4)}
    errs = []
    for i, q in enumerate(b["quarters"], 1):
        exp_t = float(tq[i]) if tq[i] else None
        if not feq(q.get("target"), exp_t):
            errs.append(f"Q{i} target {q.get('target')} vs {exp_t}")
        exp_a = pct(q["won"], tq[i]) if tq[i] else None
        if not feq(q.get("attainment"), exp_a):
            errs.append(f"Q{i} attainment {q.get('attainment')} vs {exp_a}")
    if not feq(b["total"].get("target"), float(sum(tq.values()))):
        errs.append(f"total target {b['total'].get('target')}")
    R.check("RP-051", "Forecast carries quarter/FY targets and attainment = won / target x 100", not errs, "; ".join(errs))
    s, bd = get(tok, "/sales-reports/forecast")
    R.check("RP-052", f"Default forecast year is the current FY ({CUR_FY}) for today {TODAY}", s == 200 and bd.get("fy") == CUR_FY,
            f"{bd.get('fy')}")
    s, bn = get(tok, "/sales-reports/forecast", fy="abc")
    R.check("RP-053", "Invalid fy parameter is rejected (422)", s == 422, f"{s}")


@safe("RP-060", "Team performance")
def t_team():
    U = CTX["U"]
    s, b = get(U["owner"]["token"], "/sales-reports/team-performance")
    assert s == 200, (s, b)
    reps = {CTX["name_of"].get(r["user_id"]): r for r in b["reps"]}
    errs = []
    plan = CTX["lead_plan"]
    exp_all = {}
    for who in ("owner", "head", "bd1", "bd2", "ex1", "ex2", "ex3", "mr1"):
        mine = [d for d in CTX["deals"] if d["owner"] == who]
        lp = [p for p in plan if p[1] == who]
        sqls = sum(1 for p in lp if p[3] in ("SQL", "CONVERTED"))
        conv = sum(1 for p in lp if p[3] == "CONVERTED")
        won = [d for d in mine if d["status"] == "WON"]
        lost = [d for d in mine if d["status"] == "LOST"]
        act = CTX["activity"].get(who, {"activities": 0, "calls": 0, "meetings": 0, "tasks_done": 0})
        emails = {"ex1": 2, "ex2": 2, "ex3": 1}.get(who, 0)
        e = {"leads": len(lp), "sqls": sqls, "opportunities": len(mine), "won_count": len(won),
             "won_amount": round(sum(d["amount"] for d in won), 2), "lost_count": len(lost),
             "open_pipeline": round(sum(d["amount"] for d in mine if d["status"] == "OPEN"), 2),
             "leads_reaching_sql": sqls, "sqls_converted": conv, "lead_to_sql": pct(sqls, len(lp)),
             "sql_to_opportunity": pct(conv, sqls), "win_rate": pct(len(won), len(won) + len(lost)),
             "emails_sent": emails, **act}
        exp_all[who] = e
        errs += [f"{who}.{x}" for x in diff_dict(reps.get(who, {}), e)]
    if set(reps) != set(exp_all):
        errs.append(f"reps {sorted(map(str, reps))}")
    R.check("RP-060", "Team scorecard per rep: activities, emails, leads, SQLs, deals, won, lost, pipeline exact",
            not errs, "; ".join(errs))
    tot = b["totals"]
    exp_t = {k: sum(v[k] for v in exp_all.values()) for k in ("leads", "sqls", "opportunities", "won_count", "lost_count",
                                                                 "activities", "leads_reaching_sql", "sqls_converted")}
    exp_t["won_amount"] = round(sum(v["won_amount"] for v in exp_all.values()), 2)
    exp_t["lead_to_sql"] = pct(exp_t["leads_reaching_sql"], exp_t["leads"])
    exp_t["sql_to_opportunity"] = pct(exp_t["sqls_converted"], exp_t["sqls"])
    exp_t["win_rate"] = pct(exp_t["won_count"], exp_t["won_count"] + exp_t["lost_count"])
    d = diff_dict(tot, exp_t)
    R.check("RP-061", "Team totals and stage conversion rates (lead->SQL, SQL->opportunity, win rate) exact", not d, d)
    errs = []
    for case, viewer in (("RP-062", "bd1"), ("RP-063", "ex1"), ("RP-064", "head")):
        s, bv = get(U[viewer]["token"], "/sales-reports/team-performance")
        got = {CTX["name_of"].get(r["user_id"]) for r in bv.get("reps", [])} if s == 200 else None
        exp = CTX["team"][viewer] or set(exp_all)
        R.check(case, f"Team performance for {viewer} lists exactly {sorted(exp)}", got == exp, f"{s} {sorted(map(str, got or []))}")
    s1, _ = get(U["bd2"]["token"], "/sales-reports/team-performance", member=CTX["uid"]["ex1"])
    s2, _ = get(U["ex1"]["token"], "/sales-reports/team-performance", member=CTX["uid"]["bd1"])
    R.check("RP-065", "Manager cannot open another team's (or their manager's) scorecard via member filter (404)",
            s1 == 404 and s2 == 404, f"bd2->ex1 {s1}, ex1->bd1 {s2}")
    r = reps.get("ex1", {})
    R.check("RP-066", "Logged calls, meetings and completed tasks count as rep activity",
            r.get("calls") == 1 and r.get("meetings") == 1 and r.get("tasks_done") == 1 and r.get("activities") == 3, j(r))


@safe("RP-089", "Targets vs actual")
def t_targets_leaderboard():
    U = CTX["U"]
    tok = U["owner"]["token"]
    errs = []
    for quarter in (None, 1, 2):
        if quarter:
            start, end = quarter_range(2026, quarter)
        else:
            start, end = date(2026, 4, 1), date(2027, 3, 31)
        s, b = get(tok, "/sales-reports/targets", fy=2026, quarter=quarter)
        if s != 200:
            errs.append(f"q{quarter}: {s}")
            continue
        rows = {CTX["name_of"].get(r["user_id"]): r for r in b["rows"]}
        for who in CTX["uid"]:
            mine = [d for d in CTX["deals"] if d["owner"] == who and d["close"] and start <= d["close"] <= end]
            won = sum(d["amount"] for d in mine if d["status"] == "WON")
            commit = won + sum(d["amount"] for d in mine if d["status"] == "OPEN" and d["cat"] == "COMMIT")
            best = commit + sum(d["amount"] for d in mine if d["status"] == "OPEN" and d["cat"] == "BEST_CASE")
            target = sum(a for w, q, a in CTX["targets"] if w == who and (quarter is None or q == quarter))
            exp = {"won": float(won), "commit": float(commit), "best_case": float(best),
                   "target": float(target) if target else None,
                   "attainment": pct(won, target) if target else None,
                   "gap": float(max(target - won, 0)) if target else None}
            errs += [f"q{quarter}.{who}.{e}" for e in diff_dict(rows.get(who, {}), exp)]
        if quarter is None:
            top = [CTX["name_of"].get(r["user_id"]) for r in sorted(b["rows"], key=lambda r: r["rank"])][:2]
            # ex2: 15000/20000 = 75%, ex3: 30000/25000 = 120% -> ex3 first, ex2 second
            if top != ["ex3", "ex2"]:
                errs.append(f"rank order {top}")
    R.check("RP-089", "Target vs actual per rep (FY and quarters 1, 2): won, commit, best case, attainment, gap, rank",
            not errs, "; ".join(errs))
    s, b = get(tok, "/sales-reports/leaderboard")
    order = [CTX["name_of"].get(r["user_id"]) for r in b.get("rows", [])][:3] if s == 200 else None
    top = b["rows"][0] if s == 200 and b.get("rows") else {}
    R.check("RP-090", "Leaderboard ranks reps by revenue won, then deals, SQLs, activity", order == ["ex3", "ex2", "ex1"]
            and feq(top.get("won_amount"), 30000) and top.get("rank") == 1, f"{order} {j(top)}")


@safe("RP-092", "Campaign ROI")
def t_roi():
    U = CTX["U"]
    s, b = get(U["owner"]["token"], "/sales-reports/campaign-roi")
    rows = {r["campaign_id"]: r for r in b.get("rows", [])} if s == 200 else {}
    r = rows.get(CTX["campaign_id"], {})
    conv = next(d for d in CTX["deals"] if d["key"] == "CONV")
    exp = {"contacts_emailed": 5, "replies": 2, "leads": 2, "sqls": 2, "opportunities": 1, "pipeline": 2500.0,
           "weighted": round(2500 * conv["prob"] / 100, 2), "won_count": 0, "revenue": 0.0, "reply_rate": 40.0,
           "revenue_per_contact": 0.0}
    d = diff_dict(r, exp)
    R.check("RP-092", "Campaign ROI: emailed, replies, leads, SQLs, opportunities, pipeline and revenue per campaign exact",
            s == 200 and not d and CTX.get("conv_campaign") == CTX["campaign_id"], f"{d} conv_campaign={CTX.get('conv_campaign')}")


@safe("RP-098", "Custom reports")
def t_custom_reports():
    U = CTX["U"]
    tok = U["owner"]["token"]
    s, b = req("POST", "/reports/custom/run", {"object": "deals", "group_by": "stage", "measure": "sum_amount"}, tok)
    exp = {}
    for d in CTX["deals"]:
        exp[CTX["ST"][d["stage"]]["stage_id"]] = exp.get(CTX["ST"][d["stage"]]["stage_id"], 0) + d["amount"]
    got = {r["key"]: r["value"] for r in b.get("rows", [])} if s == 200 else {}
    ok = s == 200 and set(got) == set(exp) and all(feq(got[k], v) for k, v in exp.items()) and \
        feq(b.get("total"), sum(exp.values()))
    R.check("RP-098", "Custom report (opportunities, sum of amount by stage) equals independently summed values", ok,
            f"{s} {j(got)} vs {j(exp)}")
    # Matrix: deals count by owner x status with close date in FY 2026
    s, b = req("POST", "/reports/custom/run", {"object": "deals", "group_by": "owner", "group_by_2": "status",
                                               "measure": "count", "date_field": "close_date",
                                               "date_from": "2026-04-01", "date_to": "2027-03-31"}, tok)
    expm = {}
    for d in CTX["deals"]:
        if d["close"] and date(2026, 4, 1) <= d["close"] <= date(2027, 3, 31):
            expm.setdefault(CTX["uid"][d["owner"]], {}).setdefault(d["status"], 0)
            expm[CTX["uid"][d["owner"]]][d["status"]] += 1
    gotm = {r["key"]: {k: int(v) for k, v in r["values"].items()} for r in b.get("rows", [])} if s == 200 else {}
    R.check("RP-098b", "Custom matrix report (deals by owner x status, close date in FY) exact", gotm == expm,
            f"{s} {j(gotm)} vs {j(expm)}")
    # Shared report built by a manager, run by an executive -> each sees only their records
    s, rep = req("POST", "/reports/custom", {"name": "Deals by owner", "shared": True,
                                             "definition": {"object": "deals", "group_by": "owner", "measure": "count"}},
                 U["bd1"]["token"])
    errs = [] if s == 201 else [f"create {s} {j(rep)}"]
    if s == 201:
        CTX["custom_report"] = rep["report_id"]
        for viewer in ("bd1", "ex2", "ex1"):
            s2, r2 = get(U[viewer]["token"], f"/reports/custom/{rep['report_id']}")
            got = {CTX["name_of"].get(r["key"]): r["value"] for r in r2.get("result", {}).get("rows", [])} if s2 == 200 else None
            exp = {}
            for d in visible_deals(viewer):
                exp[d["owner"]] = exp.get(d["owner"], 0) + 1
            if got != exp:
                errs.append(f"{viewer}: {got} vs {exp}")
    s3, _ = req("POST", "/reports/custom/run", {"object": "deals", "group_by": "owner", "measure": "count"}, U["ex1"]["token"])
    R.check("RP-099", "Shared custom report runs within each viewer's hierarchy; executives (L3 agents) cannot build reports",
            not errs and s3 == 403, f"{errs} ex1 build={s3}")
    s, b = req("POST", "/reports/custom/run", {"object": "deals", "group_by": "nope", "measure": "count"}, tok)
    s2, b2 = req("POST", "/reports/custom/run", {"object": "invoices", "group_by": "owner", "measure": "count"}, tok)
    R.check("RP-099b", "Custom report rejects unknown grouping/object with 400", s == 400 and s2 == 400, f"{s} {s2}")


@safe("RP-110", "Cross-tenant isolation")
def t_isolation():
    ws2 = CTX["ws2"]
    t2 = ws2.token
    bad = []
    for p in REPORTS:
        s, b = get(t2, p, member=CTX["uid"]["ex1"])
        if s != 404:
            bad.append(f"{p}:{s}")
    R.check("RP-110", "Another tenant's admin cannot report on this tenant's users (member=foreign id -> 404) on every report",
            not bad, ", ".join(bad))
    errs = []
    s, b = get(t2, "/sales-reports/forecast", fy=2026)
    if s != 200 or not feq(b["total"]["won"], 99999) or b["total"]["open_count"] != 0 or not feq(b["total"].get("target"), 77777):
        errs.append(f"forecast {s} {j(b.get('total') if isinstance(b, dict) else b)}")
    s, b = get(t2, "/sales-reports/dashboard")
    k = b.get("kpis", {}) if s == 200 else {}
    if k.get("won_count") != 1 or not feq(k.get("won_amount"), 99999) or k.get("new_leads") != 0 or k.get("contacts_emailed") != 0:
        errs.append(f"dashboard {j(k)}")
    s, b = get(t2, "/sales-reports/team-performance")
    if s != 200 or {r["user_id"] for r in b["reps"]} != {ws2.user_id}:
        errs.append(f"team {s}")
    s, b = get(t2, "/sales-reports/campaign-roi")
    if s != 200 or b.get("rows"):
        errs.append(f"roi {j(b)}")
    s, b = get(t2, "/sales-reports/targets", fy=2026)
    if s != 200 or {r["user_id"] for r in b["rows"]} != {ws2.user_id}:
        errs.append(f"targets {s}")
    s, b = req("POST", "/reports/custom/run", {"object": "deals", "group_by": "owner", "measure": "sum_amount"}, t2)
    if s != 200 or not feq(b.get("total"), 99999):
        errs.append(f"custom {j(b)}")
    R.check("RP-111", "Other tenant's reports contain only its own data (exact totals; ours excluded) and ours exclude its 99,999",
            not errs and not feq(CTX["forecast_owner"]["total"]["won"], 45000 + 99999), "; ".join(errs))
    errs = []
    checks = [("GET", f"/opportunities/{CTX['D']['D1']}", None), ("GET", f"/opportunities/{CTX['D']['D1']}/timeline", None),
              ("GET", f"/reports/custom/{CTX.get('custom_report', 'x')}", None),
              ("PUT", "/sales/targets", {"user_id": CTX["uid"]["ex1"], "fy": 2026, "quarter": 1, "amount": 1})]
    for m, p, body in checks:
        s, b = req(m, p, body, t2)
        if s != 404:
            errs.append(f"{m} {p}: {s}")
    s, b = get(CTX["U"]["owner"]["token"], f"/opportunities/{CTX['other_opp']}")
    if s != 404:
        errs.append(f"reverse opp {s}")
    R.check("RP-112", "Cross-tenant object access (deal, timeline, saved report, target write) returns 404", not errs,
            "; ".join(errs))
    U = CTX["U"]
    bad = []
    for p in REPORTS:
        for viewer, member in (("ex2", "ex1"), ("ex3", "bd2"), ("mr1", "ex1")):
            s, _ = get(U[viewer]["token"], p, member=CTX["uid"][member])
            if s != 404:
                bad.append(f"{viewer}->{member} {p}:{s}")
    R.check("RP-113", "Hierarchy: users cannot widen any report to a peer's or their manager's data (404)", not bad,
            ", ".join(bad))


@safe("RP-114", "Campaign reports hierarchy")
def t_campaign_reports_scope():
    U = CTX["U"]
    s, b = get(U["bd2"]["token"], "/reports/dashboard-summary", user_id=CTX["uid"]["ex1"])
    s2, b2 = get(U["bd2"]["token"], "/reports/campaign-comparison", user_id=CTX["uid"]["ex1"])
    R.check("RP-114", "Campaign analytics (v1 reports) refuse a BD manager reading another team's rep (user_id filter)",
            s in (403, 404) and s2 in (403, 404),
            f"/reports/dashboard-summary?user_id=<other team rep> -> {s}; campaign-comparison -> {s2} "
            "(app/routers/reports_router.py only forces AGENTs to their own id; MANAGERs may pass any user_id)")


@safe("RP-115", "Role checks")
def t_roles():
    U = CTX["U"]
    ex = U["ex1"]["token"]
    res = {
        "settings": req("PUT", "/sales/settings", {"stale_deal_days": 3}, ex)[0],
        "rule": req("POST", "/workflow/rules", {"name": "x", "object_type": "DEAL", "field": "amount",
                                                 "operator": "changes", "actions": [{"type": "notify"}]}, ex)[0],
        "rules_list": get(ex, "/workflow/rules")[0],
        "product": req("POST", "/products", {"name": "x", "unit_price": 1}, ex)[0],
        "target_self": req("PUT", "/sales/targets", {"user_id": CTX["uid"]["ex1"], "fy": 2026, "quarter": 1, "amount": 1}, ex)[0],
        "bd1_target_other_team": req("PUT", "/sales/targets", {"user_id": CTX["uid"]["ex3"], "fy": 2026, "quarter": 1,
                                                               "amount": 1}, U["bd1"]["token"])[0],
    }
    s_ok, _ = req("PUT", "/sales/targets", {"user_id": CTX["uid"]["ex2"], "fy": 2026, "quarter": 1, "amount": 20000},
                  U["bd1"]["token"])
    R.check("RP-115", "Role checks: agents cannot change sales settings, workflow rules, products or their own target; "
            "a BD manager can set targets only inside their team",
            all(v == 403 for v in res.values()) and s_ok == 200, f"{res} bd1->ex2 {s_ok}")
    s, b = get(U["ex1"]["token"], "/sales/targets", fy=2026)
    got = {CTX["name_of"].get(u["user_id"]) for u in b.get("users", [])} if s == 200 else None
    R.check("RP-088", "Targets per rep per quarter: list is scoped to the caller's team with quarter amounts",
            got == {"ex1", "mr1"} and next(u for u in b["users"] if u["user_id"] == CTX["uid"]["ex1"])["quarters"][:2] == [10000.0, 10000.0],
            f"{s} {got}")


@safe("RP-093", "Amount hiding")
def t_amount_hiding():
    U = CTX["U"]
    mr = U["mr1"]["token"]
    s, b = get(mr, "/sales-reports/forecast", fy=2026)
    before = s == 200 and feq(b["total"]["pipeline"], 3000)
    s, b = req("PUT", "/sales/settings", {"amount_hidden_levels": [4]}, U["owner"]["token"])
    assert s == 200, (s, b)
    paths = REPORTS + ["/opportunities", "/opportunities/board", "/pipeline/revenue", "/proposals", "/products",
                       f"/opportunities/{CTX['D']['D12']}", "/sales/targets"]
    leaks = []
    for p in paths:
        s, b = get(mr, p)
        if s != 200:
            leaks.append(f"{p}:{s}")
            continue
        leaks += [f"{p}{x}" for x in leaked_money(b)[:3]]
    R.check("RP-093", "Amount/revenue fields are blank on every report and deal API for a role configured to hide them",
            before and not leaks, f"before_ok={before} " + "; ".join(leaks[:12]))
    # Same viewer still sees counts
    s, b = get(mr, "/sales-reports/forecast", fy=2026)
    R.check("RP-093b", "Hidden-amount viewer still gets counts (open deals) so reports stay usable",
            s == 200 and b["total"]["open_count"] == 1 and b["total"]["pipeline"] is None, j(b.get("total")))
    s1, _ = req("PATCH", f"/opportunities/{CTX['D']['D12']}", {"amount": 1}, mr)
    s2 = None
    if CTX.get("money_report"):
        s2, _ = get(mr, f"/reports/custom/{CTX['money_report']}")
    pdf = CTX.get("proposal_id")
    s3 = get(mr, f"/proposals/{pdf}/pdf")[0] if pdf else None
    R.check("RP-094", "Hidden-amount viewer cannot change amounts, run a shared money report or download a quote PDF (403)",
            s1 == 403 and s2 == 403 and s3 == 403, f"patch {s1} money-report {s2} pdf {s3}")
    # Owner still sees amounts (setting is per level)
    s, b = get(U["ex1"]["token"], "/sales-reports/forecast", fy=2026)
    R.check("RP-093c", "Roles not listed keep seeing amounts", s == 200 and b["total"]["pipeline"] is not None, j(b.get("total")))


# ── 5.12 features that mutate data (run after the exact-number checks) ──

@safe("RP-095", "Workflow rules")
def t_workflow():
    U, uid, ST = CTX["U"], CTX["uid"], CTX["ST"]
    tok = U["owner"]["token"]
    s, r1 = req("POST", "/workflow/rules", {"name": "Big deal", "object_type": "DEAL", "field": "amount",
                                            "operator": "greater_than", "value": "50000",
                                            "actions": [{"type": "notify", "to": ["owner", "manager"],
                                                         "message": "Big deal: {name}"},
                                                        {"type": "create_task", "title": "Review {name}",
                                                         "assign_to": "owner", "due_in_days": 2}]}, tok)
    s2, r2 = req("POST", "/workflow/rules", {"name": "Negotiation next step", "object_type": "DEAL", "field": "stage_id",
                                             "operator": "equals", "value": "Negotiation",
                                             "actions": [{"type": "update_field", "field": "next_step",
                                                          "value": "Send contract"}]}, tok)
    s3, r3 = req("POST", "/workflow/rules", {"name": "Inactive", "object_type": "DEAL", "field": "close_date",
                                             "operator": "changes", "active": False,
                                             "actions": [{"type": "notify", "to": ["owner"], "message": "inactive fired"}]}, tok)
    assert s == 201 and s2 == 201 and s3 == 201, (s, r1, s2, r2, s3, r3)
    s, w = req("POST", "/opportunities", {"name": f"RP workflow {CTX['ws'].rid}", "owner_id": uid["ex1"], "amount": 1000,
                                          "stage_id": ST["Qualification"]["stage_id"], "close_date": "2026-11-30"}, tok)
    assert s == 201, (s, w)
    wid = w["opportunity_id"]
    CTX["W"] = wid
    s, _ = req("PATCH", f"/opportunities/{wid}", {"amount": 60000}, tok)
    n_ex1 = get(U["ex1"]["token"], "/notifications", limit=100)[1]
    n_bd1 = get(U["bd1"]["token"], "/notifications", limit=100)[1]
    hit = lambda n: [i for i in n.get("items", []) if i["kind"] == "WORKFLOW" and "Big deal" in (i["title"] or "")]
    tasks = get(tok, "/tasks", opportunity_id=wid, scope="all")[1]
    titems = tasks.get("items", tasks) if isinstance(tasks, dict) else tasks
    task_ok = any("Review" in (t.get("title") or "") and t.get("owner_id") == uid["ex1"] for t in (titems or []))
    R.check("RP-095", "Workflow: amount rises above 50,000 -> alert to owner and manager + task for owner",
            s == 200 and hit(n_ex1) and hit(n_bd1) and task_ok,
            f"patch {s} ex1 {len(hit(n_ex1))} bd1 {len(hit(n_bd1))} task {task_ok} {j(titems)[:200]}")
    s, d = req("PATCH", f"/opportunities/{wid}", {"stage_id": ST["Negotiation"]["stage_id"]}, tok)
    R.check("RP-096", "Workflow: stage changes to Negotiation -> rule sets next step", s == 200 and d.get("next_step") == "Send contract",
            f"{s} next_step={d.get('next_step') if isinstance(d, dict) else d}")
    s, _ = req("PATCH", f"/opportunities/{wid}", {"close_date": "2026-12-15"}, tok)
    n_ex1 = get(U["ex1"]["token"], "/notifications", limit=100)[1]
    fired = [i for i in n_ex1.get("items", []) if "inactive fired" in (i["title"] or "")]
    rules = {r["rule_id"]: r for r in get(tok, "/workflow/rules")[1]}
    bad_s, _ = req("POST", "/workflow/rules", {"name": "bad", "object_type": "DEAL", "field": "nonexistent",
                                               "operator": "changes", "actions": [{"type": "notify"}]}, tok)
    bad2, _ = req("POST", "/workflow/rules", {"name": "bad", "object_type": "DEAL", "field": "amount",
                                              "operator": "greater_than", "value": "lots", "actions": [{"type": "notify"}]}, tok)
    R.check("RP-097", "Workflow: inactive rule never fires; run counts tracked; invalid field/threshold rejected (400)",
            not fired and rules[r1["rule_id"]]["run_count"] == 1 and rules[r2["rule_id"]]["run_count"] == 1 and
            rules[r3["rule_id"]]["run_count"] == 0 and bad_s == 400 and bad2 == 400,
            f"fired={len(fired)} runs={[rules[x]['run_count'] for x in (r1['rule_id'], r2['rule_id'], r3['rule_id'])]} {bad_s} {bad2}")


@safe("RP-120", "Audit trail")
def t_audit():
    U, uid, ST = CTX["U"], CTX["uid"], CTX["ST"]
    tok = U["owner"]["token"]
    wid = CTX["W"]
    t0 = datetime.utcnow() - timedelta(minutes=5)
    s, b = req("PATCH", f"/opportunities/{wid}", {"owner_id": uid["ex2"], "amount": 61000,
                                                  "close_date": "2027-01-20", "stage_id": ST["Proposal"]["stage_id"]}, tok)
    assert s == 200, (s, b)
    hist = b.get("history", [])
    fields = {}
    for h in hist:
        fields.setdefault(h["field"], []).append(h)
    errs = []
    for f, old, new in (("owner_id", uid["ex1"], uid["ex2"]), ("amount", "60000", "61000"), ("close_date", "2026-12-15", "2027-01-20"),
                        ("stage_id", "Negotiation", "Proposal")):
        hs = fields.get(f, [])
        h = hs[0] if hs else None
        if not h:
            errs.append(f"{f} missing")
            continue
        if old not in str(h["old_value"]) or new not in str(h["new_value"]):
            errs.append(f"{f} {h['old_value']}->{h['new_value']}")
        if not h.get("changed_by_name") or "Sys" not in h["changed_by_name"]:
            errs.append(f"{f} by {h.get('changed_by_name')}")
        try:
            at = datetime.fromisoformat(str(h["changed_at"]).replace("Z", ""))
            if at < t0:
                errs.append(f"{f} at {at}")
        except Exception:
            errs.append(f"{f} at {h.get('changed_at')}")
    R.check("RP-120", "Audit: owner, stage, amount and close-date changes are logged with old/new value, user and time",
            not errs, "; ".join(errs))
    sh = b.get("stage_history", [])
    R.check("RP-083", "Opportunity keeps stage history with time in stage (BR-SF-04)",
            [x["stage"] for x in sh][-3:] == ["Qualification", "Negotiation", "Proposal"], j(sh))
    # Lead owner change logged too
    lid = CTX["L"]["L3"]
    s, lb = req("PATCH", f"/leads/{lid}", {"owner_id": uid["ex1"]}, tok)
    lh = [h for h in (lb.get("history", []) if s == 200 else []) if h["field"] == "owner_id"]
    R.check("RP-121", "Audit: lead owner change logged with user and time", s == 200 and lh and lh[0].get("changed_by_name"),
            j(lh))
    req("PATCH", f"/leads/{lid}", {"owner_id": uid["ex2"]}, tok)  # restore


@safe("RP-084", "Win/loss reasons and stale deals")
def t_win_loss_stale():
    U, uid, ST = CTX["U"], CTX["uid"], CTX["ST"]
    tok = U["owner"]["token"]
    s, b = req("POST", "/opportunities", {"name": "no reason", "amount": 10, "stage_id": ST["Closed won"]["stage_id"]}, tok)
    s2, b2 = req("PATCH", f"/opportunities/{CTX['W']}", {"stage_id": ST["Closed lost"]["stage_id"]}, tok)
    R.check("RP-084", "Closing a deal as won or lost without a reason is refused (400)", s == 400 and s2 == 400, f"{s} {s2}")
    s, st = req("PUT", "/sales/settings", {"stale_deal_days": 5}, tok)
    sql(f"UPDATE opportunities SET updated_at = NOW() - INTERVAL 10 DAY WHERE opportunity_id='{CTX['D']['D7']}' "
        f"AND tenant_id='{CTX['ws'].tenant_id}'")
    s, lst = get(tok, "/opportunities", stale="true", page_size=500)
    ids = {i["opportunity_id"]: i for i in lst.get("items", [])} if s == 200 else {}
    d7 = ids.get(CTX["D"]["D7"], {})
    d2 = ids.get(CTX["D"]["D2"])  # open, close date 1 Apr 2026 already passed -> stale by date
    R.check("RP-084b", "Stale deals: no activity for N days or past close date are flagged (list filter + flag)",
            d7.get("stale") is True and d7.get("days_idle", 0) >= 10 and d2 is not None and CTX["D"]["D8"] not in ids,
            f"{s} D7={j(d7)[:120]} D2 in={d2 is not None}")


@safe("RP-086", "Notifications")
def t_notifications():
    U = CTX["U"]
    ex2 = U["ex2"]["token"]
    s, b = get(ex2, "/notifications", limit=100)
    assigned = [i for i in b.get("items", []) if i["kind"] == "ASSIGNED"]
    unread0 = b.get("unread")
    s2, m = req("POST", "/notifications/read", {"ids": [assigned[0]["notification_id"]]} if assigned else {}, ex2)
    s3, b3 = get(ex2, "/notifications", limit=100)
    R.check("RP-086", "In-app notifications: assignment alerts reach the new owner; mark-as-read lowers unread count",
            s == 200 and len(assigned) >= 2 and s2 == 200 and m.get("marked") == 1 and b3.get("unread") == unread0 - 1,
            f"assigned={len(assigned)} unread {unread0}->{b3.get('unread')} marked={m}")
    s4, p = req("PUT", "/notifications/preferences", {"email": True}, ex2)
    s5, b5 = get(ex2, "/notifications")
    # The worker emails notifications every minute; it stamps emailed_at even when no sender is configured
    nid = assigned[-1]["notification_id"] if assigned else ""
    deadline = time.time() + 150
    stamped = ""
    while time.time() < deadline:
        stamped = sql(f"SELECT emailed_at FROM notifications WHERE notification_id='{nid}'")
        if stamped and stamped != "NULL":
            break
        time.sleep(10)
    R.check("RP-087", "Email notifications: preference saved and the worker's email loop processes the notification",
            s4 == 200 and b5.get("email_enabled") is True and stamped not in ("", "NULL"), f"pref {s4} emailed_at={stamped}")
    sender = subprocess.run(["docker", "exec", "vector-worker-1", "printenv", "SENDER_EMAIL"], capture_output=True, text=True).stdout.strip()
    R.record("RP-087b", "Email notification actually delivered to the user's mailbox",
             None, f"SENDER_EMAIL is {'set' if sender else 'empty'} in the worker; local stack has no mail sink - verify in UAT",
             "env")


@safe("RP-100", "Products, quotes, PDF")
def t_quotes():
    U = CTX["U"]
    tok = U["owner"]["token"]
    s, p = req("POST", "/products", {"name": f"RP Licence {CTX['ws'].rid}", "sku": "LIC-1", "unit_price": 1200, "unit": "seat"}, tok)
    assert s == 201, (s, p)
    s, prop = req("POST", "/proposals", {"opportunity_id": CTX["D"]["D8"], "title": "RP Quote", "status": "DRAFT"}, tok)
    assert s == 201, (s, prop)
    pid = prop["proposal_id"]
    CTX["proposal_id"] = pid
    s, q = req("PUT", f"/proposals/{pid}/lines", {"lines": [{"product_id": p["product_id"], "quantity": 3, "discount_pct": 10},
                                                            {"description": "Onboarding", "quantity": 1, "unit_price": 500}]}, tok)
    exp = {"subtotal": 4100.0, "discount_total": 360.0, "amount": 3740.0}
    lines = [l["line_total"] for l in q.get("lines", [])] if s == 200 else None
    s2, plist = get(tok, "/proposals", opportunity_id=CTX["D"]["D8"])
    pa = next((i["amount"] for i in plist.get("items", []) if i["proposal_id"] == pid), None) if s2 == 200 else None
    R.check("RP-100", "Quote lines: qty x unit price less line discount; proposal amount = line total (3 x 1,200 -10% + 500)",
            s == 200 and not diff_dict(q, exp) and lines == [3240.0, 500.0] and feq(pa, 3740), f"{s} {j(q)} list={pa}")
    s, pdf = req("GET", f"/proposals/{pid}/pdf", token=tok, raw=True)
    R.check("RP-101", "Proposal PDF downloads as a valid PDF document", s == 200 and isinstance(pdf, bytes) and
            pdf.startswith(b"%PDF") and b"%%EOF" in pdf[-1024:] and len(pdf) > 800, f"{s} {len(pdf) if isinstance(pdf, bytes) else pdf}")
    s, pdf2 = req("GET", f"/proposals/{pid}/pdf", token=CTX["ws2"].token, raw=True)
    s3, _ = req("POST", "/products", {"name": "x", "unit_price": -5}, tok)
    R.check("RP-101b", "Proposal PDF is not available across tenants (404); negative prices rejected (400)",
            s == 404 and s3 == 400, f"{s} {s3}")
    # A money report for the hidden-amount check
    s, r = req("POST", "/reports/custom", {"name": "Money", "shared": True,
                                           "definition": {"object": "deals", "group_by": "stage", "measure": "sum_amount"}}, tok)
    if s == 201:
        CTX["money_report"] = r["report_id"]


@safe("RP-091", "Template library")
def t_templates():
    U = CTX["U"]
    tok = U["bd1"]["token"]
    s, t = req("POST", "/template-library", {"name": "RP intro", "category": "Intro", "subject": "Hi {{first_name}}",
                                             "body": "Hello {{full_name}} at {{company_name}} - {{your_name}}", "shared": True}, tok)
    assert s == 201, (s, t)
    s, r = req("POST", f"/template-library/{t['template_id']}/render", {"prospect_id": CTX["P"]["P1"], "count_use": True}, tok)
    ok = s == 200 and r.get("subject") == "Hi Pat1" and "Pat1 Contact" in r.get("body", "") and "Acme1 Ltd" in r.get("body", "") \
        and "Bdan Test" in r.get("body", "") and r.get("sample") is False
    s2, lst = get(U["ex3"]["token"], "/template-library")
    shared = any(i["template_id"] == t["template_id"] for i in lst.get("items", [])) if s2 == 200 else False
    tokens = {m["token"] for m in lst.get("merge_fields", [])} if s2 == 200 else set()
    s3, _ = req("POST", f"/template-library/{t['template_id']}/render", {}, CTX["ws2"].token)
    s4, _ = req("PATCH", f"/template-library/{t['template_id']}", {"name": "hijack"}, U["ex3"]["token"])
    R.check("RP-091", "Template library: merge fields fill from the contact; shared within tenant, 404 across tenants, "
            "only owner/admin may edit", ok and shared and "{{first_name}}" in tokens and s3 == 404 and s4 == 403,
            f"{s} {j(r)} shared={shared} other-tenant={s3} edit-by-other={s4}")


@safe("RP-080", "Lead from reply")
def t_lead_from_reply():
    U = CTX["U"]
    tok = U["owner"]["token"]
    s, c = req("POST", "/contacts", {"email": f"p8-{CTX['ws'].rid}@acme8.example", "first_name": "Pat8",
                                     "last_name": "Reply", "owner_id": CTX["uid"]["ex3"]}, tok)
    mid = sql("SELECT UUID()")
    sql(f"INSERT INTO email_messages (message_id, campaign_id, prospect_id, to_email, direction, status, sent_at) VALUES "
        f"('{mid}', '{CTX['campaign_id']}', '{c['prospect_id']}', 'rep@systest.example', 'INBOUND', 'RECEIVED', NOW())")
    s, lead = req("POST", "/leads/from-message", {"message_id": mid}, tok)
    s2, again = req("POST", "/leads/from-message", {"message_id": mid}, tok)
    R.check("RP-080", "One-click lead from a campaign reply: lead linked to contact, owner and campaign; duplicate refused",
            s == 201 and lead.get("prospect_id") == c["prospect_id"] and lead.get("owner_id") == CTX["uid"]["ex3"] and
            lead.get("campaign_id") == CTX["campaign_id"] and s2 == 409, f"{s} {j(lead)} again={s2}")


@safe("RP-081", "Recycle")
def t_recycle():
    tok = CTX["U"]["owner"]["token"]
    s, b = get(tok, f"/leads/{CTX['L']['L4']}")
    ra = b.get("recycle_at") if s == 200 else None
    try:
        days = (datetime.fromisoformat(str(ra)) - datetime.utcnow()).days
    except Exception:
        days = None
    R.check("RP-081", "Disqualified lead keeps its reason and a recycle date (30 days) and shows in the report",
            s == 200 and b.get("disqualified_reason") == "No budget" and days in (29, 30), f"{ra} {days}")


@safe("RP-082", "Round robin")
def t_round_robin():
    U, uid = CTX["U"], CTX["uid"]
    tok = U["owner"]["token"]
    s, _ = req("PUT", "/sales/settings", {"lead_assignment": {"mode": "round_robin", "users": [uid["ex2"], uid["ex3"]],
                                                              "next_index": 0}}, tok)
    owners = []
    for i in range(3):
        s1, c = req("POST", "/contacts", {"email": f"rr{i}-{CTX['ws'].rid}@rr.example", "first_name": f"RR{i}"}, tok)
        sql(f"UPDATE prospects SET owner_id=NULL WHERE prospect_id='{c['prospect_id']}' AND tenant_id='{CTX['ws'].tenant_id}'")
        s2, l = req("POST", "/leads", {"prospect_id": c["prospect_id"]}, tok)
        owners.append(CTX["name_of"].get(l.get("owner_id")) if s2 == 201 else s2)
    req("PUT", "/sales/settings", {"lead_assignment": {"mode": "off"}}, tok)
    R.check("RP-082", "Round-robin assignment alternates unowned leads across the pool", owners == ["ex2", "ex3", "ex2"],
            f"{owners}")


@safe("RP-085", "Timeline")
def t_timeline():
    s, b = get(CTX["U"]["owner"]["token"], f"/opportunities/{CTX['D']['D2']}/timeline")
    txt = json.dumps(b)
    R.check("RP-085", "Opportunity timeline shows logged activities (call, meeting) and its tasks",
            s == 200 and "CALL" in txt and "MEETING" in txt and "Send deck" in txt, f"{s} {txt[:200]}")


@safe("RP-022", "Period boundaries")
def t_period_boundary():
    U, ws = CTX["U"], CTX["ws"]
    tok = U["owner"]["token"]
    stamps = ["2026-08-31 23:59:59", "2026-09-01 00:00:00", "2026-09-30 23:59:59", "2026-10-01 00:00:00"]
    for i, ts in enumerate(stamps):
        s, c = req("POST", "/contacts", {"email": f"pb{i}-{ws.rid}@pb.example", "first_name": f"PB{i}",
                                         "owner_id": CTX["uid"]["bd2"]}, tok)
        s2, l = req("POST", "/leads", {"prospect_id": c["prospect_id"], "source": "Boundary"}, tok)
        sql(f"UPDATE leads SET created_at='{ts}' WHERE lead_id='{l['lead_id']}' AND tenant_id='{ws.tenant_id}'")
    s, b = get(tok, "/sales-reports/leads", date_from="2026-09-01", date_to="2026-09-30")
    src = {x["source"]: x["leads"] for x in b.get("by_source", [])} if s == 200 else {}
    R.check("RP-022", "Report periods are inclusive whole days: 1 Sep 00:00 and 30 Sep 23:59:59 in, 31 Aug / 1 Oct out",
            src.get("Boundary") == 2 and b.get("total") == 2, f"{s} {src} total={b.get('total')}")


@safe("RP-102", "Calendar & email sync")
def t_connections():
    tok = CTX["U"]["ex1"]["token"]
    s, b = get(tok, "/connections")
    s2, b2 = req("POST", "/connections/google/start", None, tok)
    s3, _ = req("POST", "/connections/yahoo/start", None, tok)
    R.check("RP-102", "Calendar/email sync API is present: lists connections, refuses unknown provider (404)",
            s == 200 and s3 == 404, f"{s} {s3}")
    R.record("RP-102b", "Connect Google/Microsoft account and sync calendar + email", None,
             f"start -> {s2} {j(b2)[:160]} (OAuth client not configured on local stack; needs real Google/M365 accounts)", "env")


@safe("RP-070", "Not implemented checks")
def t_not_implemented():
    s, spec = req("GET", "/openapi.json")
    paths = " ".join(spec.get("paths", {}).keys()).lower() if s == 200 else ""
    ai = any(k in paths for k in ("discover", "enrich"))
    R.record("RP-070", "BR-AI-01 AI contact discovery / enrichment endpoint exists", True if ai else None,
             "" if ai else "No discovery/enrichment endpoint in /openapi.json (BRD marks it optional - C)",
             None if ai else "not-implemented")
    approval = "approval" in paths or "approve" in " ".join(p for p in spec.get("paths", {}) if "proposal" in p or "quote" in p)
    R.record("RP-103", "BR-SF-17 discount above threshold routed for approval", True if approval else None,
             "" if approval else "Quote lines accept any discount 0-100% with no approval step (quotes_router.set_lines)",
             None if approval else "not-implemented")
    ab = any(k in paths for k in ("ab-test", "ab_test", "variant", "experiment"))
    R.record("RP-104", "BR-SF-18 A/B test of email subject lines", True if ab else None,
             "" if ab else "No A/B / variant endpoint in /openapi.json", None if ab else "not-implemented")
    sso = any(k in paths for k in ("/api/auth/microsoft", "/api/auth/sso", "/api/auth/saml", "azure"))
    R.record("RP-105", "BR-SF-19 single sign-on with Microsoft 365", True if sso else None,
             "" if sso else "Only /api/auth/google sign-in exists; Microsoft OAuth is for mailboxes/sync, not sign-in",
             None if sso else "not-implemented")
    hooks = [p for p in spec.get("paths", {}) if "webhook" in p.lower() and "ses" not in p.lower()]
    R.check("RP-106", "BR-SF-20 REST API documented (OpenAPI)", s == 200 and len(spec.get("paths", {})) > 100, "")
    R.record("RP-106b", "BR-SF-20 outbound webhooks for other systems", True if hooks else None,
             "" if hooks else "Only the inbound SES webhook exists; no webhook subscription API", None if hooks else "not-implemented")
    R.record("RP-107", "BR-SF-05 stale-deal alert notification raised by hourly job", None,
             "Job runs hourly in vector-worker (app/jobs.py); not waited for in an automated run - see manual case", "env")
    R.record("RP-161", "KPI SQL-to-win rate and forecast accuracy (actual / committed per quarter) reported directly", None,
             "No report returns these ratios; derivable from team-performance counts and forecast commit/won", "not-implemented")


# ── NFR: performance / concurrency ──

@safe("RP-130", "Dashboard latency")
def t_latency():
    tok = CTX["U"]["owner"]["token"]
    worst = {}
    for p in REPORTS + ["/opportunities", "/pipeline/revenue"]:
        times = []
        for _ in range(3):
            t0 = time.time()
            s, _ = get(tok, p)
            times.append(time.time() - t0)
        worst[p] = round(max(times), 3)
    slow = {p: t for p, t in worst.items() if t >= 3}
    CTX["latency"] = worst
    R.check("RP-130", "Dashboards and reports load in under 3 s (worst of 3, owner, seeded tenant)", not slow, j(worst))


@safe("RP-131", "Concurrency")
def t_concurrency(users=25, seconds=60):
    """25 virtual users doing mixed reads for 60 s. Each user is a distinct client address (X-Forwarded-For) so the
    per-IP API ceiling (1,200/min) of the shared host IP is not consumed by this test and other suites are unaffected."""
    U = CTX["U"]
    toks = [U[k]["token"] for k in ("owner", "head", "bd1", "bd2", "ex1", "ex2", "ex3", "mr1")]
    paths = ["/sales-reports/dashboard", "/sales-reports/forecast", "/sales-reports/pipeline", "/sales-reports/funnel",
             "/sales-reports/team-performance", "/sales-reports/leaderboard", "/opportunities?page_size=50",
             "/contacts?page_size=50", "/leads", "/notifications", "/pipeline/revenue", "/sales-reports/targets"]
    lat, errors, lock = [], [], threading.Lock()
    stop = time.time() + seconds

    def worker(n):
        tok = toks[n % len(toks)]
        ip = f"198.18.{n // 250}.{n % 250 + 1}"
        i = n
        while time.time() < stop:
            p = paths[i % len(paths)]
            i += 1
            t0 = time.time()
            try:
                s, _ = req("GET", p, token=tok, headers=xff_headers(ip))
            except Exception as e:  # noqa: BLE001
                s = f"exc {type(e).__name__}"
            dt = time.time() - t0
            with lock:
                lat.append(dt)
                if s != 200:
                    errors.append(f"{p}:{s}")
            time.sleep(0.2)  # think time

    th = [threading.Thread(target=worker, args=(n,)) for n in range(users)]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=seconds + 150)
    lat.sort()
    n = len(lat)
    p50 = lat[n // 2] if n else None
    p95 = lat[int(n * 0.95) - 1] if n else None
    err = len(errors) / n * 100 if n else 100
    CTX["load"] = {"users": users, "seconds": seconds, "requests": n, "rps": round(n / seconds, 1),
                   "p50_ms": round(p50 * 1000) if p50 else None, "p95_ms": round(p95 * 1000) if p95 else None,
                   "max_ms": round(lat[-1] * 1000) if n else None, "error_rate_pct": round(err, 2),
                   "errors_sample": errors[:5]}
    R.check("RP-131", f"{users} concurrent users x {seconds}s mixed reads: 0% errors and p95 < 3 s",
            n > 0 and err == 0 and p95 < 3, j(CTX["load"]))


# ── UI (Playwright) ──

@safe("RP-150", "UI")
def t_ui():
    pw_dir = os.environ.get("PW_DIR", "/tmp/claude-0/-home-user/b764cdf9-b625-5070-aae1-e6d130b3e7df/scratchpad")
    if not os.path.isdir(os.path.join(pw_dir, "node_modules", "playwright")) or not shutil.which("node"):
        for c in ("RP-150", "RP-151", "RP-152", "RP-153", "RP-154", "RP-155"):
            R.record(c, "UI check", None, "Playwright not available (set PW_DIR)", "env")
        return
    tok = CTX["U"]["owner"]["token"]
    expected = {
        "dash": get(tok, "/sales-reports/dashboard")[1],
        "forecast": get(tok, "/sales-reports/forecast")[1],
        "team": get(tok, "/sales-reports/team-performance")[1],
        "targets": get(tok, "/sales-reports/targets")[1],
    }
    cfg = {"web": WEB, "email": CTX["ws"].email, "password": CTX["ws"].password, "expected": expected,
           "shots": os.path.join(HERE, "ui_shots")}
    cfg_path = os.path.join(pw_dir, "rp_ui_cfg.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, default=str)
    script = os.path.join(pw_dir, "rp_ui_check.mjs")
    shutil.copy(os.path.join(HERE, "ui_check.mjs"), script)
    p = subprocess.run(["node", script, cfg_path], cwd=pw_dir, capture_output=True, text=True, timeout=300)
    out = p.stdout.strip().splitlines()
    res = None
    for line in reversed(out):
        if line.startswith("{"):
            res = json.loads(line)
            break
    if not res:
        R.record("RP-150", "UI run", False, (p.stderr or p.stdout)[-400:], "test-issue")
        return
    for c in res["cases"]:
        R.record(c["id"], c["title"], c["pass"], c.get("detail", ""), None if c["pass"] else c.get("category", "defect"))


# ── Rate limiting (last) ──

@safe("RP-140", "Login rate limit")
def t_rate_limit():
    ws = CTX["ws"]
    rl = add_user(ws, "AGENT", "Ratelim")
    ip = fake_ip()
    codes = []
    for i in range(11):
        s, b = req("POST", "/api/auth/login", {"email": rl["email"], "password": "wrong-password"}, headers=xff_headers(ip))
        codes.append(s)
    s, b = req("POST", "/api/auth/login", {"email": rl["email"], "password": ws.password}, headers=xff_headers(fake_ip()))
    # Retry-After is not exposed by req(); read it with a raw call
    import urllib.request
    import urllib.error
    retry = None
    try:
        r = urllib.request.Request(API + "/api/auth/login", method="POST",
                                   data=json.dumps({"email": rl["email"], "password": ws.password}).encode(),
                                   headers={"Content-Type": "application/json", "X-Forwarded-For": fake_ip()})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=30)
    except urllib.error.HTTPError as e:
        retry = e.headers.get("Retry-After")
    CTX["lockout"] = {"codes": codes, "correct_after": s, "retry_after_s": retry}
    R.check("RP-140", "Login: 10 wrong passwords lock the account; even the right password gets 429 with Retry-After (~15 min)",
            codes[:10] == [401] * 10 and codes[10] == 429 and s == 429 and retry and 800 <= int(retry) <= 900,
            j(CTX["lockout"]))
    ip = fake_ip()
    codes = [req("POST", "/api/auth/login", {"email": f"nobody{i}-{ws.rid}@nowhere.example", "password": "x"},
                 headers=xff_headers(ip))[0] for i in range(61)]
    other = req("POST", "/api/auth/login", {"email": f"nobody-x-{ws.rid}@nowhere.example", "password": "x"},
                headers=xff_headers(fake_ip()))[0]
    CTX["ip_limit"] = {"first_429_at": codes.index(429) + 1 if 429 in codes else None, "fresh_xff_status": other}
    R.check("RP-141", "Login: per-IP limit (60 attempts / 5 min) answers 429 from the 61st attempt", codes[:60] == [401] * 60 and codes[60] == 429,
            j(CTX["ip_limit"]))
    R.check("RP-142", "Per-IP limits cannot be bypassed by sending a different X-Forwarded-For on the published API port",
            other == 429, f"A new X-Forwarded-For value from the same client gets {other} (not 429): client_ip() in "
            "app/core/rate_limit.py trusts the right-most X-Forwarded-For hop, which the client controls when it calls "
            ":8191 directly (TRUSTED_PROXY_HOPS=1, no proxy in front)")


def main():
    t_health()
    t_auth_required()
    setup()
    seed()
    t_dashboard()
    t_leads_report()
    t_pipeline_report()
    t_funnel()
    t_forecast()
    t_team()
    t_targets_leaderboard()
    t_roi()
    t_custom_reports()
    t_isolation()
    t_campaign_reports_scope()
    t_roles()
    t_quotes()
    t_amount_hiding()
    t_workflow()
    t_audit()
    t_win_loss_stale()
    t_notifications()
    t_templates()
    t_lead_from_reply()
    t_recycle()
    t_round_robin()
    t_timeline()
    t_period_boundary()
    t_connections()
    t_not_implemented()
    t_latency()
    if "--skip-ui" not in ARGS:
        t_ui()
    if "--skip-load" not in ARGS:
        t_concurrency()
    if "--skip-ratelimit" not in ARGS:
        t_rate_limit()
    counts = R.write()
    with open(os.path.join(HERE, "measurements.json"), "w") as f:
        json.dump({k: CTX.get(k) for k in ("latency", "load", "lockout", "ip_limit", "health_ms")}, f, indent=2, default=str)
    print(counts, json.dumps({k: CTX.get(k) for k in ("load", "lockout", "ip_limit")}, default=str))


if __name__ == "__main__":
    main()
