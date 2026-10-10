#!/usr/bin/env python3
"""
System & integration suite for BRD v2.0 sections 4 (personas), 5.4 (sales hierarchy),
5.5 (daily new-contact limit), 5.6 (leads & SQL) and 5.7 (sales pipeline), plus the
Salesforce-style "should" items that hang off them (BR-SF-01..05, 12, 15).

Runs against the live stack (API_URL / WEB_URL env, see ../common.py). Every run creates
its own workspaces (tenants), so it is re-runnable and never touches other data.
Case IDs match TEST_CASES.md.   Usage:  python3 run_sales_pipeline.py [--no-ui] [--no-worker]
"""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import WEB, Results, Workspace, req, sql  # noqa: E402

R = Results("Sales hierarchy, daily limit, leads & pipeline (BRD 5.4-5.7)", HERE)
NO_UI = "--no-ui" in sys.argv
NO_WORKER = "--no-worker" in sys.argv
PW_MODULES = os.environ.get("PW_MODULES", "/tmp/claude-0/-home-user/b764cdf9-b625-5070-aae1-e6d130b3e7df/scratchpad")
HELD = "DAILY_NEW_CONTACT_LIMIT"


# ───────────────────────────── helpers ─────────────────────────────

def ok(s, expect=(200, 201)):
    return s in ((expect,) if isinstance(expect, int) else expect)


def must(resp, expect=(200, 201), what=""):
    s, b = resp
    if not ok(s, expect):
        raise AssertionError(f"setup failed {what}: HTTP {s} {str(b)[:300]}")
    return b


