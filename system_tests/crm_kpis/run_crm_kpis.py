"""System tests for the BRD v2.0 section 8 CRM KPIs (GET /sales-reports/kpis) on the live stack.

Builds a workspace with a known dataset (duplicate contacts, positive replies and leads, SQLs and won
deals, forecast snapshots, daily activity) and checks every KPI's numerator, denominator and value
against figures computed independently from SQL (and, for duplicates, from the rows with the BRD
matching rules in Python and from the Duplicates page). Also: schema at alembic 0014_crm_kpis, role
scoping (workspace KPIs for admins and the Sales Head only), member= filter, amount hiding, activity
recorded on sign-in / API use, deactivated users not counted as seats, workspace isolation.

Runs against the live stack; creates its own workspaces (2 sign-ups).  python3 run_crm_kpis.py
Writes results.json / results.md next to this file.
"""
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("crm_kpis", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("ck")
OWNER = W.token
TID = W.tenant_id
rid = W.rid
HEAD = W.add_user("MANAGER", "Hana", sales_level=1)
MGR = W.add_user("MANAGER", "Milo", sales_level=2, manager_id=HEAD["user_id"])
REP1 = W.add_user("AGENT", "Remy", sales_level=3, manager_id=MGR["user_id"])
REP2 = W.add_user("AGENT", "Rosa", sales_level=3)
GONE = W.add_user("AGENT", "Gus")             # signs in once, then is deactivated: no longer a seat
sql(f"UPDATE users SET status = 'INACTIVE' WHERE user_id = '{GONE['user_id']}'")
W2 = Workspace("ck-b")


def kpis(tok, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    s, b = req("GET", "/sales-reports/kpis" + (f"?{q}" if q else ""), None, tok)
    return s, ({k["key"]: k for k in b["kpis"]} if s == 200 else b), b


def nd(k):
    return (k["numerator"], k["denominator"])


def num(v):
    return float(v) if v not in (None, "", "NULL") else 0.0


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def mk_contact(local, tok=OWNER, **kw):
    s, b = req("POST", "/contacts", {"email": f"{local}.{rid}@ck-{rid}.io", **kw}, tok)
    assert s == 201, (s, b)
    return b["prospect_id"]


# ── Schema ────────────────────────────────────────────────────────
head = sql("SELECT version_num FROM alembic_version")
cols = sql("SELECT GROUP_CONCAT(column_name ORDER BY column_name) FROM information_schema.columns WHERE table_schema = DATABASE() "
           "AND table_name = 'forecast_snapshots' AND column_name IN ('commit_open', 'committed')")
tbl = sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'user_activity_days'")
# At or past 0014_crm_kpis: later revisions (0015 session limits, 0016 optimistic concurrency) keep these objects
R.check("CK-001", "Schema is at alembic 0014_crm_kpis or later: committed columns on forecast_snapshots and the user_activity_days table",
        (head or "")[:4].isdigit() and int(head[:4]) >= 14 and cols == "commit_open,committed" and tbl == "1", (head, cols, tbl))

# ── Shape and access ──────────────────────────────────────────────
s, k, body = kpis(OWNER)
R.check("CK-002", "Owner gets all six KPIs in BRD order with value, numerator, denominator, target, period, note and definition",
        s == 200 and [x["key"] for x in body["kpis"]] == ["duplicate_contact_rate", "reply_to_lead", "lead_to_sql", "sql_to_win",
                                                         "forecast_accuracy", "crm_adoption"]
        and all({"value", "numerator", "denominator", "target", "period", "note", "definition", "formula"} <= set(x) for x in body["kpis"]),
        (s, str(body)[:300]))
R.check("CK-003", "Only the duplicate rate has a BRD target (< 1%)",
        s == 200 and k["duplicate_contact_rate"]["target"] == {"operator": "<", "value": 1.0, "label": "< 1%"}
        and all(k[x]["target"] is None for x in k if x != "duplicate_contact_rate"), str(body)[:300])
s1, k1, _ = kpis(REP1["token"])
s2, k2, _ = kpis(MGR["token"])
s3, k3, b3 = kpis(HEAD["token"])
R.check("CK-004", "Agents and level-2 managers get the workspace KPIs (duplicate rate, adoption) as unavailable",
        s1 == s2 == 200 and all(kk[x]["available"] is False and kk[x]["value"] is None for kk in (k1, k2)
                                for x in ("duplicate_contact_rate", "crm_adoption")), (k1.get("crm_adoption"), k2.get("crm_adoption")))
R.check("CK-005", "The Sales Head (level 1) sees the workspace KPIs", s3 == 200 and b3["workspace_view"] is True
        and k3["crm_adoption"]["available"] and k3["duplicate_contact_rate"]["available"], str(b3)[:200])
codes = (req("GET", "/sales-reports/kpis?quarter=2", None, OWNER)[0], req("GET", "/sales-reports/kpis?fy=1900", None, OWNER)[0],
         req("GET", f"/sales-reports/kpis?member={REP2['user_id']}", None, REP1["token"])[0],
         req("GET", "/sales-reports/kpis", None, None)[0])
R.check("CK-006", "Bad filters: quarter without fy 400, fy out of range 422, member outside your team 404, no token 401",
        codes == (400, 422, 404, 401), codes)

# ── Duplicate contact rate ───────────────────────────────────────
PHONE = f"{int(rid, 16) % 10 ** 7:07d}"   # unique per run
ids = {
    "a": mk_contact("robert", first_name="Robert", last_name="Smith", company_name=f"Dupco {rid}", owner_id=REP1["user_id"]),
    "b": mk_contact("rsmith", first_name="Rob", last_name="Smith", company_name=f"DUPCO {rid}"),
    "e": mk_contact("ph1", first_name="Pat", phone=f"+1 (555) {PHONE[:3]}-{PHONE[3:]}"),
    "g": mk_contact("uniq1", first_name="Grace", last_name="Hopper", company_name="Navy"),
    "h": mk_contact("uniq2", first_name="Alan", last_name="Turing", owner_id=REP1["user_id"]),
}
ids["f"] = mk_contact("ph2", first_name="Sam", mobile_phone=f"555.{PHONE[:3]}.{PHONE[3:]}")
for key, email in (("c", f"jo.doe.{rid}@gmail.com"), ("d", f"jodoe.{rid}+promo@gmail.com")):
    s, b = req("POST", "/contacts", {"email": email, "first_name": key.upper()}, OWNER)
    ids[key] = b["prospect_id"]
deleted = mk_contact("uniq1b", first_name="Grace", last_name="Hopper", company_name="Navy")
req("DELETE", f"/contacts/{deleted}", None, OWNER)


def expected_duplicates(owner_filter=""):
    """The BRD-CM-32 rules in Python over the live rows, independently of the KPI's SQL."""
    rows = sql("SELECT prospect_id, LOWER(email), IFNULL(first_name,''), IFNULL(last_name,''), IFNULL(company_name,''), "
               "IFNULL(account_id,''), IFNULL(phone,''), IFNULL(mobile_phone,'') FROM prospects "
               f"WHERE tenant_id = '{TID}' AND deleted_at IS NULL {owner_filter}")
    personal = {"gmail", "googlemail", "yahoo", "hotmail", "outlook", "icloud"}
    buckets = {}
    for line in filter(None, rows.split("\n")):
        pid, email, first, last, company, account, phone, mobile = (line.split("\t") + [""] * 8)[:8]
        norm = lambda v: re.sub(r"[^a-z0-9]", "", v.lower())  # noqa: E731
        local, _, domain = email.partition("@")
        local = local.split("+")[0]
        if not domain or domain.split(".")[0] in personal:
            local = local.replace(".", "")
        buckets.setdefault(("e", f"{local}@{domain}"), set()).add(pid)
        f, l, c = norm(first), norm(last), (account or norm(company))
        if f and l and c:
            buckets.setdefault(("n", f, l, c), set()).add(pid)
            if len(f) > 1:
                buckets.setdefault(("i", f[0], l, c), set()).add(pid)
        for p in {re.sub(r"\D", "", phone), re.sub(r"\D", "", mobile)}:
            if len(p) >= 7:
                buckets.setdefault(("p", p[-10:]), set()).add(pid)
    dup = set().union(*[v for v in buckets.values() if len(v) > 1] or [set()])
    return len(dup), len([x for x in rows.split("\n") if x])


time.sleep(1.1)
s, k, _ = kpis(OWNER)
exp = expected_duplicates()
R.check("CK-010", "Duplicate contact rate: numerator / denominator match the BRD rules applied to the rows from SQL",
        s == 200 and nd(k["duplicate_contact_rate"]) == exp and k["duplicate_contact_rate"]["value"] == pct(*exp), (nd(k["duplicate_contact_rate"]), exp))
R.check("CK-011", "The dataset's duplicates: similar name at the same company, Gmail dots/+tag, same phone (6 of 8 live; deleted one ignored)",
        exp == (6, 8) and k["duplicate_contact_rate"]["meets_target"] is False, exp)
s, groups = req("GET", "/contacts/duplicates?limit=200", None, OWNER)
in_groups = {c["prospect_id"] for g in groups.get("groups", []) for c in g["contacts"]} if s == 200 else set()
R.check("CK-012", "The contacts counted are exactly those the Duplicates page groups", in_groups == {ids[x] for x in "abcdef"},
        (len(in_groups), sorted(in_groups ^ {ids[x] for x in "abcdef"})[:4]))
s, k, _ = kpis(OWNER, member=REP1["user_id"])
exp1 = expected_duplicates(f"AND owner_id = '{REP1['user_id']}'")
R.check("CK-013", "member= narrows the duplicate rate to that person's contacts (their duplicate partner is someone else's)",
        s == 200 and nd(k["duplicate_contact_rate"]) == exp1 == (0, 2) and k["duplicate_contact_rate"]["meets_target"] is True,
        (nd(k.get("duplicate_contact_rate", {})), exp1))
s, b = req("POST", "/contacts/merge", {"primary_id": ids["c"], "duplicate_ids": [ids["d"]]}, OWNER)
s2, k, _ = kpis(OWNER)
R.check("CK-014", "Merging a duplicate group resolves it: 4 of 7", s == 200 and nd(k["duplicate_contact_rate"]) == expected_duplicates() == (4, 7),
        (s, nd(k["duplicate_contact_rate"]), expected_duplicates()))

# ── Reply-to-lead conversion ─────────────────────────────────────
PERIOD_START = (datetime.now(timezone.utc) - timedelta(days=89)).date()


def positive_reply(pid, days_ago):
    mid = str(uuid.uuid4())
    sql(f"INSERT INTO email_messages (message_id, prospect_id, tenant_id, direction, status, to_email, subject, sent_at) "
        f"SELECT '{mid}', prospect_id, tenant_id, 'OUTBOUND', 'SENT', 'x@example.com', 'Hi', "
        f"UTC_TIMESTAMP() - INTERVAL {days_ago + 1} DAY FROM prospects WHERE prospect_id = '{pid}'")
    for etype in (4, 8):
        sql(f"INSERT INTO email_events (event_id, message_id, tenant_id, event_type, event_time) "
            f"SELECT '{uuid.uuid4()}', message_id, tenant_id, {etype}, UTC_TIMESTAMP() - INTERVAL {days_ago} DAY "
            f"FROM email_messages WHERE message_id = '{mid}'")
    return mid


rc = {i: mk_contact(f"reply{i}", first_name=f"Reply{i}", owner_id=(REP1 if i == 1 else REP2)["user_id"]) for i in range(1, 8)}
for i in (1, 2, 3, 4):
    positive_reply(rc[i], 5)
positive_reply(rc[1], 4)                       # a second positive reply from the same contact
positive_reply(rc[5], 200)                     # outside the period
inbound = str(uuid.uuid4())
sql(f"INSERT INTO email_messages (message_id, prospect_id, tenant_id, direction, status, to_email, subject, body_text, sent_at) "
    f"SELECT '{inbound}', prospect_id, tenant_id, 'INBOUND', 'RECEIVED', 'me@example.com', 'Re: Hi', 'Sounds good, call me', "
    f"UTC_TIMESTAMP() - INTERVAL 5 DAY FROM prospects WHERE prospect_id = '{rc[1]}'")
s1, l1 = req("POST", "/leads/from-message", {"message_id": inbound}, OWNER)
s2, _ = req("POST", "/leads", {"prospect_id": rc[2], "source": "Referral"}, OWNER)
s3, _ = req("POST", "/leads", {"prospect_id": rc[6], "source": "Campaign reply"}, OWNER)
s4, _ = req("POST", "/leads", {"prospect_id": rc[7], "source": "Website"}, OWNER)
R.check("CK-020", "Leads from a reply (one-click, source 'Campaign reply'), by hand after a reply, and unrelated leads are created",
        (s1, s2, s3, s4) == (201, 201, 201, 201) and l1.get("source") == "Campaign reply", (s1, s2, s3, s4, l1))


def sql_reply_to_lead(owner_ids=None):
    lo = f"AND l.owner_id IN ({','.join(repr(o) for o in owner_ids)})" if owner_ids else ""
    po = f"AND p.owner_id IN ({','.join(repr(o) for o in owner_ids)})" if owner_ids else ""
    leads = sql(f"""SELECT COUNT(*) FROM leads l WHERE l.tenant_id = '{TID}' AND l.created_at >= '{PERIOD_START}' {lo}
                    AND (l.source = 'Campaign reply' OR EXISTS (SELECT 1 FROM email_messages m JOIN email_events e ON e.message_id = m.message_id
                         WHERE m.prospect_id = l.prospect_id AND e.event_type = 8 AND e.event_time <= l.created_at
                         AND e.event_time >= l.created_at - INTERVAL 30 DAY))""")
    pos = sql(f"""SELECT COUNT(DISTINCT m.prospect_id) FROM email_events e JOIN email_messages m ON m.message_id = e.message_id
                  JOIN prospects p ON p.prospect_id = m.prospect_id WHERE p.tenant_id = '{TID}' AND e.event_type = 8
                  AND e.event_time >= '{PERIOD_START}' {po}""")
    return int(leads), int(pos)


s, k, _ = kpis(OWNER)
exp = sql_reply_to_lead()
R.check("CK-021", "Reply-to-lead: leads from replies / contacts with a positive reply match SQL (3 / 4 = 75%)",
        s == 200 and nd(k["reply_to_lead"]) == exp == (3, 4) and k["reply_to_lead"]["value"] == 75.0, (nd(k["reply_to_lead"]), exp))
s, k, _ = kpis(REP2["token"])
exp = sql_reply_to_lead([REP2["user_id"]])
R.check("CK-022", "An agent's reply-to-lead covers their own leads and contacts (2 / 3)", s == 200 and nd(k["reply_to_lead"]) == exp == (2, 3),
        (nd(k["reply_to_lead"]), exp))
s, k, _ = kpis(W2.token)
R.check("CK-023", "No positive replies: value null with a note (the stack runs without an AI classifier key)",
        s == 200 and k["reply_to_lead"]["value"] is None and k["reply_to_lead"]["denominator"] == 0
        and "not configured" in (k["reply_to_lead"]["note"] or ""), k["reply_to_lead"])

# ── Lead-to-SQL and SQL-to-win ───────────────────────────────────
req("GET", "/sales/stages", None, OWNER)       # seeds the default stages
won_stage = sql(f"SELECT stage_id FROM sales_stages WHERE tenant_id = '{TID}' AND is_won = 1 LIMIT 1")
for i, won in enumerate((True, True, False)):
    p = mk_contact(f"sql{i}", first_name=f"Sql{i}", owner_id=REP1["user_id"])
    s, lead = req("POST", "/leads", {"prospect_id": p, "source": "Test"}, OWNER)
    sql(f"UPDATE leads SET stage = 'SQL', qualified_at = UTC_TIMESTAMP() - INTERVAL 3 DAY WHERE lead_id = '{lead['lead_id']}'")
    if won:
        s, o = req("POST", "/opportunities", {"name": f"Won {i} {rid}", "prospect_id": p, "owner_id": REP1["user_id"],
                                              "stage_id": won_stage, "amount": "500", "close_date": date.today().isoformat(),
                                              "closed_reason": "Price"}, OWNER)
        sql(f"UPDATE opportunities SET lead_id = '{lead['lead_id']}' WHERE opportunity_id = '{o['opportunity_id']}'")
s, k, _ = kpis(OWNER)
l_sqls = int(sql(f"SELECT COUNT(*) FROM leads WHERE tenant_id = '{TID}' AND created_at >= '{PERIOD_START}' AND qualified_at IS NOT NULL"))
l_all = int(sql(f"SELECT COUNT(*) FROM leads WHERE tenant_id = '{TID}' AND created_at >= '{PERIOD_START}'"))
R.check("CK-030", "Lead-to-SQL: SQLs among the period's new leads / new leads match SQL (3 / 7)",
        nd(k["lead_to_sql"]) == (l_sqls, l_all) == (3, 7) and k["lead_to_sql"]["value"] == pct(3, 7), (nd(k["lead_to_sql"]), l_sqls, l_all))
q_sqls = int(sql(f"SELECT COUNT(*) FROM leads WHERE tenant_id = '{TID}' AND qualified_at >= '{PERIOD_START}'"))
q_won = int(sql(f"SELECT COUNT(DISTINCT l.lead_id) FROM leads l JOIN opportunities o ON o.lead_id = l.lead_id WHERE l.tenant_id = '{TID}' "
                f"AND l.qualified_at >= '{PERIOD_START}' AND o.status = 'WON'"))
s, s2w = req("GET", "/sales-reports/sql-to-win", None, OWNER)
R.check("CK-031", "SQL-to-win: won SQLs / SQLs match SQL and the SQL-to-win report (2 / 3)",
        nd(k["sql_to_win"]) == (q_won, q_sqls) == (s2w["won"], s2w["sqls"]) == (2, 3) and k["sql_to_win"]["value"] == 66.7,
        (nd(k["sql_to_win"]), q_won, q_sqls))
s, leads_rep = req("GET", "/sales-reports/leads", None, OWNER)
R.check("CK-032", "Lead-to-SQL equals the Leads & SQLs report's rate", k["lead_to_sql"]["value"] == leads_rep["lead_to_sql_rate"],
        (k["lead_to_sql"]["value"], leads_rep.get("lead_to_sql_rate")))

# ── Forecast accuracy ────────────────────────────────────────────
s, fc = req("GET", "/sales-reports/forecast", None, OWNER)
start_month = fc["fiscal_year_start_month"]


def add_months(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


today = date.today()
fy = today.year if today.month >= start_month else today.year - 1
qn = ((today.month - start_month) % 12) // 3 + 1
cur_q = add_months(date(fy, start_month, 1), 3 * (qn - 1))
last_q = add_months(cur_q, -3)
for owner, committed, commit_open in ((REP1, 1000, 600), (REP2, 3000, 3000)):
    sql(f"INSERT INTO forecast_snapshots (snapshot_id, tenant_id, period_type, period_start, period_end, owner_id, won, weighted, forecast, "
        f"open_count, commit_open, committed, source, taken_at) VALUES ('{uuid.uuid4()}', '{TID}', 'QUARTER', '{last_q}', '{add_months(last_q, 3)}', "
        f"'{owner['user_id']}', {committed - commit_open}, 0, {committed - commit_open}, 1, {commit_open}, {committed}, 'SCHEDULED', '{last_q} 00:00:00')")
for owner, amount, close in ((REP1, 1200, last_q + timedelta(days=5)), (REP2, 1800, last_q + timedelta(days=40)),
                             (REP2, 777, last_q - timedelta(days=2))):
    s, _ = req("POST", "/opportunities", {"name": f"Q {uuid.uuid4().hex[:5]}", "owner_id": owner["user_id"], "stage_id": won_stage,
                                          "amount": str(amount), "close_date": close.isoformat(), "closed_reason": "Price"}, OWNER)
s, k, _ = kpis(OWNER)
committed = num(sql(f"SELECT SUM(committed) FROM forecast_snapshots WHERE tenant_id = '{TID}' AND period_type = 'QUARTER' AND period_start = '{last_q}'"))
actual = num(sql(f"SELECT SUM(amount) FROM opportunities WHERE tenant_id = '{TID}' AND status = 'WON' AND close_date >= '{last_q}' "
                 f"AND close_date < '{add_months(last_q, 3)}'"))
fa = k["forecast_accuracy"]
R.check("CK-040", "Forecast accuracy for the last finished quarter = won in it / committed at its start, from SQL (3000 / 4000 = 75%)",
        (fa["numerator"], fa["denominator"]) == (actual, committed) == (3000.0, 4000.0) and fa["value"] == 75.0
        and fa["period"]["from"] == last_q.isoformat(), (fa["numerator"], fa["denominator"], fa["value"], actual, committed, str(last_q)))
s, k, _ = kpis(OWNER, member=REP1["user_id"])
R.check("CK-041", "Forecast accuracy can exceed 100% (rep 1: 1200 won / 1000 committed = 120%)", k["forecast_accuracy"]["value"] == 120.0,
        k["forecast_accuracy"])
s, acc = req("GET", "/sales-reports/forecast-accuracy", None, OWNER)
row = next((p for p in acc.get("periods", []) if p["period_start"] == last_q.isoformat()), {})
R.check("CK-042", "The forecast-accuracy report uses the same BRD formula (accuracy = actual / committed) and keeps the weighted measure as secondary",
        (row.get("committed"), row.get("actual"), row.get("accuracy")) == (4000.0, 3000.0, 75.0) and "weighted_accuracy" in row, row)
# Current quarter: the snapshot stores the committed figure (won to date + open Commit deals)
neg = sql(f"SELECT stage_id FROM sales_stages WHERE tenant_id = '{TID}' AND probability >= 70 AND is_won = 0 AND is_lost = 0 ORDER BY probability LIMIT 1")
prop = sql(f"SELECT stage_id FROM sales_stages WHERE tenant_id = '{TID}' AND probability BETWEEN 40 AND 69 AND is_won = 0 ORDER BY probability LIMIT 1")
for stage, amount, cat in ((neg, 5000, None), (prop, 7000, None), (prop, 2000, "COMMIT"), (neg, 900, "OMITTED")):
    body = {"name": f"C {uuid.uuid4().hex[:5]}", "owner_id": REP2["user_id"], "stage_id": stage, "amount": str(amount),
            "close_date": today.isoformat()}
    if cat:
        body["forecast_category"] = cat
    req("POST", "/opportunities", body, OWNER)
s, snap = req("POST", "/sales-reports/forecast-snapshots", {"period_type": "QUARTER", "replace": True}, OWNER)
stored = sql(f"SELECT CONCAT(SUM(commit_open), '|', SUM(committed)) FROM forecast_snapshots WHERE tenant_id = '{TID}' AND period_type = 'QUARTER' "
             f"AND period_start = '{cur_q}'")
open_commit = num(sql(f"SELECT SUM(amount) FROM opportunities WHERE tenant_id = '{TID}' AND status = 'OPEN' AND forecast_category = 'COMMIT' "
                      f"AND close_date >= '{cur_q}' AND close_date < '{add_months(cur_q, 3)}'"))
won_q = num(sql(f"SELECT SUM(amount) FROM opportunities WHERE tenant_id = '{TID}' AND status = 'WON' AND close_date >= '{cur_q}' "
                f"AND close_date < '{add_months(cur_q, 3)}'"))
co, cm = (num(x) for x in (stored or "0|0").split("|"))
R.check("CK-043", "A snapshot stores committed = won to date + open Commit deals (stage default or set by hand; Omitted excluded)",
        s == 201 and co == open_commit == 7000.0 and cm == won_q + open_commit, (s, stored, open_commit, won_q))
s, k, _ = kpis(OWNER, fy=fy, quarter=qn)
fa = k["forecast_accuracy"]
R.check("CK-044", "The current quarter by fy/quarter is in progress: won so far / committed, with a note",
        s == 200 and fa["in_progress"] is True and fa["denominator"] == cm and fa["value"] == pct(won_q, cm) and "in progress" in fa["note"],
        (s, fa.get("denominator"), fa.get("value"), fa.get("note"), cm, won_q))
s, k, _ = kpis(OWNER, fy=fy, quarter=qn, member=W.user_id)   # the owner has no deals: nothing committed
R.check("CK-045", "A quarter with nothing committed in scope gives null with a note", k["forecast_accuracy"]["value"] is None
        and k["forecast_accuracy"]["note"], k["forecast_accuracy"])

# Amount hiding
req("PUT", "/sales/settings", {"amount_hidden_roles": ["AGENT"]}, OWNER)
s, k, b = kpis(REP1["token"])
fa = k.get("forecast_accuracy", {})
R.check("CK-046", "Hidden amounts: an agent sees the accuracy % but not revenue or committed amounts",
        s == 200 and b["amounts_hidden"] and fa["numerator"] is None and fa["denominator"] is None and fa["value"] == 120.0
        and all(q["committed"] is None and q["actual"] is None for q in fa["quarters"]), fa)
req("PUT", "/sales/settings", {"amount_hidden_roles": []}, OWNER)

# ── CRM adoption ─────────────────────────────────────────────────
monday = today - timedelta(days=today.weekday())
prev = monday - timedelta(days=7)
for user, day in ((REP1, prev), (REP2, prev + timedelta(days=6)), (GONE, prev), (MGR, prev - timedelta(days=8))):
    sql(f"INSERT IGNORE INTO user_activity_days (tenant_id, user_id, day) VALUES ('{TID}', '{user['user_id']}', '{day}')")
s, k, _ = kpis(OWNER)
ad = k["crm_adoption"]
licensed = int(sql(f"SELECT COUNT(*) FROM users WHERE tenant_id = '{TID}' AND status = 'ACTIVE' AND role <> 'PLATFORM_ADMIN'"))


def active_in(start, end):
    return int(sql(f"SELECT COUNT(DISTINCT a.user_id) FROM user_activity_days a JOIN users u ON u.user_id = a.user_id "
                   f"WHERE a.tenant_id = '{TID}' AND u.status = 'ACTIVE' AND a.day >= '{start}' AND a.day < '{end}'"))


this_week = active_in(monday, monday + timedelta(days=7))
R.check("CK-050", "Adoption this week = users active this week / licensed seats, from SQL (5 / 5; the deactivated user is no seat)",
        nd(ad) == (this_week, licensed) == (5, 5) and ad["value"] == 100.0, (nd(ad), this_week, licensed))
trend = {w["week_start"]: w for w in ad.get("trend", [])}
R.check("CK-051", "Adoption trend: 9 weeks, last week 2 / 5 = 40% (deactivated user ignored), Sunday counted in its own week",
        len(trend) == 9 and trend.get(prev.isoformat(), {}).get("active_users") == active_in(prev, monday) == 2
        and trend[prev.isoformat()]["value"] == 40.0 and trend.get((prev - timedelta(days=14)).isoformat(), {}).get("active_users") == 1,
        {d: w["active_users"] for d, w in trend.items()})
s, k, _ = kpis(OWNER, member=MGR["user_id"])
R.check("CK-052", "member= narrows adoption to that team's seats (manager + rep 1)", nd(k["crm_adoption"]) == (2, 2), nd(k["crm_adoption"]))
# Activity is recorded on the first authenticated request of the day
fresh = W.add_user("AGENT", "Fern")
n1 = sql(f"SELECT COUNT(*) FROM user_activity_days WHERE user_id = '{fresh['user_id']}' AND day = UTC_DATE()")
req("GET", "/api/auth/me", None, fresh["token"])
req("GET", "/api/auth/me", None, fresh["token"])
n2 = sql(f"SELECT COUNT(*) FROM user_activity_days WHERE user_id = '{fresh['user_id']}'")
R.check("CK-053", "Signing in and using the API records one activity row per user per day", n1 == "1" and n2 == "1", (n1, n2))

# ── Isolation and cost ───────────────────────────────────────────
s, k, b = kpis(W2.token)
R.check("CK-060", "Another workspace sees only its own numbers (1 seat, no contacts, no leads)",
        s == 200 and nd(k["crm_adoption"]) == (1, 1) and nd(k["duplicate_contact_rate"]) == (0, 0) and nd(k["lead_to_sql"]) == (0, 0),
        (nd(k["crm_adoption"]), nd(k["duplicate_contact_rate"]), nd(k["lead_to_sql"])))
s, _ = req("GET", f"/sales-reports/kpis?member={REP1['user_id']}", None, W2.token)
R.check("CK-061", "Another workspace cannot report on this workspace's people (404)", s == 404, s)
t0 = time.time()
s, _, _ = kpis(OWNER)
R.check("CK-062", "The KPI call answers quickly (< 3 s)", s == 200 and time.time() - t0 < 3, round(time.time() - t0, 2))

R.write()
c = {k: sum(1 for r in R.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")}
print("COUNTS", c)