def guarded(case_id, title):
    """Decorator-ish: run a block, record BLOCKED with the exception if setup breaks."""
    def wrap(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            R.record(case_id, title, None, f"blocked by setup error: {e!r}", "test-issue")
    return wrap


def q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def ids_of(items, key):
    return {i[key] for i in items}


def poll(fn, timeout=170, every=5):
    """Bounded wait for the worker (jobs run every 60 s)."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    return last


def stage_by_name(token):
    return {s["name"]: s for s in must(req("GET", "/sales/stages", token=token))}


def contact(user, first, last, company=None, owner_id=None, **extra):
    body = {"email": f"{first.lower()}.{last.lower()}@{(company or 'nocompany').lower().replace(' ', '')}-sp.example",
            "first_name": first, "last_name": last, **extra}
    if company:
        body["company_name"] = company
    if owner_id:
        body["owner_id"] = owner_id
    return must(req("POST", "/contacts", body, user["token"]), 201, f"contact {first} {last}")


def lead(user, prospect_id, **body):
    return must(req("POST", "/leads", {"prospect_id": prospect_id, **body}, user["token"]), 201, "lead")


def bant(user, lead_id, **extra):
    return req("PATCH", f"/leads/{lead_id}", {"qualification": {"budget": True, "authority": True, "need": True,
                                                                  "timeline": True}, **extra}, user["token"])


def to_sql(user, lead_id):
    must(bant(user, lead_id), 200, "bant")
    return must(req("PATCH", f"/leads/{lead_id}", {"stage": "SQL", "next_step": "Book demo"}, user["token"]), 200, "sql")


def opp(user, name, prospect_id=None, amount=None, **extra):
    body = {"name": name, **extra}
    if prospect_id:
        body["prospect_id"] = prospect_id
    if amount is not None:
        body["amount"] = amount
    return must(req("POST", "/opportunities", body, user["token"]), 201, f"opp {name}")


def contact_detail(token, pid):
    return req("GET", f"/contacts/{pid}", token=token)


def all_opps(token, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return must(req("GET", f"/opportunities?page_size=500&{qs}", token=token))["items"]


def all_leads(token, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return must(req("GET", f"/leads?page_size=200&{qs}", token=token))["items"]


def all_contacts(token):
    out, page = [], 1
    while True:
        b = must(req("GET", f"/contacts?page_size=200&page={page}", token=token))
        out += b["items"]
        if len(out) >= b["total"] or not b["items"]:
            return out
        page += 1


# ───────────────────────────── fixtures ─────────────────────────────

print("Setting up workspaces and the sales hierarchy ...")
A = Workspace("salespipe")
B = Workspace("salespipeb")
RID = A.rid
PROSPECT_MGR_PERMS = '["manage_campaigns","manage_templates","view_analytics","manage_prospects","export_data"]'

U = {}
U["head"] = A.add_user("MANAGER", f"Head{RID}")
U["mgr1"] = A.add_user("MANAGER", f"Mgrone{RID}", custom_permissions=PROSPECT_MGR_PERMS)
U["mgr2"] = A.add_user("MANAGER", f"Mgrtwo{RID}")
U["ag1a"] = A.add_user("AGENT", f"Agonea{RID}")
U["ag1b"] = A.add_user("AGENT", f"Agoneb{RID}")
U["ag2a"] = A.add_user("AGENT", f"Agtwoa{RID}")
U["mr1"] = A.add_user("AGENT", f"Research{RID}")
U["dlm"] = A.add_user("MANAGER", f"Dlmgr{RID}")
U["dl1"] = A.add_user("AGENT", f"Dlone{RID}")
U["dl2"] = A.add_user("AGENT", f"Dltwo{RID}")
U["owner"] = {"email": A.email, "user_id": A.user_id, "token": A.token}
UB = {"owner": {"email": B.email, "user_id": B.user_id, "token": B.token}}
UB["agent"] = B.add_user("AGENT", f"Bagent{RID}")
NAME = {k: v["email"].split("-")[0] for k, v in U.items()}

HIER = [("head", 1, None), ("mgr1", 2, "head"), ("mgr2", 2, "head"), ("ag1a", 3, "mgr1"), ("ag1b", 3, "mgr1"),
        ("ag2a", 3, "mgr2"), ("mr1", 4, "mgr1"), ("dlm", 2, "head"), ("dl1", 3, "dlm"), ("dl2", 3, "dlm")]


# ═══════════════════════ 5.4 Sales hierarchy ═══════════════════════

@guarded("SP-001", "Admin configures the 4-level hierarchy (L1..L4) via the API")
def _():
    bad = []
    for key, level, mgr in HIER:
        s, b = req("PUT", f"/team/hierarchy/{U[key]['user_id']}",
                   {"sales_level": level, "manager_id": U[mgr]["user_id"] if mgr else None}, A.token)
        if s != 200 or b.get("sales_level") != level:
            bad.append((key, s, b))
    s, h = req("GET", "/team/hierarchy", token=A.token)
    levels = {l["level"]: l["label"] for l in h.get("levels", [])} if s == 200 else {}
    rows = {u["user_id"]: u for u in h.get("users", [])} if s == 200 else {}
    for key, level, mgr in HIER:
        r = rows.get(U[key]["user_id"], {})
        if r.get("sales_level") != level or r.get("manager_id") != (U[mgr]["user_id"] if mgr else None):
            bad.append(("readback", key, r))
    R.check("SP-001", "Admin configures the 4-level hierarchy (L1..L4) via the API",
            not bad and sorted(levels) == [1, 2, 3, 4] and "Market Research" in levels.get(4, ""),
            f"bad={bad[:3]} levels={levels}")


@guarded("SP-002", "Hierarchy validation rejects invalid configurations")
def _():
    cases = {
        "L3 without manager": req("PUT", f"/team/hierarchy/{U['ag1b']['user_id']}", {"sales_level": 3}, A.token),
        "manager at same level": req("PUT", f"/team/hierarchy/{U['ag1b']['user_id']}",
                                     {"sales_level": 3, "manager_id": U["ag1a"]["user_id"]}, A.token),
        "manager below (would invert)": req("PUT", f"/team/hierarchy/{U['mgr1']['user_id']}",
                                            {"sales_level": 2, "manager_id": U["ag1a"]["user_id"]}, A.token),
        "level 5": req("PUT", f"/team/hierarchy/{U['ag1b']['user_id']}",
                       {"sales_level": 5, "manager_id": U["mgr1"]["user_id"]}, A.token),
        "remove manager who has a team": req("PUT", f"/team/hierarchy/{U['mgr1']['user_id']}",
                                             {"sales_level": None, "manager_id": None}, A.token),
        "manager from another tenant": req("PUT", f"/team/hierarchy/{U['ag1b']['user_id']}",
                                           {"sales_level": 3, "manager_id": UB["owner"]["user_id"]}, A.token),
    }
    wrong = {k: (s, b) for k, (s, b) in cases.items() if s != 400}
    R.check("SP-002", "Hierarchy validation rejects invalid configurations (400)", not wrong, f"{wrong}")


@guarded("SP-003", "Only admins may change the hierarchy; other tenants cannot")
def _():
    r1 = req("PUT", f"/team/hierarchy/{U['ag1a']['user_id']}", {"sales_level": 3, "manager_id": U["mgr2"]["user_id"]},
             U["mgr1"]["token"])
    r2 = req("PUT", f"/team/hierarchy/{U['ag1a']['user_id']}", {"sales_level": 3, "manager_id": U["mgr1"]["user_id"]},
             U["head"]["token"])
    r3 = req("PUT", f"/team/hierarchy/{U['ag1a']['user_id']}", {"sales_level": 1}, B.token)
    unchanged = sql(f"SELECT CONCAT(sales_level,'/',manager_id) FROM users WHERE user_id='{U['ag1a']['user_id']}'")
    R.check("SP-003", "Only admins may change the hierarchy; other tenants cannot (403/403/404)",
            r1[0] == 403 and r2[0] == 403 and r3[0] == 404 and unchanged == f"3/{U['mgr1']['user_id']}",
            f"mgr={r1[0]} head={r2[0]} otherTenant={r3[0]} db={unchanged}")


@guarded("SP-004", "Team directory is limited to the viewer's team")
def _():
    def seen(k):
        s, b = req("GET", "/team/hierarchy", token=U[k]["token"])
        return {u["user_id"] for u in b.get("users", [])} if s == 200 else {"HTTP", s}
    uid = lambda *ks: {U[k]["user_id"] for k in ks}  # noqa: E731
    ag, mg, hd = seen("ag1a"), seen("mgr1"), seen("head")
    everyone = {v["user_id"] for v in U.values()}
    R.check("SP-004", "Team directory: exec sees self, BD sees own team, Sales Head sees all",
            ag == uid("ag1a") and mg == uid("mgr1", "ag1a", "ag1b", "mr1") and everyone <= hd,
            f"ag1a={len(ag)} mgr1={len(mg)} head={len(hd)}")


# Records owned across the hierarchy
REC = {}


@guarded("SP-010", "Fixture: each user owns a contact, a lead and a deal")
def _():
    stages = stage_by_name(U["owner"]["token"])
    REC["stages"] = stages
    amounts = {"ag1a": 10000, "ag1b": 20000, "ag2a": 40000, "mgr1": 5000, "mgr2": 7000}
    for k in ("ag1a", "ag1b", "ag2a", "mr1", "mgr1", "mgr2"):
        c = contact(U[k], "Vis", f"{k}{RID}", company=f"Co {k} {RID}")
        rec = {"contact": c["prospect_id"], "last": f"{k}{RID}"}
        if k != "mr1":  # Market Research only researches contacts
            rec["lead"] = lead(U[k], c["prospect_id"], source="Visibility test")["lead_id"]
            o = opp(U[k], f"Deal {k} {RID}", c["prospect_id"], amounts[k],
                    close_date=str(date.today() + timedelta(days=30)))
            rec["opp"] = o["opportunity_id"]
            rec["opp_name"] = o["name"]
        REC[k] = rec
    # one SQL lead per team for the SQL queue checks
    for k in ("ag1a", "ag2a"):
        c = contact(U[k], "Queue", f"q{k}{RID}", company=f"Qco {k} {RID}")
        l = lead(U[k], c["prospect_id"], source="Queue test")
        to_sql(U[k], l["lead_id"])
        REC[k]["sql_lead"] = l["lead_id"]
        REC[k]["sql_last"] = f"q{k}{RID}"
    REC["proposal_ag2a"] = must(req("POST", "/proposals", {"opportunity_id": REC["ag2a"]["opp"], "title": "P ag2a"},
                                    U["ag2a"]["token"]), 201)["proposal_id"]
    R.check("SP-010", "Fixture: users across the hierarchy own contacts, leads and deals", True)


VISIBLE = {
    "ag1a": {"ag1a"}, "ag1b": {"ag1b"}, "ag2a": {"ag2a"}, "mr1": {"mr1"},
    "mgr1": {"mgr1", "ag1a", "ag1b", "mr1"}, "mgr2": {"mgr2", "ag2a"},
    "head": {"ag1a", "ag1b", "ag2a", "mr1", "mgr1", "mgr2"}, "owner": {"ag1a", "ag1b", "ag2a", "mr1", "mgr1", "mgr2"},
}
OWNERS = ["ag1a", "ag1b", "ag2a", "mr1", "mgr1", "mgr2"]


def visibility_case(case_id, viewer, kind):
    title = f"{viewer} {kind} visibility: open-by-URL and list match the hierarchy"

    @guarded(case_id, title)
    def _():
        tok = U[viewer]["token"]
        errs = []
        path = {"contact": "/contacts/", "lead": "/leads/", "opp": "/opportunities/"}[kind]
        for o in OWNERS:
            if kind not in REC[o]:
                continue
            s, _b = req("GET", path + REC[o][kind], token=tok)
            exp = 200 if o in VISIBLE[viewer] else 404
            if s != exp:
                errs.append(f"GET {kind} of {o}: {s} (want {exp})")
        if kind == "contact":
            listed = ids_of(all_contacts(tok), "prospect_id")
        elif kind == "lead":
            listed = ids_of(all_leads(tok), "lead_id")
        else:
            listed = ids_of(all_opps(tok), "opportunity_id")
        for o in OWNERS:
            if kind not in REC[o]:
                continue
            if (REC[o][kind] in listed) != (o in VISIBLE[viewer]):
                errs.append(f"list {kind} of {o}: listed={REC[o][kind] in listed}")
        R.check(case_id, title, not errs, "; ".join(errs))


n = 20
for viewer in ("ag1a", "mgr1", "mgr2", "head", "mr1"):
    for kind in ("contact", "lead", "opp"):
        n += 1
        visibility_case(f"SP-{n:03d}", viewer, kind)
# SP-021..SP-035


@guarded("SP-036", "Exec cannot find a peer's records by search")
def _():
    t = U["ag1a"]["token"]
    peer = REC["ag1b"]["last"]
    hits = {
        "contacts": must(req("GET", f"/contacts?q={peer}", token=t))["total"],
        "leads": must(req("GET", f"/leads?q={peer}", token=t))["total"],
        "opps": must(req("GET", f"/opportunities?q=Deal%20ag1b%20{RID}", token=t))["total"],
    }
    own = REC["ag1a"]["lead"] in ids_of(must(req("GET", f"/leads?q={REC['ag1a']['last']}", token=t))["items"], "lead_id")
    R.check("SP-036", "Exec cannot find a peer's contact/lead/deal by search (BR-SH-02 acceptance)",
            hits == {"contacts": 0, "leads": 0, "opps": 0} and own, f"peer hits={hits} own={own}")


@guarded("SP-037", "Exec cannot act on a peer's records by URL")
def _():
    t = U["ag1a"]["token"]
    r = {
        "patch lead": req("PATCH", f"/leads/{REC['ag1b']['lead']}", {"next_step": "x"}, t)[0],
        "convert lead": req("POST", f"/leads/{REC['ag2a']['sql_lead']}/convert", {}, t)[0],
        "delete lead": req("DELETE", f"/leads/{REC['ag1b']['lead']}", token=t)[0],
        "patch opp": req("PATCH", f"/opportunities/{REC['ag1b']['opp']}", {"next_step": "x"}, t)[0],
        "opp timeline": req("GET", f"/opportunities/{REC['ag1b']['opp']}/timeline", token=t)[0],
        "log activity": req("POST", f"/opportunities/{REC['ag1b']['opp']}/activities",
                            {"activity_type": "NOTE", "subject": "x"}, t)[0],
        "proposal on peer opp": req("POST", "/proposals", {"opportunity_id": REC["ag2a"]["opp"]}, t)[0],
        "patch peer proposal": req("PATCH", f"/proposals/{REC['proposal_ag2a']}", {"status": "SENT"}, t)[0],
        "lead on peer contact": req("POST", "/leads", {"prospect_id": REC["ag1b"]["contact"]}, t)[0],
        "patch peer contact": req("PATCH", f"/contacts/{REC['ag1b']['contact']}", {"designation": "x"}, t)[0],
    }
    wrong = {k: v for k, v in r.items() if v != 404}
    still = sql(f"SELECT next_step FROM leads WHERE lead_id='{REC['ag1b']['lead']}'")
    R.check("SP-037", "Exec cannot read/edit/convert/delete a peer's records by URL (404)",
            not wrong and still in ("", "NULL"), f"{wrong} lead.next_step={still!r}")


@guarded("SP-038", "Exec cannot attach a peer's contact to a new deal")
def _():
    s, b = req("POST", "/opportunities", {"name": f"Steal {RID}", "prospect_id": REC["ag1b"]["contact"]},
               U["ag1a"]["token"])
    R.check("SP-038", "Exec cannot create a deal on a peer's contact (400/404)", s in (400, 404), f"{s} {b}")


@guarded("SP-039", "Peer proposals are hidden from the proposal pipeline")
def _():
    items = must(req("GET", "/proposals", token=U["ag1a"]["token"]))["items"]
    mgr2 = must(req("GET", "/proposals", token=U["mgr2"]["token"]))["items"]
    R.check("SP-039", "Proposal pipeline only lists proposals on visible deals",
            REC["proposal_ag2a"] not in ids_of(items, "proposal_id") and REC["proposal_ag2a"] in ids_of(mgr2, "proposal_id"),
            f"ag1a sees={REC['proposal_ag2a'] in ids_of(items, 'proposal_id')}")


@guarded("SP-040", "Assignment is limited to self and team")
def _():
    s1 = req("PATCH", f"/leads/{REC['ag1a']['lead']}", {"owner_id": U["ag1b"]["user_id"]}, U["ag1a"]["token"])[0]
    s2 = req("PATCH", f"/leads/{REC['mgr1']['lead']}", {"owner_id": U["ag2a"]["user_id"]}, U["mgr1"]["token"])[0]
    s3 = req("PATCH", f"/opportunities/{REC['ag1a']['opp']}", {"owner_id": U["ag2a"]["user_id"]}, U["ag1a"]["token"])[0]
    s4 = req("PATCH", f"/leads/{REC['ag1a']['lead']}", {"owner_id": None}, U["ag1a"]["token"])[0]
    owner = sql(f"SELECT owner_id FROM leads WHERE lead_id='{REC['ag1a']['lead']}'")
    R.check("SP-040", "Exec can't assign to a peer, BD can't assign outside own team, owner can't be blanked",
            s1 == 403 and s2 == 403 and s3 == 403 and s4 == 400 and owner == U["ag1a"]["user_id"],
            f"exec->peer={s1} bd->other team={s2} opp exec->other={s3} blank={s4} owner={owner}")


@guarded("SP-041", "BD reassigns within team; visibility follows the new owner")
def _():
    c = contact(U["ag1b"], "Move", f"mv{RID}", company=f"Mv {RID}")
    l = lead(U["ag1b"], c["prospect_id"])
    s, b = req("PATCH", f"/leads/{l['lead_id']}", {"owner_id": U["ag1a"]["user_id"]}, U["mgr1"]["token"])
    a_sees = req("GET", f"/leads/{l['lead_id']}", token=U["ag1a"]["token"])[0]
    b_sees = req("GET", f"/leads/{l['lead_id']}", token=U["ag1b"]["token"])[0]
    hist = [h for h in (b.get("history") or []) if h["field"] == "owner_id"] if s == 200 else []
    R.check("SP-041", "BD reassigns a lead within the team; new owner sees it, old owner no longer does; owner change logged",
            s == 200 and a_sees == 200 and b_sees == 404 and hist and hist[0].get("changed_by_name"),
            f"patch={s} new={a_sees} old={b_sees} hist={hist[:1]}")


@guarded("SP-042", "Market Research hands contacts over for assignment")
def _():
    c = contact(U["mr1"], "Research", f"rs{RID}", company=f"Rs {RID}")
    before = contact_detail(U["ag1a"]["token"], c["prospect_id"])[0]
    s, b = req("PATCH", f"/contacts/{c['prospect_id']}", {"owner_id": U["ag1a"]["user_id"]}, U["mgr1"]["token"])
    after = contact_detail(U["ag1a"]["token"], c["prospect_id"])[0]
    mr_after = contact_detail(U["mr1"]["token"], c["prospect_id"])[0]
    l = req("POST", "/leads", {"prospect_id": c["prospect_id"], "source": "Market research"}, U["ag1a"]["token"])
    R.check("SP-042", "L4 contact is invisible to the exec until the BD hands it over; then the exec can work it",
            before == 404 and s == 200 and after == 200 and mr_after == 404 and l[0] == 201,
            f"before={before} handover={s} {str(b)[:120]} after={after} mr_after={mr_after} lead={l[0]}")


@guarded("SP-043", "Sales Head sees every team; BD only their own (pipeline totals)")
def _():
    rev = lambda k: must(req("GET", "/pipeline/revenue", token=U[k]["token"]))["totals"]  # noqa: E731
    expect = lambda ks: sum(o["amount"] or 0 for o in all_opps(A.token, status="OPEN")  # noqa: E731
                            if o["owner_id"] in {U[k]["user_id"] for k in ks})
    h, m1, m2, a = rev("head"), rev("mgr1"), rev("mgr2"), rev("ag1a")
    exp_h = sum(o["amount"] or 0 for o in all_opps(A.token, status="OPEN"))
    R.check("SP-043", "Revenue pipeline is scoped: Head = all teams, BD = own team, exec = own deals",
            abs(h["open_amount"] - exp_h) < 0.01 and abs(m1["open_amount"] - expect(VISIBLE["mgr1"])) < 0.01
            and abs(m2["open_amount"] - expect(VISIBLE["mgr2"])) < 0.01 and abs(a["open_amount"] - expect({"ag1a"})) < 0.01,
            f"head={h['open_amount']}/{exp_h} mgr1={m1['open_amount']} mgr2={m2['open_amount']} ag1a={a['open_amount']}")


@guarded("SP-044", "Contact export honours the hierarchy")
def _():
    s, body = req("GET", "/contacts/export?format=csv", token=U["mgr1"]["token"], raw=True)
    text = body.decode(errors="ignore") if isinstance(body, bytes) else str(body)
    has_team = REC["ag1a"]["last"].lower() in text.lower()
    has_other = REC["ag2a"]["last"].lower() in text.lower()
    R.check("SP-044", "BD's contact export contains own team and not other teams (NFR: every screen, API and export)",
            s == 200 and has_team and not has_other, f"HTTP {s} team={has_team} other={has_other} {text[:120]!r}")


@guarded("SP-045", "Cross-tenant isolation of sales records")
def _():
    tb = B.token
    reads = {
        "lead": req("GET", f"/leads/{REC['ag1a']['lead']}", token=tb)[0],
        "opp": req("GET", f"/opportunities/{REC['ag1a']['opp']}", token=tb)[0],
        "contact": req("GET", f"/contacts/{REC['ag1a']['contact']}", token=tb)[0],
        "patch opp": req("PATCH", f"/opportunities/{REC['ag1a']['opp']}", {"amount": 1}, tb)[0],
        "convert": req("POST", f"/leads/{REC['ag1a']['sql_lead']}/convert", {}, tb)[0],
        "proposal": req("PATCH", f"/proposals/{REC['proposal_ag2a']}", {"status": "SENT"}, tb)[0],
        "lead on A contact": req("POST", "/leads", {"prospect_id": REC["ag1a"]["contact"]}, tb)[0],
    }
    lists = (len(all_leads(tb)) + len(all_opps(tb)) + len(must(req("GET", "/leads/sql-queue", token=tb))["items"])
             + len(must(req("GET", "/proposals", token=tb))["items"]))
    rev = must(req("GET", "/pipeline/revenue", token=tb))["totals"]["open_amount"]
    wrong = {k: v for k, v in reads.items() if v != 404}
    R.check("SP-045", "Another tenant's admin cannot read, edit, convert or list tenant A's sales records",
            not wrong and lists == 0 and rev == 0, f"{wrong} listed={lists} rev={rev}")


@guarded("SP-046", "Cross-tenant owners and stages are rejected")
def _():
    sb = stage_by_name(B.token)
    s1 = req("PATCH", f"/leads/{REC['ag1a']['lead']}", {"owner_id": UB["agent"]["user_id"]}, A.token)[0]
    s2 = req("POST", "/opportunities", {"name": "x", "owner_id": UB["agent"]["user_id"]}, A.token)[0]
    s3 = req("PATCH", f"/opportunities/{REC['ag1a']['opp']}", {"stage_id": sb["Proposal"]["stage_id"]}, A.token)[0]
    R.check("SP-046", "Assigning to another tenant's user or stage is rejected (400)",
            s1 == 400 and s2 == 400 and s3 == 400, f"lead owner={s1} opp owner={s2} stage={s3}")


@guarded("SP-047", "Users outside the hierarchy keep the role rule")
def _():
    # An agent with no sales level sees only their own records; an admin sees everything
    loner = A.add_user("AGENT", f"Loner{RID}")
    c = contact(loner, "Lone", f"ln{RID}")
    l = lead(loner, c["prospect_id"])
    lone_sees = {x["lead_id"] for x in all_leads(loner["token"])}
    admin_sees = req("GET", f"/leads/{l['lead_id']}", token=A.token)[0]
    mgr1_sees = req("GET", f"/leads/{l['lead_id']}", token=U["mgr1"]["token"])[0]
    R.check("SP-047", "Agent outside the hierarchy sees only own leads; admin sees all; BD cannot see it",
            lone_sees == {l["lead_id"]} and admin_sees == 200 and mgr1_sees == 404,
            f"lone={len(lone_sees)} admin={admin_sees} mgr1={mgr1_sees}")


@guarded("SP-048", "Amount fields hidden from configured roles/levels")
def _():
    must(req("PUT", "/sales/settings", {"amount_hidden_roles": ["AGENT"], "amount_hidden_levels": [4]}, A.token))
    try:
        o = must(req("GET", f"/opportunities/{REC['ag1a']['opp']}", token=U["ag1a"]["token"]))
        lst = must(req("GET", "/opportunities", token=U["ag1a"]["token"]))
        rev = must(req("GET", "/pipeline/revenue", token=U["ag1a"]["token"]))["totals"]
        board = must(req("GET", "/opportunities/board", token=U["ag1a"]["token"]))
        prop = must(req("GET", "/proposals", token=U["ag2a"]["token"]))
        write = req("PATCH", f"/opportunities/{REC['ag1a']['opp']}", {"amount": 1}, U["ag1a"]["token"])[0]
        meta_mr = must(req("GET", "/leads/meta", token=U["mr1"]["token"]))["can_see_amounts"]
        mgr = must(req("GET", f"/opportunities/{REC['ag1a']['opp']}", token=U["mgr1"]["token"]))
        hidden = (o["amount"] is None and o["weighted_amount"] is None and lst["total_amount"] is None
                  and all(i["amount"] is None for i in lst["items"]) and rev["open_amount"] is None
                  and rev["weighted_amount"] is None and all(c["amount"] is None for c in board["columns"])
                  and prop["total_amount"] is None)
        R.check("SP-048", "With amounts hidden for AGENT/L4: agent sees no amounts anywhere, cannot write one; manager still sees",
                hidden and write == 403 and meta_mr is False and mgr["amount"] == 10000.0,
                f"hidden={hidden} write={write} mr1_can_see={meta_mr} mgr_amount={mgr.get('amount')}")
        # sorting by a hidden amount must not reveal the order
        s, b = req("GET", "/opportunities?sort_by=amount&sort_order=desc", token=U["ag1a"]["token"])
        R.check("SP-049", "Hidden amounts: list still works when sorted by amount and stays masked",
                s == 200 and all(i["amount"] is None for i in b["items"]), f"{s}")
    finally:
        must(req("PUT", "/sales/settings", {"amount_hidden_roles": [], "amount_hidden_levels": []}, A.token))


# ═══════════════════════ 5.6 Leads & SQL ═══════════════════════

L = {}


@guarded("SP-060", "Lead record with source, status/stage and owner, linked to contact")
def _():
    c = contact(U["ag1a"], "Lena", f"lead{RID}", company=f"Leadco {RID}")
    L["c"] = c
    l = lead(U["ag1a"], c["prospect_id"], source="Trade show")
    L["l"] = l
    got = must(req("GET", f"/leads/{l['lead_id']}", token=U["ag1a"]["token"]))
    R.check("SP-060", "Create lead: source, stage NEW, owner = contact owner, linked contact & company shown",
            got["source"] == "Trade show" and got["stage"] == "NEW" and got["owner_id"] == U["ag1a"]["user_id"]
            and got["prospect_id"] == c["prospect_id"] and got["contact_email"] == c["email"]
            and got["company_name"] == f"Leadco {RID}" and got["account_id"],
            json.dumps({k: got.get(k) for k in ("source", "stage", "owner_id", "company_name", "account_id")}))


@guarded("SP-061", "Lead creation negatives")
def _():
    t = U["ag1a"]["token"]
    dup = req("POST", "/leads", {"prospect_id": L["c"]["prospect_id"]}, t)
    nf = req("POST", "/leads", {"prospect_id": str(uuid.uuid4())}, t)[0]
    c2 = contact(U["ag1a"], "Neg", f"neg{RID}")
    bad_stage = req("POST", "/leads", {"prospect_id": c2["prospect_id"], "stage": "BOGUS"}, t)[0]
    left = sql(f"SELECT COUNT(*) FROM leads WHERE prospect_id='{c2['prospect_id']}'")
    R.check("SP-061", "Duplicate open lead → 409 (with existing id); unknown contact → 404; bad stage → 400 and nothing saved",
            dup[0] == 409 and isinstance(dup[1].get("detail"), dict) and dup[1]["detail"].get("lead_id") == L["l"]["lead_id"]
            and nf == 404 and bad_stage == 400 and left == "0",
            f"dup={dup[0]} nf={nf} bad_stage={bad_stage} saved_rows={left}")


@guarded("SP-062", "Creating a lead directly as SQL cannot bypass BANT")
def _():
    c = contact(U["ag1a"], "Skip", f"skip{RID}")
    s, b = req("POST", "/leads", {"prospect_id": c["prospect_id"], "stage": "SQL"}, U["ag1a"]["token"])
    R.check("SP-062", "POST /leads with stage=SQL and no BANT does not create an SQL",
            (s == 400) or (s == 201 and b["stage"] != "SQL"), f"{s} stage={b.get('stage') if isinstance(b, dict) else b}")


@guarded("SP-063", "Lead stages advance and move the contact lifecycle forward")
def _():
    t, lid, pid = U["ag1a"]["token"], L["l"]["lead_id"], L["c"]["prospect_id"]
    lc = lambda: must(contact_detail(t, pid))["lifecycle_stage"]  # noqa: E731
    seq = []
    seq.append(("NEW", lc()))
    must(req("PATCH", f"/leads/{lid}", {"stage": "CONTACTED"}, t))
    seq.append(("CONTACTED", lc()))
    must(req("PATCH", f"/leads/{lid}", {"stage": "ENGAGED"}, t))
    seq.append(("ENGAGED", lc()))
    must(req("PATCH", f"/leads/{lid}", {"stage": "CONTACTED"}, t))
    seq.append(("back to CONTACTED", lc()))
    must(req("PATCH", f"/leads/{lid}", {"stage": "ENGAGED"}, t))
    R.check("SP-063", "Lead stage ↔ lifecycle: NEW/CONTACTED=LEAD, ENGAGED=MQL, moving back never downgrades the contact",
            [x[1] for x in seq] == ["LEAD", "LEAD", "MQL", "MQL"], f"{seq}")


@guarded("SP-064", "Invalid stage transitions are rejected")
def _():
    t, lid = U["ag1a"]["token"], L["l"]["lead_id"]
    a = req("PATCH", f"/leads/{lid}", {"stage": "CONVERTED"}, t)[0]
    b = req("PATCH", f"/leads/{lid}", {"stage": "WHATEVER"}, t)[0]
    c = req("PATCH", f"/leads/{lid}", {"stage": "DISQUALIFIED"}, t)[0]
    st = sql(f"SELECT stage FROM leads WHERE lead_id='{lid}'")
    R.check("SP-064", "Set CONVERTED directly / unknown stage / disqualify without reason → 400; stage unchanged",
            a == 400 and b == 400 and c == 400 and st == "ENGAGED", f"{a} {b} {c} stage={st}")


@guarded("SP-065", "SQL qualification criteria (BANT) enforced")
def _():
    t, lid = U["ag1a"]["token"], L["l"]["lead_id"]
    must(req("PATCH", f"/leads/{lid}", {"qualification": {"budget": True, "need": True}}, t))
    s1, b1 = req("PATCH", f"/leads/{lid}", {"stage": "SQL"}, t)
    missing = b1.get("detail", "") if isinstance(b1, dict) else ""
    must(req("PATCH", f"/leads/{lid}", {"qualification": {"authority": True, "timeline": True, "notes": "BANT ok"}}, t))
    s2, b2 = req("PATCH", f"/leads/{lid}", {"stage": "SQL", "next_step": "Send pricing",
                                             "next_step_at": (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"}, t)
    lc = must(contact_detail(t, L["c"]["prospect_id"]))["lifecycle_stage"]
    R.check("SP-065", "SQL needs all four BANT criteria; missing ones are named; once met: SQL, qualified_at set, contact → SQL",
            s1 == 400 and "decision maker" in missing and "timeline" in missing and "Budget" not in missing
            and s2 == 200 and b2["stage"] == "SQL" and b2["qualified_at"] and b2["missing_criteria"] == [] and lc == "SQL",
            f"partial={s1} {missing!r} full={s2} lc={lc}")


@guarded("SP-066", "SQL queue lists SQLs with age, owner and next step")
def _():
    qd = must(req("GET", "/leads/sql-queue", token=U["mgr1"]["token"]))
    ids = ids_of(qd["items"], "lead_id")
    mine = next((i for i in qd["items"] if i["lead_id"] == L["l"]["lead_id"]), {})
    only_sql = all(i["stage"] == "SQL" for i in qd["items"])
    team = {i["owner_id"] for i in qd["items"]} <= {U[k]["user_id"] for k in VISIBLE["mgr1"]}
    R.check("SP-066", "SQL queue (BD view): only SQL leads of the team, with owner, age, next step & overdue flag, by-owner summary",
            L["l"]["lead_id"] in ids and REC["ag1a"]["sql_lead"] in ids and REC["ag2a"]["sql_lead"] not in ids
            and only_sql and team and mine.get("owner_name") and mine.get("sql_age_days") == 0
            and mine.get("next_step") == "Send pricing" and mine.get("next_step_overdue") is True
            and qd["overdue_next_step"] >= 1 and any(o["owner_id"] == U["ag1a"]["user_id"] for o in qd["by_owner"]),
            f"in={L['l']['lead_id'] in ids} other_team={REC['ag2a']['sql_lead'] in ids} only_sql={only_sql} team={team} mine={ {k: mine.get(k) for k in ('owner_name', 'sql_age_days', 'next_step', 'next_step_overdue')} }")


@guarded("SP-067", "SQL queue age and owner filter")
def _():
    # Age the queue lead by 20 days in the DB, then the queue must report it as > 14 days, oldest first
    sql(f"UPDATE leads SET qualified_at = UTC_TIMESTAMP() - INTERVAL 20 DAY, created_at = UTC_TIMESTAMP() - INTERVAL 25 DAY "
        f"WHERE lead_id='{REC['ag1a']['sql_lead']}'")
    qd = must(req("GET", f"/leads/sql-queue?owner={U['ag1a']['user_id']}", token=U["mgr1"]["token"]))
    first = qd["items"][0] if qd["items"] else {}
    R.check("SP-067", "SQL queue tracks age (oldest first, >14-day count) and filters by owner",
            first.get("lead_id") == REC["ag1a"]["sql_lead"] and first.get("sql_age_days") == 20
            and first.get("age_days") == 25 and qd["over_14_days"] >= 1
            and all(i["owner_id"] == U["ag1a"]["user_id"] for i in qd["items"]),
            f"first={first.get('lead_id')} age={first.get('sql_age_days')} over14={qd['over_14_days']}")


@guarded("SP-068", "Leads pipeline board by stage")
def _():
    b = must(req("GET", "/leads/board", token=U["ag1a"]["token"]))
    cols = [c["stage"] for c in b["columns"]]
    count_ok = all(c["count"] == len(c["items"]) for c in b["columns"])
    placement = {i["lead_id"]: c["stage"] for c in b["columns"] for i in c["items"]}
    every = all_leads(U["ag1a"]["token"], open_only="true")
    one_col = all(placement.get(l["lead_id"]) == l["stage"] for l in every)
    full = must(req("GET", "/leads/board?include_closed=true", token=U["ag1a"]["token"]))
    R.check("SP-068", "Lead board: one column per open stage, each open lead in exactly its stage column; closed stages on request",
            cols == ["NEW", "CONTACTED", "ENGAGED", "SQL"] and count_ok and one_col
            and [c["stage"] for c in full["columns"]][-2:] == ["CONVERTED", "DISQUALIFIED"],
            f"cols={cols} counts_ok={count_ok} placement_ok={one_col}")


@guarded("SP-069", "Every lead has exactly one stage and one owner")
def _():
    bad = sql(f"SELECT COUNT(*) FROM leads WHERE tenant_id='{A.tenant_id}' AND (owner_id IS NULL OR stage IS NULL OR stage='')")
    total = sql(f"SELECT COUNT(*) FROM leads WHERE tenant_id='{A.tenant_id}'")
    R.check("SP-069", "Invariant: no lead in the workspace without a stage or an owner (DB check)",
            bad == "0" and int(total) > 5, f"bad={bad} total={total}")


@guarded("SP-070", "Convert SQL → opportunity without re-typing contact/company")
def _():
    t, lid = U["ag1a"]["token"], L["l"]["lead_id"]
    s, o = req("POST", f"/leads/{lid}/convert", {"amount": 12500, "close_date": str(date.today() + timedelta(days=45))}, t)
    L["opp"] = o.get("opportunity_id") if isinstance(o, dict) else None
    lead_now = must(req("GET", f"/leads/{lid}", token=t))
    lc = must(contact_detail(t, L["c"]["prospect_id"]))["lifecycle_stage"]
    stages = REC["stages"]
    R.check("SP-070", "Convert: deal linked to the lead's contact, company and owner, default name from company, first open stage, NEW client",
            s == 201 and o["prospect_id"] == L["c"]["prospect_id"] and o["account_id"] == lead_now["account_id"]
            and o["company_name"] == f"Leadco {RID}" and o["owner_id"] == U["ag1a"]["user_id"]
            and o["name"].startswith(f"Leadco {RID}") and o["stage_id"] == stages["Qualification"]["stage_id"]
            and o["client_type"] == "NEW" and o["amount"] == 12500.0 and o["lead_id"] == lid,
            f"{s} {str(o)[:300]}")
    R.check("SP-071", "Convert (integration): lead → CONVERTED with opportunity link, contact lifecycle → OPPORTUNITY, history on lead and deal",
            lead_now["stage"] == "CONVERTED" and lead_now["opportunity_id"] == L["opp"] and lead_now["converted_at"]
            and lc == "OPPORTUNITY"
            and any(h["field"] == "stage" and h["new_value"] == "CONVERTED" for h in lead_now["history"])
            and sql(f"SELECT COUNT(*) FROM property_changes WHERE object_type='DEAL' AND object_id='{L['opp']}'") != "0",
            f"lead={lead_now['stage']} opp_link={lead_now['opportunity_id'] == L['opp']} lc={lc}")


@guarded("SP-072", "Conversion negatives")
def _():
    t = U["ag1a"]["token"]
    again = req("POST", f"/leads/{L['l']['lead_id']}/convert", {}, t)[0]
    c = contact(U["ag1a"], "Notsql", f"ns{RID}")
    l = lead(U["ag1a"], c["prospect_id"])
    not_sql = req("POST", f"/leads/{l['lead_id']}/convert", {}, t)[0]
    reopen = req("PATCH", f"/leads/{L['l']['lead_id']}", {"stage": "ENGAGED"}, t)[0]
    delete = req("DELETE", f"/leads/{L['l']['lead_id']}", token=t)[0]
    n_opps = sql(f"SELECT COUNT(*) FROM opportunities WHERE lead_id='{L['l']['lead_id']}'")
    R.check("SP-072", "Converting twice / a non-SQL lead → 400; converted lead can't change stage or be deleted; one deal only",
            again == 400 and not_sql == 400 and reopen == 400 and delete == 400 and n_opps == "1",
            f"again={again} not_sql={not_sql} reopen={reopen} delete={delete} opps={n_opps}")


@guarded("SP-073", "Convert with overrides; owner override limited to team")
def _():
    t = U["mgr1"]["token"]
    c = contact(U["ag1b"], "Ovr", f"ovr{RID}", company=f"Ovr {RID}")
    l = lead(U["ag1b"], c["prospect_id"])
    to_sql(U["ag1b"], l["lead_id"])
    bad = req("POST", f"/leads/{l['lead_id']}/convert", {"owner_id": U["ag2a"]["user_id"]}, t)[0]
    s, o = req("POST", f"/leads/{l['lead_id']}/convert", {"name": f"Custom {RID}", "owner_id": U["ag1a"]["user_id"],
                                                          "stage_id": REC["stages"]["Proposal"]["stage_id"],
                                                          "client_type": "EXISTING"}, t)
    R.check("SP-073", "BD converts with name/owner/stage/client-type overrides; owner outside team → 403",
            bad == 403 and s == 201 and o["name"] == f"Custom {RID}" and o["owner_id"] == U["ag1a"]["user_id"]
            and o["stage_name"] == "Proposal" and o["probability"] == 50 and o["client_type"] == "EXISTING",
            f"bad={bad} ok={s} {str(o)[:200]}")


@guarded("SP-074", "Contact without company record: company resolved at conversion")
def _():
    t = U["ag1a"]["token"]
    c = contact(U["ag1a"], "Nocomp", f"nc{RID}", company=f"Freshco {RID}")
    # detach the company to simulate an imported contact that only carries a company name
    sql(f"UPDATE prospects SET account_id=NULL WHERE prospect_id='{c['prospect_id']}'")
    l = lead(U["ag1a"], c["prospect_id"])
    sql(f"UPDATE leads SET account_id=NULL WHERE lead_id='{l['lead_id']}'")
    to_sql(U["ag1a"], l["lead_id"])
    s, o = req("POST", f"/leads/{l['lead_id']}/convert", {}, t)
    R.check("SP-074", "Converting a lead whose contact has only a company name links the deal to a company record",
            s == 201 and o.get("account_id") and (o.get("company_name") or "").lower() == f"freshco {RID}".lower(),
            f"{s} account={o.get('account_id') if isinstance(o, dict) else o} company={o.get('company_name') if isinstance(o, dict) else ''}")


@guarded("SP-075", "Disqualify with reason and recycle date")
def _():
    t = U["ag1a"]["token"]
    c = contact(U["ag1a"], "Dq", f"dq{RID}")
    l = lead(U["ag1a"], c["prospect_id"])
    L["dq"] = l["lead_id"]
    s, b = req("PATCH", f"/leads/{l['lead_id']}", {"stage": "DISQUALIFIED", "disqualified_reason": "No budget",
                                                   "recycle_in_days": 30}, t)
    days = (datetime.fromisoformat(b["recycle_at"]) - datetime.utcnow()).days if s == 200 and b.get("recycle_at") else None
    R.check("SP-075", "Disqualify needs a reason (BR-SF-02); reason and recycle date (30 days) stored",
            s == 200 and b["stage"] == "DISQUALIFIED" and b["disqualified_reason"] == "No budget" and days in (29, 30),
            f"{s} days={days}")


@guarded("SP-076", "One-click lead from a positive campaign reply")
def _():
    t = U["ag1a"]["token"]
    c = contact(U["ag1a"], "Reply", f"rp{RID}", company=f"Replyco {RID}")
    cid, mid = str(uuid.uuid4()), str(uuid.uuid4())
    sql(f"INSERT INTO campaigns (campaign_id, tenant_id, campaign_name, created_by, status, auto_paused, created_at) "
        f"VALUES ('{cid}','{A.tenant_id}','Reply camp {RID}','{U['ag1a']['user_id']}','DRAFT',0,UTC_TIMESTAMP())")
    sql(f"INSERT INTO email_messages (message_id, prospect_id, to_email, campaign_id, direction, status, subject, body_text, sent_at) "
        f"VALUES ('{mid}','{c['prospect_id']}','me@sp.example','{cid}','INBOUND','RECEIVED','Re: hello',"
        f"'Yes, interested - call me next week', UTC_TIMESTAMP())")
    peer = req("POST", "/leads/from-message", {"message_id": mid}, U["ag1b"]["token"])[0]
    s, b = req("POST", "/leads/from-message", {"message_id": mid}, t)
    again = req("POST", "/leads/from-message", {"message_id": mid}, t)[0]
    L["reply_lead"], L["reply_contact"] = (b.get("lead_id") if isinstance(b, dict) else None), c["prospect_id"]
    R.check("SP-076", "Reply → lead in one click: ENGAGED, source 'Campaign reply', need ticked, campaign linked; peer 404; repeat 409",
            s == 201 and b["stage"] == "ENGAGED" and b["source"] == "Campaign reply" and b["qualification"].get("need") is True
            and b["campaign_id"] == cid and b["next_step"] and peer == 404 and again == 409,
            f"{s} {str(b)[:200]} peer={peer} again={again}")


@guarded("SP-077", "Round-robin assignment of unowned leads")
def _():
    pool = [U["ag1a"]["user_id"], U["ag1b"]["user_id"]]
    must(req("PUT", "/sales/settings", {"lead_assignment": {"mode": "round_robin", "users": pool, "next_index": 0}}, A.token))
    try:
        owners = []
        for i in range(3):
            c = contact(U["owner"], "Rr", f"rr{i}{RID}")
            sql(f"UPDATE prospects SET owner_id=NULL WHERE prospect_id='{c['prospect_id']}'")
            l = lead(U["owner"], c["prospect_id"])
            owners.append((l["owner_id"], sql(f"SELECT owner_id FROM prospects WHERE prospect_id='{c['prospect_id']}'")))
        R.check("SP-077", "Unowned leads are assigned round robin (BR-SF-03) and the contact follows its lead's owner",
                [o[0] for o in owners] == [pool[0], pool[1], pool[0]] and all(a == b for a, b in owners),
                f"{owners}")
    finally:
        must(req("PUT", "/sales/settings", {"lead_assignment": {"mode": "off", "users": []}}, A.token))


# ═══════════════════════ 5.7 Sales pipeline ═══════════════════════

P = {}


@guarded("SP-090", "Default sales stages with probability")
def _():
    st = must(req("GET", "/sales/stages", token=U["ag1a"]["token"]))
    got = [(s["name"], s["probability"], s["status"]) for s in st]
    R.check("SP-090", "Sales stages are classified (open/won/lost) with a probability each, in order",
            got[:6] == [("Qualification", 10, "OPEN"), ("Needs analysis", 25, "OPEN"), ("Proposal", 50, "OPEN"),
                        ("Negotiation", 75, "OPEN"), ("Closed won", 100, "WON"), ("Closed lost", 0, "LOST")],
            f"{got}")


@guarded("SP-091", "Admin manages stages; validation")
def _():
    s, st = req("POST", "/sales/stages", {"name": f"Demo {RID}", "probability": 40, "sort_order": 2}, A.token)
    P["demo_stage"] = st.get("stage_id") if isinstance(st, dict) else None
    neg = {
        "prob 150": req("POST", "/sales/stages", {"name": f"Bad {RID}", "probability": 150}, A.token)[0],
        "prob -1": req("POST", "/sales/stages", {"name": f"Bad2 {RID}", "probability": -1}, A.token)[0],
        "won+lost": req("POST", "/sales/stages", {"name": f"Bad3 {RID}", "is_won": True, "is_lost": True}, A.token)[0],
        "duplicate": req("POST", "/sales/stages", {"name": f"demo {RID}"}, A.token)[0],
        "no name": req("POST", "/sales/stages", {"probability": 5}, A.token)[0],
    }
    agent = req("POST", "/sales/stages", {"name": f"Agent {RID}"}, U["ag1a"]["token"])[0]
    mgr = req("PATCH", f"/sales/stages/{P['demo_stage']}", {"probability": 99}, U["head"]["token"])[0]
    upd = req("PATCH", f"/sales/stages/{P['demo_stage']}", {"probability": 45}, A.token)
    R.check("SP-091", "Admin adds/re-weights a stage; prob outside 0-100, won+lost, duplicate, blank → 4xx; non-admins 403",
            s == 201 and st["probability"] == 40 and neg == {"prob 150": 400, "prob -1": 400, "won+lost": 400,
                                                              "duplicate": 409, "no name": 400}
            and agent == 403 and mgr == 403 and upd[0] == 200 and upd[1]["probability"] == 45,
            f"create={s} neg={neg} agent={agent} head={mgr} upd={upd[0]}")


@guarded("SP-092", "Deal probability and weighted amount follow the stage")
def _():
    t = U["ag1a"]["token"]
    c = contact(U["ag1a"], "Pipe", f"pp{RID}", company=f"Pipeco {RID}")
    P["c"] = c
    o = opp(U["ag1a"], f"Pipe deal {RID}", c["prospect_id"], "20,000", close_date=str(date.today() + timedelta(days=20)))
    P["o"] = o["opportunity_id"]
    s, moved = req("PATCH", f"/opportunities/{P['o']}", {"stage_id": REC["stages"]["Proposal"]["stage_id"]}, t)
    R.check("SP-092", "New deal: first stage 10% → weighted 2,000; move to Proposal: 50% → weighted 10,000; '20,000' parsed",
            o["amount"] == 20000.0 and o["probability"] == 10 and o["weighted_amount"] == 2000.0 and o["status"] == "OPEN"
            and s == 200 and moved["probability"] == 50 and moved["weighted_amount"] == 10000.0,
            f"{o.get('amount')} {o.get('probability')} {o.get('weighted_amount')} -> {s} {moved.get('weighted_amount') if isinstance(moved, dict) else moved}")


@guarded("SP-093", "Stage, amount and close-date changes are logged")
def _():
    t = U["ag1a"]["token"]
    new_close = str(date.today() + timedelta(days=60))
    must(req("PATCH", f"/opportunities/{P['o']}", {"amount": 25000, "close_date": new_close}, t))
    d = must(req("GET", f"/opportunities/{P['o']}", token=t))
    h = {x["field"]: x for x in d["history"]}
    st = h.get("stage_id", {})
    R.check("SP-093", "History has stage (by name), amount and close-date changes with user and time; stage_history lists stays",
            st.get("old_value") == "Qualification" and st.get("new_value") == "Proposal"
            and h.get("amount", {}).get("new_value") in ("25000.00", "25000", "25000.0")
            and h.get("close_date", {}).get("new_value") == new_close
            and all(x.get("changed_by_name") and x.get("changed_at") for x in (st, h.get("amount", {}), h.get("close_date", {})))
            and [s["stage"] for s in d["stage_history"]] == ["Qualification", "Proposal"],
            f"stage={st} amount={h.get('amount')} close={h.get('close_date')} sh={d.get('stage_history')}")


@guarded("SP-094", "Amount and close-date validation")
def _():
    t = U["ag1a"]["token"]
    r = {
        "negative": req("PATCH", f"/opportunities/{P['o']}", {"amount": -5}, t)[0],
        "text": req("PATCH", f"/opportunities/{P['o']}", {"amount": "lots"}, t)[0],
        "bad date": req("PATCH", f"/opportunities/{P['o']}", {"close_date": "2026-13-45"}, t)[0],
        "bad client type": req("PATCH", f"/opportunities/{P['o']}", {"client_type": "VIP"}, t)[0],
        "unknown stage": req("PATCH", f"/opportunities/{P['o']}", {"stage_id": str(uuid.uuid4())}, t)[0],
        "no name": req("POST", "/opportunities", {"name": "  "}, t)[0],
    }
    amt = sql(f"SELECT amount FROM opportunities WHERE opportunity_id='{P['o']}'")
    R.check("SP-094", "Negative/non-numeric amount, bad date, bad client type, unknown stage, blank name → 4xx; amount unchanged",
            r == {"negative": 400, "text": 400, "bad date": 422, "bad client type": 400, "unknown stage": 400, "no name": 400}
            and amt == "25000.00", f"{r} amount={amt}")


@guarded("SP-095", "Past close date flags the deal overdue")
def _():
    t = U["ag1a"]["token"]
    o = opp(U["ag1a"], f"Overdue {RID}", P["c"]["prospect_id"], 1000, close_date=str(date.today() - timedelta(days=2)))
    P["overdue"] = o["opportunity_id"]
    stale = ids_of(all_opps(t, stale="true", status="OPEN"), "opportunity_id")
    R.check("SP-095", "Close date in the past → overdue + stale flags and listed in the stale filter",
            o["overdue"] is True and o["stale"] is True and P["overdue"] in stale and P["o"] not in stale,
            f"overdue={o['overdue']} stale={o['stale']} in_filter={P['overdue'] in stale}")


@guarded("SP-096", "Won requires a reason; won deal closes and contact becomes customer")
def _():
    t = U["ag1a"]["token"]
    won = REC["stages"]["Closed won"]["stage_id"]
    s0, b0 = req("PATCH", f"/opportunities/{P['o']}", {"stage_id": won}, t)
    st0 = sql(f"SELECT status FROM opportunities WHERE opportunity_id='{P['o']}'")
    s1, b1 = req("PATCH", f"/opportunities/{P['o']}", {"stage_id": won, "closed_reason": "Best product fit"}, t)
    lc = must(contact_detail(t, P["c"]["prospect_id"]))["lifecycle_stage"]
    R.check("SP-096", "Closed won without a reason → 400 (still OPEN); with reason → WON, closed_at, forecast CLOSED, contact CUSTOMER",
            s0 == 400 and st0 == "OPEN" and s1 == 200 and b1["status"] == "WON" and b1["closed_at"]
            and b1["closed_reason"] == "Best product fit" and b1["forecast_category"] == "CLOSED" and lc == "CUSTOMER",
            f"noreason={s0} status_then={st0} withreason={s1} {b1.get('status') if isinstance(b1, dict) else b1} lc={lc}")


@guarded("SP-097", "Lost requires a reason; lost deal leaves the open pipeline")
def _():
    t = U["ag1a"]["token"]
    lost = REC["stages"]["Closed lost"]["stage_id"]
    o = opp(U["ag1a"], f"Lose {RID}", P["c"]["prospect_id"], 3000)
    s0 = req("PATCH", f"/opportunities/{o['opportunity_id']}", {"stage_id": lost}, t)[0]
    s1, b1 = req("PATCH", f"/opportunities/{o['opportunity_id']}", {"stage_id": lost, "closed_reason": "Lost to competitor"}, t)
    open_ids = ids_of(all_opps(t, status="OPEN"), "opportunity_id")
    R.check("SP-097", "Closed lost without a reason → 400; with reason → LOST, forecast OMITTED, not in open list",
            s0 == 400 and s1 == 200 and b1["status"] == "LOST" and b1["forecast_category"] == "OMITTED"
            and b1["closed_reason"] == "Lost to competitor" and o["opportunity_id"] not in open_ids,
            f"noreason={s0} withreason={s1}")


@guarded("SP-098", "Client type: new vs existing")
def _():
    t = U["ag1a"]["token"]
    acct = sql(f"SELECT account_id FROM opportunities WHERE opportunity_id='{P['o']}'")
    o2 = opp(U["ag1a"], f"Upsell {RID}", P["c"]["prospect_id"], 8000)
    other = contact(U["ag1a"], "Newbie", f"nb{RID}", company=f"Brandnew {RID}")
    o3 = opp(U["ag1a"], f"Newlogo {RID}", other["prospect_id"], 4000)
    P["upsell"], P["newlogo"] = o2["opportunity_id"], o3["opportunity_id"]
    R.check("SP-098", "Deal for a company that already won defaults to EXISTING; brand-new company defaults to NEW",
            o2["account_id"] == acct and o2["client_type"] == "EXISTING" and o3["client_type"] == "NEW",
            f"upsell={o2['client_type']} new={o3['client_type']}")


@guarded("SP-099", "Proposal pipeline: draft → sent → under review → accepted / rejected")
def _():
    t = U["ag1a"]["token"]
    s, p = req("POST", "/proposals", {"opportunity_id": P["upsell"], "title": f"Upsell proposal {RID}"}, t)
    P["prop"] = p["proposal_id"]
    trail = [(p["status"], p["amount"], bool(p["sent_at"]))]
    for st in ("SENT", "UNDER_REVIEW", "ACCEPTED"):
        b = must(req("PATCH", f"/proposals/{P['prop']}", {"status": st}, t))
        trail.append((b["status"], bool(b["sent_at"]), bool(b["decided_at"])))
    p2 = must(req("POST", "/proposals", {"opportunity_id": P["newlogo"], "status": "SENT", "amount": 3900}, t), 201)
    rej = must(req("PATCH", f"/proposals/{p2['proposal_id']}", {"status": "REJECTED"}, t))
    bad = req("PATCH", f"/proposals/{P['prop']}", {"status": "WON"}, t)[0]
    noopp = req("POST", "/proposals", {"title": "orphan"}, t)[0]
    hist = must(req("GET", f"/opportunities/{P['upsell']}", token=t))["history"]
    R.check("SP-099", "Proposal statuses draft/sent/under review/accepted/rejected with sent & decided dates; amount defaults to deal; bad status 400",
            s == 201 and trail == [("DRAFT", 8000.0, False), ("SENT", True, False), ("UNDER_REVIEW", True, False),
                                   ("ACCEPTED", True, True)]
            and rej["status"] == "REJECTED" and rej["decided_at"] and bad == 400 and noopp == 400
            and any(h["field"] == "proposal" for h in hist), f"{trail} rej={rej.get('status')} bad={bad} noopp={noopp}")


@guarded("SP-100", "Proposal pipeline columns and client-type filter")
def _():
    t = U["ag1a"]["token"]
    allp = must(req("GET", "/proposals", token=t))
    cols = {c["status"]: c for c in allp["columns"]}
    ex = must(req("GET", "/proposals?client_type=EXISTING", token=t))
    nw = must(req("GET", "/proposals?client_type=NEW", token=t))
    R.check("SP-100", "Proposal pipeline has a column per status with counts/amounts and filters by new vs existing client",
            list(cols) == ["DRAFT", "SENT", "UNDER_REVIEW", "ACCEPTED", "REJECTED"]
            and all(c["count"] == len(c["items"]) for c in cols.values())
            and all(i["client_type"] == "EXISTING" for i in ex["items"]) and P["prop"] in ids_of(ex["items"], "proposal_id")
            and all(i["client_type"] == "NEW" for i in nw["items"]) and P["prop"] not in ids_of(nw["items"], "proposal_id"),
            f"cols={list(cols)} ex={len(ex['items'])} new={len(nw['items'])}")


@guarded("SP-101", "Quote PDF for a proposal")
def _():
    s, body = req("GET", f"/proposals/{P['prop']}/pdf", token=U["ag1a"]["token"], raw=True)
    peer = req("GET", f"/proposals/{P['prop']}/pdf", token=U["ag1b"]["token"], raw=True)[0]
    R.check("SP-101", "Proposal PDF downloads for the owner (BR-SF-15) and is hidden from a peer",
            s == 200 and isinstance(body, bytes) and body[:4] == b"%PDF" and peer == 404, f"{s} {body[:8]!r} peer={peer}")


def pipeline_matches(case_id, title, token, **flt):
    @guarded(case_id, title)
    def _():
        qs = "&".join(f"{k}={v}" for k, v in flt.items())
        rev = must(req("GET", f"/pipeline/revenue?{qs}", token=token))
        opps = all_opps(token, status="OPEN", **flt)
        exp_amt = round(sum(o["amount"] or 0 for o in opps), 2)
        exp_w = round(sum((o["amount"] or 0) * o["probability"] / 100 for o in opps), 2)
        lst = must(req("GET", f"/opportunities?status=OPEN&page_size=500&{qs}", token=token))
        tot = rev["totals"]
        stage_sum = round(sum(s["amount"] for s in rev["stages"] if s["status"] == "OPEN"), 2)
        R.check(case_id, title,
                abs(tot["open_amount"] - exp_amt) < 0.01 and abs(tot["weighted_amount"] - exp_w) < 0.02
                and tot["open_count"] == len(opps) and abs(lst["total_amount"] - exp_amt) < 0.01
                and abs(stage_sum - exp_amt) < 0.01,
                f"rev open={tot['open_amount']} w={tot['weighted_amount']} n={tot['open_count']} | "
                f"sum open={exp_amt} w={exp_w} n={len(opps)} list_total={lst['total_amount']} stage_sum={stage_sum}")


pipeline_matches("SP-102", "Revenue pipeline totals (unweighted & weighted) = sum of open deals, all deals (admin)", A.token)
pipeline_matches("SP-103", "Revenue pipeline totals = open deals, filtered to client type NEW", A.token, client_type="NEW")
pipeline_matches("SP-104", "Revenue pipeline totals = open deals, filtered to client type EXISTING", A.token, client_type="EXISTING")
pipeline_matches("SP-105", "Revenue pipeline totals = open deals, filtered by owner (BD view of one exec)", U["mgr1"]["token"],
                 owner=U["ag1a"]["user_id"])
pipeline_matches("SP-106", "Revenue pipeline totals = open deals, filtered by close-date window",
                 A.token, close_from=str(date.today()), close_to=str(date.today() + timedelta(days=40)))


@guarded("SP-107", "Won and lost totals in the revenue pipeline")
def _():
    rev = must(req("GET", f"/pipeline/revenue?owner={U['ag1a']['user_id']}", token=A.token))["totals"]
    won = [o for o in all_opps(A.token, status="WON", owner=U["ag1a"]["user_id"])]
    lost = [o for o in all_opps(A.token, status="LOST", owner=U["ag1a"]["user_id"])]
    bct = rev["by_client_type"]
    R.check("SP-107", "Pipeline reports won/lost counts and amounts and splits open amount by NEW/EXISTING",
            rev["won_count"] == len(won) and abs(rev["won_amount"] - sum(o["amount"] or 0 for o in won)) < 0.01
            and rev["lost_count"] == len(lost)
            and abs(bct["NEW"]["amount"] + bct["EXISTING"]["amount"] - rev["open_amount"]) < 0.01,
            f"won {rev['won_count']}/{len(won)} {rev['won_amount']} lost {rev['lost_count']}/{len(lost)} bct={bct}")


@guarded("SP-108", "Deal board by stage")
def _():
    b = must(req("GET", "/opportunities/board", token=U["ag1a"]["token"]))
    full = must(req("GET", "/opportunities/board?include_closed=true", token=U["ag1a"]["token"]))
    ok_cols = all(c["status"] == "OPEN" for c in b["columns"])
    sums = all(abs(c["amount"] - sum(i["amount"] or 0 for i in c["items"])) < 0.01
               and abs(c["weighted"] - round(c["amount"] * c["probability"] / 100, 2)) < 0.01 for c in b["columns"])
    R.check("SP-108", "Deal board: open stage columns with count/amount/weighted; closed columns on request",
            ok_cols and sums and {c["status"] for c in full["columns"]} == {"OPEN", "WON", "LOST"},
            f"open_only={ok_cols} sums={sums}")


@guarded("SP-109", "Retired stage keeps deals but is hidden from new work")
def _():
    must(req("PATCH", f"/sales/stages/{P['demo_stage']}", {"active": False}, A.token))
    active = [s["stage_id"] for s in must(req("GET", "/sales/stages", token=A.token))]
    inact = [s["stage_id"] for s in must(req("GET", "/sales/stages?include_inactive=true", token=A.token))]
    R.check("SP-109", "Admin retires a stage: hidden from the active list, still listed with include_inactive",
            P["demo_stage"] not in active and P["demo_stage"] in inact, f"active={P['demo_stage'] in active}")


@guarded("SP-110", "Agents cannot delete deals; managers can")
def _():
    t = U["ag1a"]["token"]
    o = opp(U["ag1a"], f"Del {RID}", None, 10)
    s1 = req("DELETE", f"/opportunities/{o['opportunity_id']}", token=t)[0]
    s2 = req("DELETE", f"/opportunities/{o['opportunity_id']}", token=U["mgr1"]["token"])[0]
    R.check("SP-110", "Exec cannot delete a deal (403); their BD manager can (200)", s1 == 403 and s2 == 200, f"{s1} {s2}")


# ═══════════════════════ System journeys ═══════════════════════

@guarded("SP-120", "Journey: lead → SQL → opportunity → proposal → won")
def _():
    ex, bd, head = U["ag1b"], U["mgr1"], U["head"]
    steps = []
    c = contact(ex, "Journey", f"jr{RID}", company=f"Journeyco {RID}")
    l = lead(ex, c["prospect_id"], source="Webinar")
    for st in ("CONTACTED", "ENGAGED"):
        steps.append((st, req("PATCH", f"/leads/{l['lead_id']}", {"stage": st}, ex["token"])[0]))
    steps.append(("BANT", bant(ex, l["lead_id"])[0]))
    steps.append(("SQL", req("PATCH", f"/leads/{l['lead_id']}", {"stage": "SQL", "next_step": "Demo"}, ex["token"])[0]))
    in_q = l["lead_id"] in ids_of(must(req("GET", "/leads/sql-queue", token=bd["token"]))["items"], "lead_id")
    s, o = req("POST", f"/leads/{l['lead_id']}/convert", {"amount": 50000, "close_date": str(date.today() + timedelta(days=10))},
               ex["token"])
    steps.append(("convert", s))
    oid = o["opportunity_id"]
    for name in ("Needs analysis", "Proposal"):
        steps.append((name, req("PATCH", f"/opportunities/{oid}", {"stage_id": REC["stages"][name]["stage_id"]}, ex["token"])[0]))
    p = must(req("POST", "/proposals", {"opportunity_id": oid, "status": "SENT"}, ex["token"]), 201)
    steps.append(("proposal accepted", req("PATCH", f"/proposals/{p['proposal_id']}", {"status": "ACCEPTED"}, ex["token"])[0]))
    steps.append(("won", req("PATCH", f"/opportunities/{oid}", {"stage_id": REC["stages"]["Closed won"]["stage_id"],
                                                                "closed_reason": "Strong champion"}, ex["token"])[0]))
    lc = must(contact_detail(ex["token"], c["prospect_id"]))["lifecycle_stage"]
    head_sees = req("GET", f"/opportunities/{oid}", token=head["token"])[0]
    won_bd = must(req("GET", f"/pipeline/revenue?owner={ex['user_id']}", token=bd["token"]))["totals"]["won_amount"]
    bad = [x for x in steps if x[1] not in (200, 201)]
    R.check("SP-120", "Exec journey NEW→CONTACTED→ENGAGED→BANT→SQL→(in BD queue)→convert→stages→proposal accepted→won; contact CUSTOMER; Head sees it; BD won total includes it",
            not bad and in_q and lc == "CUSTOMER" and head_sees == 200 and won_bd >= 50000,
            f"bad={bad} in_queue={in_q} lc={lc} head={head_sees} won={won_bd}")


@guarded("SP-121", "Journey: reply → lead → SQL → opportunity → lost")
def _():
    t = U["ag1a"]["token"]
    lid = L["reply_lead"]
    steps = [("bant", bant(U["ag1a"], lid)[0]),
             ("sql", req("PATCH", f"/leads/{lid}", {"stage": "SQL"}, t)[0])]
    s, o = req("POST", f"/leads/{lid}/convert", {"amount": 9000}, t)
    steps.append(("convert", s))
    steps.append(("lost", req("PATCH", f"/opportunities/{o['opportunity_id']}",
                              {"stage_id": REC["stages"]["Closed lost"]["stage_id"], "closed_reason": "Price"}, t)[0]))
    d = must(req("GET", f"/opportunities/{o['opportunity_id']}", token=t))
    bad = [x for x in steps if x[1] not in (200, 201)]
    R.check("SP-121", "Reply-to-loss journey: deal keeps the reply's campaign (ROI link), lead link and LOST with reason",
            not bad and d["status"] == "LOST" and d["campaign_id"] and d["lead"] and d["lead"]["lead_id"] == lid,
            f"bad={bad} status={d.get('status')} campaign={d.get('campaign_id')}")


# ═══════════════════════ 5.5 Daily new-contact limit ═══════════════════════

DL = {}


def dl_status(k):
    return must(req("GET", "/sales-reports/daily-limit", token=U[k]["token"]))


def seed_campaign(creator_key, status="DRAFT"):
    cid = str(uuid.uuid4())
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    sql(f"INSERT INTO campaigns (campaign_id, tenant_id, campaign_name, created_by, status, auto_paused, campaign_timezone,"
        f" respect_timezone, send_window_start, send_window_end, created_at) VALUES ('{cid}','{A.tenant_id}',"
        f"'DL camp {creator_key} {RID} {cid[:4]}','{U[creator_key]['user_id']}','{status}',0,'UTC',0,'00:00:00','23:59:59',UTC_TIMESTAMP())")
    sql(f"INSERT INTO email_sequences (sequence_id, campaign_id, step_number, wait_days) VALUES "
        f"('{s1}','{cid}',1,0),('{s2}','{cid}',2,2)")
    return cid, s1, s2


def seed_prospects(owner_key, n, tag):
    ids = [str(uuid.uuid4()) for _ in range(n)]
    owner = q(U[owner_key]["user_id"]) if owner_key else "NULL"
    rows = ",".join(f"('{p}','{A.tenant_id}','dl{tag}{i}-{RID}@dl-sp.example',{owner},'Dl','{tag}{i}','OPT_IN',1,UTC_TIMESTAMP())"
                    for i, p in enumerate(ids))
    sql("INSERT INTO prospects (prospect_id, tenant_id, email, owner_id, first_name, last_name, consent_status, "
        f"is_valid_email, created_at) VALUES {rows}")
    return ids


def seed_msgs(prospect_ids, cid, seq, status="SENT", sent="UTC_TIMESTAMP()", extra_cols="", extra_vals=""):
    rows = ",".join(f"('{uuid.uuid4()}','{p}','x@dl-sp.example','{cid}','{seq}','OUTBOUND','{status}',{sent}{extra_vals})"
                    for p in prospect_ids)
    sql("INSERT INTO email_messages (message_id, prospect_id, to_email, campaign_id, sequence_id, direction, status, sent_at"
        f"{extra_cols}) VALUES {rows}")


@guarded("SP-130", "Daily limit status at start of day")
def _():
    st = dl_status("dl1")
    R.check("SP-130", "User with no first emails today: limit 500, used 0, remaining 500, not reached",
            st["limit"] == 500 and st["used_today"] == 0 and st["remaining"] == 500 and st["reached"] is False
            and st["message"] is None, f"{st}")


@guarded("SP-131", "Enrollment notice is based on the contact owner's quota")
def _():
    # dl1 (exec) will be at the limit; their BD manager (dlm, quota untouched) enrolls two of dl1's contacts
    cid, s1, s2 = seed_campaign("dl1")
    DL["camp"], DL["s1"], DL["s2"] = cid, s1, s2
    p500 = seed_prospects("dl1", 500, "a")
    seed_msgs(p500, cid, s1)
    fresh = seed_prospects("dl1", 2, "enr")
    mcid, _m1, _m2 = seed_campaign("dlm")
    DL["mcamp"] = mcid
    s, b = req("POST", f"/campaigns/{mcid}/prospects", {"prospect_ids": fresh}, U["dlm"]["token"])
    R.check("SP-131", "BD manager enrolls an exec's contacts while the exec is at 500: user is told the first emails will wait",
            s == 200 and b.get("enrolled_count") == 2 and b.get("daily_limit_notice"),
            f"HTTP {s} enrolled={b.get('enrolled_count') if isinstance(b, dict) else b} rejected={b.get('rejected_summary') if isinstance(b, dict) else ''} notice={b.get('daily_limit_notice') if isinstance(b, dict) else ''}")


@guarded("SP-132", "Boundary: 499 then 500 first emails")
def _():
    # dl2: 499 first emails today + follow-ups + yesterday's first emails
    cid, s1, s2 = seed_campaign("dl2")
    p = seed_prospects("dl2", 499, "b")
    seed_msgs(p, cid, s1)
    seed_msgs(p[:20], cid, s2)                                            # follow-ups today: never count
    seed_msgs(seed_prospects("dl2", 7, "y"), cid, s1, sent="UTC_TIMESTAMP() - INTERVAL 1 DAY")  # yesterday: reset
    st499 = dl_status("dl2")
    R.check("SP-132", "At 499 new contacts (plus 20 follow-ups today and 7 yesterday): used 499, remaining 1, not reached",
            st499["used_today"] == 499 and st499["remaining"] == 1 and st499["reached"] is False and st499["message"] is None,
            f"{st499}")
    seed_msgs(seed_prospects("dl2", 1, "c"), cid, s1)
    st500 = dl_status("dl2")
    R.check("SP-133", "At exactly 500: reached, remaining 0, and the user is told (message mentions 500 and follow-ups)",
            st500["used_today"] == 500 and st500["remaining"] == 0 and st500["reached"] is True
            and "500" in (st500["message"] or "") and "ollow" in (st500["message"] or ""), f"{st500}")
    DL["dl2"] = (cid, s1, s2)


@guarded("SP-134", "Limit is per user")
def _():
    others = {k: dl_status(k)["used_today"] for k in ("dlm", "ag1a")}
    R.check("SP-134", "Another user's 500 first emails don't consume this user's quota", others == {"dlm": 0, "ag1a": 0},
            f"{others}")


@guarded("SP-135", "Unowned contacts count against the campaign creator")
def _():
    cid, s1, _s2 = seed_campaign("dlm")
    seed_msgs(seed_prospects(None, 3, "u"), cid, s1)
    R.check("SP-135", "First emails to unowned contacts count for the campaign creator", dl_status("dlm")["used_today"] == 3,
            f"{dl_status('dlm')}")


@guarded("SP-136", "Held first emails are reported to the user")
def _():
    cid, s1, _s2 = DL["dl2"]
    seed_msgs(seed_prospects("dl2", 2, "h"), cid, s1, status="QUEUED", sent="NULL",
              extra_cols=", last_error_code, scheduled_at", extra_vals=f", '{HELD}', UTC_DATE() + INTERVAL 1 DAY")
    st = dl_status("dl2")
    R.check("SP-136", "Held-for-tomorrow count and message tell the user how many first emails were queued for the next day",
            st["held_for_tomorrow"] == 2 and "2 first emails are queued for tomorrow" in (st["message"] or ""), f"{st}")


@guarded("SP-137", "Enrollment notice when the user's own quota runs out")
def _():
    # dlm has 3 used (SP-135); add 496 → 499 used; enrolling 3 own contacts leaves 2 waiting
    cid, s1, _s2 = seed_campaign("dlm")
    seed_msgs(seed_prospects("dlm", 496, "m"), cid, s1)
    fresh = seed_prospects("dlm", 3, "me")
    s, b = req("POST", f"/campaigns/{DL['mcamp']}/prospects", {"prospect_ids": fresh}, U["dlm"]["token"])
    note = (b or {}).get("daily_limit_notice") or ""
    under = seed_prospects("mgr2", 2, "un")
    mc2 = seed_campaign("mgr2")[0]
    s2, b2 = req("POST", f"/campaigns/{mc2}/prospects", {"prospect_ids": under}, U["mgr2"]["token"])
    R.check("SP-137", "Enrolling past the remaining quota returns a notice (1 left, 2 wait); within quota → no notice",
            s == 200 and "Only 1 of today's 500" in note and "other 2" in note and s2 == 200
            and b2.get("enrolled_count") == 2 and b2.get("daily_limit_notice") is None, f"{s} {note!r} | {s2} {b2}")


@guarded("SP-138", "Imports do not consume the daily limit")
def _():
    before = dl_status("dl1")["used_today"]
    csv = "email,first_name\n" + "\n".join(f"imp{i}-{RID}@imp-sp.example,Imp{i}" for i in range(3))
    s, b = req("POST", "/imports", files={"file": ("dl.csv", csv.encode())},
               form={"mapping": json.dumps({"email": "contact.email", "first_name": "contact.first_name"}),
                     "options": json.dumps({"owner_id": U["dl1"]["user_id"]})}, token=A.token)
    imported = poll(lambda: sql(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{A.tenant_id}' AND email LIKE 'imp%-{RID}@imp-sp.example' "
                                f"AND owner_id='{U['dl1']['user_id']}'") == "3", timeout=60, every=2)
    after = dl_status("dl1")["used_today"]
    R.check("SP-138", "Importing 3 contacts for a user at the limit leaves used_today unchanged (only first sends count)",
            s in (200, 201) and imported and after == before == 500, f"import={s} imported={imported} before={before} after={after}")


@guarded("SP-139", "Scheduler holds the 501st new contact until tomorrow (worker)")
def _():
    if NO_WORKER:
        R.record("SP-139", "Scheduler holds the 501st new contact until tomorrow (worker)", None, "skipped (--no-worker)", "test-issue")
        R.record("SP-140", "Follow-ups keep sending after the limit (worker)", None, "skipped (--no-worker)", "test-issue")
        return
    # Active campaign, dl1 already at 500. One new first email and one follow-up become due now.
    # No sender mailbox and no system sender exist, so nothing can actually be delivered.
    cid, s1, s2 = seed_campaign("dl1", status="ACTIVE")
    DL["active"] = cid
    new_p, fu_p = seed_prospects("dl1", 1, "n")[0], seed_prospects("dl1", 1, "f")[0]
    m_new, m_fu = str(uuid.uuid4()), str(uuid.uuid4())
    seed_msgs([fu_p], cid, s1, sent="UTC_TIMESTAMP() - INTERVAL 3 DAY")  # follow-up's first step went out earlier
    for mid, p, seq in ((m_new, new_p, s1), (m_fu, fu_p, s2)):
        sql(f"INSERT INTO email_messages (message_id, prospect_id, to_email, campaign_id, sequence_id, direction, status, "
            f"scheduled_at, subject, body_text, max_retries, retry_count) VALUES ('{mid}','{p}','nobody@dl-sp.invalid','{cid}','{seq}',"
            f"'OUTBOUND','QUEUED', UTC_TIMESTAMP() - INTERVAL 1 MINUTE, 'Hi', 'Hello', 0, 0)")
    try:
        row = lambda m: sql(f"SELECT CONCAT_WS('|', status, IFNULL(last_error_code,'-'), IFNULL(DATE(scheduled_at),'-'), "  # noqa: E731
                            f"IFNULL(failure_reason,'-')) FROM email_messages WHERE message_id='{m}'")
        held = poll(lambda: HELD in row(m_new) and row(m_new), timeout=200)
        tomorrow = str(date.today() + timedelta(days=1)) if datetime.utcnow().date() == date.today() else str(datetime.utcnow().date() + timedelta(days=1))
        r = row(m_new)
        R.check("SP-139", "Worker: 501st new contact's first email is held (QUEUED, DAILY_NEW_CONTACT_LIMIT) and rescheduled to tomorrow",
                bool(held) and r.startswith("QUEUED|") and f"|{tomorrow}|" in r, f"row={r}", "defect")
        fu = poll(lambda: (row(m_fu).split("|")[0] != "QUEUED" or row(m_fu).split("|")[3] != "-") and row(m_fu), timeout=120)
        rf = row(m_fu)
        R.check("SP-140", "Worker: a follow-up for the same user at the limit is not held by the daily limit (it proceeds to send)",
                bool(fu) and HELD not in rf, f"row={rf}" if fu else f"follow-up never processed within 120 s: {rf}",
                "defect" if fu else "test-issue")
    finally:
        sql(f"UPDATE campaigns SET status='PAUSED' WHERE campaign_id='{cid}'")
        sql(f"UPDATE email_messages SET status='CANCELLED' WHERE campaign_id='{cid}' AND status IN ('QUEUED','SCHEDULED')")


# ═══════════════════════ Worker integrations (BR-SF-02/05) ═══════════════════════

@guarded("SP-141", "Worker recycles a disqualified lead on its recycle date")
def _():
    if NO_WORKER:
        R.record("SP-141", "Worker recycles a disqualified lead on its recycle date", None, "skipped (--no-worker)", "test-issue")
        return
    sql(f"UPDATE leads SET recycle_at = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE lead_id='{L['dq']}'")
    back = poll(lambda: sql(f"SELECT stage FROM leads WHERE lead_id='{L['dq']}'") == "NEW")
    notes = must(req("GET", "/notifications?limit=100", token=U["ag1a"]["token"]))["items"]
    hist = must(req("GET", f"/leads/{L['dq']}", token=U["ag1a"]["token"]))["history"]
    R.check("SP-141", "Worker reopens the disqualified lead (→ NEW), notifies the owner and logs the change",
            bool(back) and any(n.get("kind") == "LEAD_RECYCLED" for n in notes)
            and any(h["field"] == "stage" and h["new_value"] == "NEW" for h in hist),
            f"back={back} kinds={[n.get('kind') for n in notes][:5]}")


@guarded("SP-142", "Worker alerts the owner about a stale/overdue deal")
def _():
    if NO_WORKER:
        R.record("SP-142", "Worker alerts the owner about a stale/overdue deal", None, "skipped (--no-worker)", "test-issue")
        return
    got = poll(lambda: any(n.get("kind") == "STALE_DEAL" and P["overdue"] in (n.get("link") or "")
                           for n in must(req("GET", "/notifications?limit=100", token=U["ag1a"]["token"]))["items"]))
    R.check("SP-142", "Overdue deal raises a STALE_DEAL notification for its owner within a worker cycle",
            bool(got), "no STALE_DEAL notification within 170 s")


# ═══════════════════════ UI (Playwright) ═══════════════════════

@guarded("SP-150", "UI checks")
def _():
    if NO_UI:
        R.record("SP-150", "UI checks", None, "skipped (--no-ui)", "test-issue")
        return
    fx = {
        "web": WEB, "password": A.password, "out": os.path.join(HERE, "ui_shots"),
        "mgr1": U["mgr1"]["email"], "ag1a": U["ag1a"]["email"],
        "team_leads": [REC["ag1a"]["last"], REC["ag1b"]["last"]], "other_leads": [REC["ag2a"]["last"], REC["mgr2"]["last"]],
        "own_lead": REC["ag1a"]["last"], "peer_lead": REC["ag1b"]["last"], "peer_lead_id": REC["ag1b"]["lead"],
        "team_sql": [REC["ag1a"]["sql_last"]], "other_sql": [REC["ag2a"]["sql_last"]],
        "team_deals": [REC["ag1a"]["opp_name"], REC["ag1b"]["opp_name"]], "other_deals": [REC["ag2a"]["opp_name"]],
        "own_deal": REC["ag1a"]["opp_name"], "peer_deal": REC["ag1b"]["opp_name"], "peer_deal_id": REC["ag1b"]["opp"],
    }
    fx_path = os.path.join(HERE, "ui_fixture.json")
    with open(fx_path, "w") as f:
        json.dump(fx, f)
    env = dict(os.environ, PW_MODULES=PW_MODULES)
    out = subprocess.run(["node", os.path.join(HERE, "ui_sales.mjs"), fx_path], capture_output=True, text=True,
                         timeout=600, env=env)
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        R.record("SP-150", "UI checks", None, f"UI script failed: {out.stdout[-300:]} {out.stderr[-500:]}", "test-issue")
        return
    for c in res:
        R.record(c["id"], c["title"], c["pass"], c.get("detail", ""), None if c["pass"] else c.get("category", "defect"))


counts = R.write()
print(counts)
