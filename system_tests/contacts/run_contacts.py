#!/usr/bin/env python3
"""
System & integration tests for BRD v2.0 §5.2 Contact Management (BR-CM-01..43) plus the
CM-related NFRs (§6) and data rules (§7), run against the live stack.

    python3 system_tests/contacts/run_contacts.py            # functional + UI + performance
    python3 system_tests/contacts/run_contacts.py --no-perf  # skip the 10k-row import / search latency
    python3 system_tests/contacts/run_contacts.py --no-ui    # skip Playwright

Every run creates fresh workspaces (tenants) via self sign-up, so it is re-runnable and never
touches other tenants' data (except the global CRM purge job, which the BRD defines as global).
Results: results.json / results.md / perf.json next to this file.
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import API, WEB, Results, Workspace, req, sql  # noqa: E402
from cm_helpers import count, csv_bytes, parse_csv, pct, q, run_import, timed, xlsx_bytes  # noqa: E402

R = Results("Contact Management (BRD §5.2)", HERE)
PERF = "--no-perf" not in sys.argv
UI = "--no-ui" not in sys.argv
ONLY = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--only=")), None)


class Ctx:
    pass


C = Ctx()


def case(cid, title):
    """Decorator: run the case, record an unexpected exception as a FAIL (to be triaged)."""
    def wrap(fn):
        if ONLY and not any(cid.startswith(o) for o in ONLY):
            return fn
        try:
            fn()
        except AssertionError as exc:
            R.record(cid, title, False, f"precondition failed: {exc}", "test-issue")
        except Exception as exc:  # noqa: BLE001
            R.record(cid, title, False, f"exception {type(exc).__name__}: {exc} | {traceback.format_exc()[-300:]}",
                     "test-issue")
        return fn
    return wrap


def ok(status, body, expect=(200, 201)):
    expect = expect if isinstance(expect, tuple) else (expect,)
    assert status in expect, f"HTTP {status}: {str(body)[:300]}"
    return body


def new_contact(token, email, **kw):
    s, b = req("POST", "/contacts", {"email": email, **kw}, token)
    return ok(s, b, 201)


def em(local, dom=None):
    return f"{local}.{C.rid}@{dom or C.dom}"


# ════════════════════════════════════════════════════════════════════
# SETUP
# ════════════════════════════════════════════════════════════════════
W = Workspace("contacts")
C.rid = W.rid
C.dom = f"acme-{W.rid}.io"
C.owner = W.token
C.admin = W.add_user("ADMIN", f"Ada{W.rid}")
C.manager = W.add_user("MANAGER", f"Max{W.rid}")
C.agent1 = W.add_user("AGENT", f"Ann{W.rid}")
C.agent2 = W.add_user("AGENT", f"Bob{W.rid}")
C.agent_mp = W.add_user("AGENT", f"Pia{W.rid}", custom_permissions='["manage_prospects"]')
W2 = Workspace("contacts-b")
print(f"workspace A tenant={W.tenant_id} owner={W.email}; workspace B tenant={W2.tenant_id}")

# ════════════════════════════════════════════════════════════════════
# CONTACTS & COMPANIES (BR-CM-01..05)
# ════════════════════════════════════════════════════════════════════


@case("CM-001", "Create contact with all standard properties (BR-CM-01)")
def _():
    body = {"first_name": "Jane", "last_name": "Doe", "phone": "+1 (512) 555-0143", "mobile_phone": "+1 512 555 0199",
            "designation": "VP Sales", "linkedin_url": "https://linkedin.com/in/janedoe", "poc_city": "Austin",
            "poc_state": "Texas", "poc_country": "United States", "lead_source": "Webinar",
            "owner_id": C.admin["user_id"]}
    c = new_contact(C.owner, em("jane.doe"), **body)
    C.jane = c
    s, d = req("GET", f"/contacts/{c['prospect_id']}", None, C.owner)
    ok(s, d)
    missing = [k for k, v in body.items() if d.get(k) != v]
    need_keys = ["prospect_id", "created_at", "last_activity_at", "last_contacted_at", "timezone", "owner_name"]
    absent = [k for k in need_keys if k not in d]
    R.check("CM-001", "Create contact with all standard properties (BR-CM-01)",
            not missing and not absent and d["timezone"] and d["created_at"] and d["owner_name"],
            f"mismatched={missing} absent_keys={absent} timezone={d.get('timezone')} owner_name={d.get('owner_name')}")


@case("CM-002", "Last activity / last contacted dates follow logged activities (BR-CM-01)")
def _():
    c = new_contact(C.owner, em("lastact"))
    pid = c["prospect_id"]
    ok(*req("POST", f"/contacts/{pid}/activities", {"activity_type": "NOTE", "body": "just a note"}, C.owner))
    s, d1 = req("GET", f"/contacts/{pid}", None, C.owner)
    ok(*req("POST", f"/contacts/{pid}/activities", {"activity_type": "CALL", "subject": "Intro call",
                                                     "outcome": "Connected", "duration_minutes": 5}, C.owner))
    s, d2 = req("GET", f"/contacts/{pid}", None, C.owner)
    R.check("CM-002", "Last activity / last contacted dates follow logged activities (BR-CM-01)",
            d1["last_activity_at"] and d1["last_contacted_at"] is None and d2["last_contacted_at"]
            and d2["lead_status"] == "CONNECTED",
            f"after note: act={d1['last_activity_at']} contacted={d1['last_contacted_at']}; after call: "
            f"contacted={d2['last_contacted_at']} lead_status={d2['lead_status']}")


@case("CM-003", "Duplicate email (exact) is rejected with 409 (BR-CM-02, §7)")
def _():
    s, b = req("POST", "/contacts", {"email": C.jane["email"]}, C.owner)
    R.check("CM-003", "Duplicate email (exact) is rejected with 409 (BR-CM-02, §7)",
            s == 409 and b["detail"]["prospect_id"] == C.jane["prospect_id"], f"{s} {b}")


@case("CM-004", "Duplicate email differing only in case/whitespace is rejected (BR-CM-02)")
def _():
    s1, b1 = req("POST", "/contacts", {"email": C.jane["email"].upper()}, C.owner)
    s2, b2 = req("POST", "/contacts", {"email": "  " + C.jane["email"] + " "}, C.owner)
    n = count(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{W.tenant_id}' AND LOWER(TRIM(email))='{C.jane['email'].lower()}'")
    R.check("CM-004", "Duplicate email differing only in case/whitespace is rejected (BR-CM-02)",
            s1 == 409 and s2 in (409, 422) and n == 1, f"upper={s1} {b1}; spaces={s2} {b2}; rows={n}")


@case("CM-005", "Changing a contact's email to another contact's email is rejected (BR-CM-02)")
def _():
    other = new_contact(C.owner, em("other.person"))
    s, b = req("PATCH", f"/contacts/{other['prospect_id']}", {"email": C.jane["email"].upper()}, C.owner)
    R.check("CM-005", "Changing a contact's email to another contact's email is rejected (BR-CM-02)",
            s == 409, f"{s} {b}")


@case("CM-006", "Invalid / missing email rejected on create (BR-CM-02)")
def _():
    s1, _ = req("POST", "/contacts", {"email": "not-an-email"}, C.owner)
    s2, _ = req("POST", "/contacts", {"first_name": "NoEmail"}, C.owner)
    R.check("CM-006", "Invalid / missing email rejected on create (BR-CM-02)", s1 == 422 and s2 == 422, f"{s1} {s2}")


@case("CM-007", "Each contact has an immutable system Record ID (BR-CM-02)")
def _():
    pid = C.jane["prospect_id"]
    s, b = req("PATCH", f"/contacts/{pid}", {"prospect_id": "hacked-id", "designation": "VP Sales"}, C.owner)
    n = count(f"SELECT COUNT(*) FROM prospects WHERE prospect_id='{pid}'")
    R.check("CM-007", "Each contact has an immutable system Record ID (BR-CM-02)",
            s == 200 and b["prospect_id"] == pid and n == 1 and len(pid) == 36, f"{s} {str(b)[:200]}")


@case("CM-008", "Create company with all properties; domain normalised (BR-CM-03)")
def _():
    body = {"name": f"Globex {C.rid}", "domain": f"https://www.Globex-{C.rid}.com/about", "industry": "Software",
            "emp_band": "51-200", "annual_revenue": "1,250,000", "phone": "+1 512 555 0100", "street": "1 Main St",
            "city": "Austin", "state": "Texas", "postal_code": "78701", "country": "United States",
            "owner_id": C.admin["user_id"]}
    s, a = req("POST", "/accounts", body, C.owner)
    ok(s, a, 201)
    C.globex = a
    s, d = req("GET", f"/accounts/{a['account_id']}", None, C.owner)
    exp = {"domain": f"globex-{C.rid}.com", "industry": "Software", "emp_band": "51-200", "annual_revenue": 1250000.0,
           "city": "Austin", "postal_code": "78701", "owner_id": C.admin["user_id"], "phone": "+1 512 555 0100"}
    bad = {k: d.get(k) for k, v in exp.items() if d.get(k) != v}
    R.check("CM-008", "Create company with all properties; domain normalised (BR-CM-03)", s == 200 and not bad, f"{bad}")


@case("CM-009", "Company domain is unique per workspace; duplicate name rejected (BR-CM-03)")
def _():
    s1, b1 = req("POST", "/accounts", {"name": f"Globex Two {C.rid}", "domain": f"globex-{C.rid}.com"}, C.owner)
    s2, b2 = req("POST", "/accounts", {"name": f"Globex {C.rid}"}, C.owner)
    s3, b3 = req("PATCH", f"/accounts/{C.jane['account_id']}", {"domain": f"globex-{C.rid}.com"}, C.owner)
    R.check("CM-009", "Company domain is unique per workspace; duplicate name rejected (BR-CM-03)",
            s1 == 409 and s2 == 409 and s3 == 409, f"dup domain create={s1} {b1}; dup name={s2}; dup domain patch={s3} {b3}")


@case("CM-010", "Company auto-created from email domain; colleagues auto-associated (BR-CM-04)")
def _():
    a = C.jane
    b = new_contact(C.owner, em("john.roe"))
    s, acc = req("GET", f"/accounts/{a['account_id']}", None, C.owner)
    n_acc = count(f"SELECT COUNT(*) FROM accounts WHERE tenant_id='{W.tenant_id}' AND domain='{C.dom}'")
    R.check("CM-010", "Company auto-created from email domain; colleagues auto-associated (BR-CM-04)",
            a["account_id"] and a["account_id"] == b["account_id"] and acc.get("domain") == C.dom and n_acc == 1
            and acc.get("contact_count") >= 2,
            f"jane.account={a['account_id']} john.account={b['account_id']} domain={acc.get('domain')} n_acc={n_acc}")


@case("CM-011", "Personal email domains don't create a company; company name is used instead (BR-CM-04)")
def _():
    c = new_contact(C.owner, f"solo.{C.rid}@gmail.com", company_name=f"Solo Ventures {C.rid}")
    n_gmail = count(f"SELECT COUNT(*) FROM accounts WHERE tenant_id='{W.tenant_id}' AND domain='gmail.com'")
    R.check("CM-011", "Personal email domains don't create a company; company name is used instead (BR-CM-04)",
            n_gmail == 0 and c["account_name"] == f"Solo Ventures {C.rid}", f"gmail accounts={n_gmail} acc={c['account_name']}")


@case("CM-012", "Contact on a soft-deleted company's domain can still be created (BR-CM-04 edge)")
def _():
    dom = f"ghostco-{C.rid}.com"
    first = new_contact(C.owner, f"first@{dom}")
    ok(*req("DELETE", f"/accounts/{first['account_id']}", None, C.owner))
    s, b = req("POST", "/contacts", {"email": f"second@{dom}", "first_name": "Second"}, C.owner)
    R.check("CM-012", "Contact on a soft-deleted company's domain can still be created (BR-CM-04 edge)",
            s == 201, f"POST /contacts after deleting company of {dom} -> {s} {str(b)[:200]}")


@case("CM-013", "Import row on a soft-deleted company's domain is not linked to the deleted company (BR-CM-04 edge)")
def _():
    dom = f"ghostimp-{C.rid}.com"
    first = new_contact(C.owner, f"first@{dom}")
    acc_id = first["account_id"]
    ok(*req("DELETE", f"/accounts/{acc_id}", None, C.owner))
    st, body, _ = run_import(C.owner, csv_bytes(["Email", "First Name"], [[f"imported@{dom}", "Imp"]]),
                              {"Email": "contact.email", "First Name": "contact.first_name"})
    linked = sql(f"SELECT IFNULL(a.deleted_at,'live') FROM prospects p JOIN accounts a ON a.account_id=p.account_id "
                 f"WHERE p.tenant_id='{W.tenant_id}' AND p.email='imported@{dom}'")
    R.check("CM-013", "Import row on a soft-deleted company's domain is not linked to the deleted company (BR-CM-04 edge)",
            st == 201 and (linked in ("", "live")),
            f"import={st} created={body.get('contacts_created') if isinstance(body, dict) else body}; "
            f"contact linked to account with deleted_at={linked!r}")


@case("CM-014", "Contact linked to several companies with primary + association labels (BR-CM-05)")
def _():
    s, spec = req("GET", "/openapi.json")
    props = spec["components"]["schemas"]["ContactCreate"]["properties"]
    multi = [k for k in props if "compan" in k and k != "company_name" or "association" in k]
    if multi:
        R.record("CM-014", "Contact linked to several companies with primary + association labels (BR-CM-05)", None,
                 f"API has {multi}; extend test", "test-issue")
    else:
        R.record("CM-014", "Contact linked to several companies with primary + association labels (BR-CM-05)", None,
                 "Contact has a single account_id; no multi-company association or labels in API/schema",
                 "not-implemented")


@case("CM-015", "Renaming a company updates its contacts' company name (BR-CM-03)")
def _():
    new = f"Acme Renamed {C.rid}"
    ok(*req("PATCH", f"/accounts/{C.jane['account_id']}", {"name": new}, C.owner))
    s, d = req("GET", f"/contacts/{C.jane['prospect_id']}", None, C.owner)
    R.check("CM-015", "Renaming a company updates its contacts' company name (BR-CM-03)",
            d["company_name"] == new and d["account"]["name"] == new, f"{d['company_name']}")


# ════════════════════════════════════════════════════════════════════
# LIFECYCLE STAGE & LEAD STATUS (BR-CM-06..08)
# ════════════════════════════════════════════════════════════════════
STAGES = ["SUBSCRIBER", "LEAD", "MQL", "SQL", "OPPORTUNITY", "CUSTOMER", "EVANGELIST", "OTHER"]
STATUSES = ["NEW", "OPEN", "IN_PROGRESS", "ATTEMPTED_TO_CONTACT", "CONNECTED", "OPEN_DEAL", "BAD_TIMING", "UNQUALIFIED"]


@case("CM-016", "All 8 lifecycle stages on contacts and companies; invalid rejected (BR-CM-06)")
def _():
    s, meta = req("GET", "/contacts/meta", None, C.owner)
    c = new_contact(C.owner, em("stages"))
    acc = ok(*req("POST", "/accounts", {"name": f"Stage Co {C.rid}"}, C.owner))
    fails = []
    for st in STAGES:
        s1, _ = req("PATCH", f"/contacts/{c['prospect_id']}", {"lifecycle_stage": st}, C.owner)
        s2, _ = req("PATCH", f"/accounts/{acc['account_id']}", {"lifecycle_stage": st}, C.owner)
        if s1 != 200 or s2 != 200:
            fails.append((st, s1, s2))
    b1, _ = req("PATCH", f"/contacts/{c['prospect_id']}", {"lifecycle_stage": "PROSPECT"}, C.owner)
    b2, _ = req("PATCH", f"/accounts/{acc['account_id']}", {"lifecycle_stage": "PROSPECT"}, C.owner)
    R.check("CM-016", "All 8 lifecycle stages on contacts and companies; invalid rejected (BR-CM-06)",
            [x["value"] for x in meta["lifecycle_stages"]] == STAGES and not fails and b1 == 400 and b2 == 400,
            f"meta={[x['value'] for x in meta['lifecycle_stages']]} fails={fails} invalid={b1},{b2}")


@case("CM-017", "All 8 lead statuses; invalid rejected (BR-CM-07)")
def _():
    s, meta = req("GET", "/contacts/meta", None, C.owner)
    c = new_contact(C.owner, em("statuses"))
    fails = [st for st in STATUSES
             if req("PATCH", f"/contacts/{c['prospect_id']}", {"lead_status": st}, C.owner)[0] != 200]
    bad, _ = req("PATCH", f"/contacts/{c['prospect_id']}", {"lead_status": "HOT"}, C.owner)
    R.check("CM-017", "All 8 lead statuses; invalid rejected (BR-CM-07)",
            [x["value"] for x in meta["lead_statuses"]] == STATUSES and not fails and bad == 400,
            f"fails={fails} invalid={bad}")


@case("CM-018", "Non-admin can move lifecycle forward but not backward (BR-CM-08)")
def _():
    c = new_contact(C.agent1["token"], em("agentlc"))
    pid = c["prospect_id"]
    f, _ = req("PATCH", f"/contacts/{pid}", {"lifecycle_stage": "MQL"}, C.agent1["token"])
    b, bb = req("PATCH", f"/contacts/{pid}", {"lifecycle_stage": "LEAD"}, C.agent1["token"])
    o, _ = req("PATCH", f"/contacts/{pid}", {"lifecycle_stage": "OTHER"}, C.agent1["token"])
    C.agent1_contact = c
    R.check("CM-018", "Non-admin can move lifecycle forward but not backward (BR-CM-08)",
            f == 200 and b == 400, f"forward={f} backward={b} {bb} other={o}")


@case("CM-019", "Admin can move lifecycle backward (BR-CM-08)")
def _():
    c = new_contact(C.owner, em("adminlc"), lifecycle_stage="CUSTOMER")
    s1, _ = req("PATCH", f"/contacts/{c['prospect_id']}", {"lifecycle_stage": "SUBSCRIBER"}, C.admin["token"])
    s2, d = req("GET", f"/contacts/{c['prospect_id']}", None, C.owner)
    R.check("CM-019", "Admin can move lifecycle backward (BR-CM-08)", s1 == 200 and d["lifecycle_stage"] == "SUBSCRIBER",
            f"{s1} {d.get('lifecycle_stage')}")


@case("CM-020", "Company lifecycle backward move blocked for non-admin (BR-CM-08)")
def _():
    acc = ok(*req("POST", "/accounts", {"name": f"Agent Co {C.rid}"}, C.agent1["token"]))
    f, fb = req("PATCH", f"/accounts/{acc['account_id']}", {"lifecycle_stage": "SQL"}, C.agent1["token"])
    b, bb = req("PATCH", f"/accounts/{acc['account_id']}", {"lifecycle_stage": "LEAD"}, C.agent1["token"])
    R.check("CM-020", "Company lifecycle backward move blocked for non-admin (BR-CM-08)", f == 200 and b == 400,
            f"forward={f} {str(fb)[:120]} backward={b} {str(bb)[:120]}")


# ════════════════════════════════════════════════════════════════════
# PROPERTIES (BR-CM-09..11)
# ════════════════════════════════════════════════════════════════════
@case("CM-021", "Admin creates custom properties of all 8 types (BR-CM-09)")
def _():
    defs = [("Seats", "NUMBER", None), ("Renewal Date", "DATE", None), ("Tier", "SELECT", ["Gold", "Silver"]),
            ("Interests", "MULTI_CHECKBOX", ["AI", "Cloud", "Data"]), ("Region", "RADIO", ["EMEA", "APAC"]),
            ("Assistant Phone", "PHONE", None), ("Website Link", "URL", None), ("Notes Text", "TEXT", None)]
    C.fields = {}
    fails = []
    for label, ft, opts in defs:
        s, b = req("POST", "/contacts/fields", {"label": f"{label} {C.rid}", "field_type": ft, "options": opts,
                                                "group_name": "Qualification"}, C.owner)
        if s != 201:
            fails.append((label, s, b))
        else:
            C.fields[ft] = b["field_key"]
    s, lst = req("GET", "/contacts/fields", None, C.agent1["token"])
    R.check("CM-021", "Admin creates custom properties of all 8 types (BR-CM-09)",
            not fails and len([f for f in lst if f["field_key"] in C.fields.values()]) == 8
            and all(f["group_name"] == "Qualification" for f in lst if f["field_key"] in C.fields.values()),
            f"fails={fails}")


@case("CM-022", "Custom property values are validated and normalised by type (BR-CM-09)")
def _():
    F = C.fields
    c = new_contact(C.owner, em("custom"))
    pid = c["prospect_id"]
    good = {F["NUMBER"]: "42", F["DATE"]: "2026-12-31", F["SELECT"]: "Gold", F["MULTI_CHECKBOX"]: ["Data", "AI"],
            F["RADIO"]: "EMEA", F["PHONE"]: "+44 20 7946 0958", F["URL"]: "example.com/x", F["TEXT"]: "hello"}
    s, d = req("PATCH", f"/contacts/{pid}", {"custom_fields": good}, C.owner)
    cf = d.get("custom_fields", {}) if s == 200 else {}
    bad_cases = {"NUMBER": "abc", "DATE": "31/12/2026", "SELECT": "Bronze", "MULTI_CHECKBOX": ["AI", "Mobile"],
                 "RADIO": "LATAM", "PHONE": "call me"}
    rejected = {k: req("PATCH", f"/contacts/{pid}", {"custom_fields": {F[k]: v}}, C.owner)[0] for k, v in bad_cases.items()}
    unknown, _ = req("PATCH", f"/contacts/{pid}", {"custom_fields": {"no_such_prop": "x"}}, C.owner)
    C.custom_contact = pid
    R.check("CM-022", "Custom property values are validated and normalised by type (BR-CM-09)",
            s == 200 and cf.get(F["NUMBER"]) == 42 and cf.get(F["MULTI_CHECKBOX"]) == ["AI", "Data"]
            and cf.get(F["URL"]) == "https://example.com/x" and all(v == 400 for v in rejected.values()) and unknown == 400,
            f"save={s} stored={cf} rejected={rejected} unknown={unknown}")


@case("CM-023", "Users without manage permission cannot create properties (BR-CM-09/38)")
def _():
    s, b = req("POST", "/contacts/fields", {"label": f"Agent Prop {C.rid}"}, C.agent1["token"])
    s2, _ = req("POST", "/contacts/fields", {"label": f"Bad type {C.rid}", "field_type": "BLOB"}, C.owner)
    s3, _ = req("POST", "/contacts/fields", {"label": f"No opts {C.rid}", "field_type": "SELECT", "options": []}, C.owner)
    R.check("CM-023", "Users without manage permission cannot create properties (BR-CM-09/38)",
            s == 403 and s2 == 400 and s3 == 400, f"agent={s} bad_type={s2} select_no_options={s3}")


@case("CM-024", "Required property enforced on create via UI/API (BR-CM-10)")
def _():
    s, f = req("POST", "/contacts/fields", {"label": f"Must Have {C.rid}", "field_type": "TEXT", "required": True,
                                            "group_name": "Mandatory"}, C.owner)
    ok(s, f, 201)
    C.req_field = f
    try:
        s1, b1 = req("POST", "/contacts", {"email": em("reqmissing")}, C.owner)
        s2, b2 = req("POST", "/contacts", {"email": em("reqgiven"), "custom_fields": {f["field_key"]: "yes"}}, C.owner)
        # BR-CM-10 also applies to creates through import (no bypass)
        st, body, _ = run_import(C.owner, csv_bytes(["Email", "First Name"], [[em("reqimport"), "Req"]]),
                                     {"Email": "contact.email", "First Name": "contact.first_name"})
        imp_created = body.get("contacts_created") if isinstance(body, dict) else None
    finally:
        ok(*req("PATCH", f"/contacts/fields/{f['field_id']}", {"label": f["label"], "field_type": "TEXT",
                                                                "required": False, "group_name": "Mandatory"}, C.owner))
    R.check("CM-024", "Required property enforced on create via UI/API (BR-CM-10)", s1 == 400 and s2 == 201,
            f"missing={s1} {b1}; given={s2}")
    R.check("CM-024b", "Required property also enforced for contacts created by import (BR-CM-10)",
            st == 201 and imp_created == 0,
            f"import status={st} contacts_created={imp_created} (row lacking required '{f['label']}' was accepted)")


@case("CM-025", "Property change history keeps old/new value, user, time, source (BR-CM-11)")
def _():
    pid = C.jane["prospect_id"]
    ok(*req("PATCH", f"/contacts/{pid}", {"phone": "+1 512 555 7777", "lifecycle_stage": "MQL"}, C.admin["token"]))
    s, h = req("GET", f"/contacts/{pid}/history", None, C.owner)
    items = h["items"]
    ph = next((i for i in items if i["field"] == "phone"), None)
    lc = next((i for i in items if i["field"] == "lifecycle_stage"), None)
    good = ph and ph["old_value"] == "+1 (512) 555-0143" and ph["new_value"] == "+1 512 555 7777" \
        and ph["changed_by"] == C.admin["user_id"] and ph["changed_at"] and ph["source"] == "UI" \
        and lc and lc["old_value"] == "Lead" and lc["new_value"] == "Marketing qualified lead"
    s2, hf = req("GET", f"/contacts/{pid}/history?field=phone", None, C.owner)
    R.check("CM-025", "Property change history keeps old/new value, user, time, source (BR-CM-11)",
            good and all(i["field"] == "phone" for i in hf["items"]), f"phone={ph} lifecycle={lc}")


@case("CM-026", "Custom property and owner changes are in history with readable values (BR-CM-11)")
def _():
    pid = C.custom_contact
    ok(*req("PATCH", f"/contacts/{pid}", {"custom_fields": {C.fields["NUMBER"]: 43}, "owner_id": C.agent2["user_id"]},
            C.owner))
    s, h = req("GET", f"/contacts/{pid}/history", None, C.owner)
    cf = next((i for i in h["items"] if i["field"] == f"custom.{C.fields['NUMBER']}"), None)
    ow = next((i for i in h["items"] if i["field"] == "owner_id"), None)
    R.check("CM-026", "Custom property and owner changes are in history with readable values (BR-CM-11)",
            cf and cf["old_value"] == "42" and cf["new_value"] == "43" and cf["label"].startswith("Seats")
            and ow and ow["new_value"] == f"Bob{C.rid} Test",
            f"custom={cf} owner={ow}")


# ════════════════════════════════════════════════════════════════════
# RECORD PAGE, ACTIVITIES, TIMELINE, TASKS (BR-CM-12..16)
# ════════════════════════════════════════════════════════════════════
@case("CM-027", "Record page payload: properties, company, lists, campaigns, stats in one call (BR-CM-12)")
def _():
    s, d = req("GET", f"/contacts/{C.jane['prospect_id']}", None, C.owner)
    need = ["email", "lifecycle_stage", "lead_status", "account", "lists", "campaigns", "stats", "owner_name",
            "consent_status"]
    absent = [k for k in need if k not in d]
    R.check("CM-027", "Record page payload: properties, company, lists, campaigns, stats in one call (BR-CM-12)",
            s == 200 and not absent and d["account"] and d["account"]["contact_count"] >= 2, f"absent={absent}")


@case("CM-028", "Add note and log call, email, meeting from the record (BR-CM-13)")
def _():
    c = new_contact(C.owner, em("acts"))
    pid = c["prospect_id"]
    C.acts_contact = pid
    res = {}
    for t, extra in (("NOTE", {"body": "Met at expo"}), ("CALL", {"subject": "Call", "outcome": "No answer", "duration_minutes": 3}),
                     ("EMAIL", {"subject": "Sent deck", "body": "Deck attached"}), ("MEETING", {"subject": "Demo", "duration_minutes": 30})):
        s, b = req("POST", f"/contacts/{pid}/activities", {"activity_type": t, **extra}, C.owner)
        res[t] = s
    bad_type, _ = req("POST", f"/contacts/{pid}/activities", {"activity_type": "SMS", "body": "x"}, C.owner)
    empty, _ = req("POST", f"/contacts/{pid}/activities", {"activity_type": "NOTE"}, C.owner)
    too_long, _ = req("POST", f"/contacts/{pid}/activities", {"activity_type": "CALL", "subject": "x", "duration_minutes": 5000}, C.owner)
    s, d = req("GET", f"/contacts/{pid}", None, C.owner)
    st = d["stats"]
    R.check("CM-028", "Add note and log call, email, meeting from the record (BR-CM-13)",
            all(v == 201 for v in res.values()) and bad_type == 400 and empty == 400 and too_long == 422
            and (st["notes"], st["calls"], st["logged_emails"], st["meetings"]) == (1, 1, 1, 1),
            f"{res} bad_type={bad_type} empty={empty} too_long={too_long} stats={st}")


@case("CM-029", "Timeline shows notes, calls, meetings, tasks, property changes; filter by type and user (BR-CM-14)")
def _():
    pid = C.acts_contact
    ok(*req("POST", "/tasks", {"title": "Follow up", "prospect_id": pid}, C.owner))
    ok(*req("POST", f"/contacts/{pid}/activities", {"activity_type": "NOTE", "body": "admin note"}, C.admin["token"]))
    ok(*req("PATCH", f"/contacts/{pid}", {"designation": "CTO"}, C.owner))
    s, t = req("GET", f"/contacts/{pid}/timeline", None, C.owner)
    kinds = {i["kind"] for i in t["items"]}
    need = {"NOTE", "CALL", "MEETING", "EMAIL_LOGGED", "TASK", "PROPERTY_CHANGE"}
    s, tc = req("GET", f"/contacts/{pid}/timeline?types=CALL", None, C.owner)
    s, tu = req("GET", f"/contacts/{pid}/timeline?user_id={C.admin['user_id']}", None, C.owner)
    ats = [i["at"] for i in t["items"]]
    R.check("CM-029", "Timeline shows notes, calls, meetings, tasks, property changes; filter by type and user (BR-CM-14)",
            need <= kinds and {i["kind"] for i in tc["items"]} == {"CALL"}
            and tu["items"] and all(i.get("user_id") == C.admin["user_id"] for i in tu["items"])
            and ats == sorted(ats, reverse=True),
            f"kinds={kinds} call_filter={[i['kind'] for i in tc['items']]} user_filter={[i['kind'] for i in tu['items']]}")


@case("CM-030", "Campaign emails (sent, opened, clicked, replied) appear on the timeline (BR-CM-14, integration)")
def _():
    c = new_contact(C.owner, em("emailtl"), first_name="Mail", last_name="Target")
    pid = c["prospect_id"]
    C.email_contact = c
    out_id, in_id = str(uuid.uuid4()), str(uuid.uuid4())
    sql(f"INSERT INTO email_messages (message_id, prospect_id, to_email, from_email, direction, status, subject, sent_at, body_text) "
        f"VALUES ('{out_id}','{pid}','{c['email']}','rep@{C.dom}','OUTBOUND','SENT','Hello from systest',NOW() - INTERVAL 2 HOUR,'Hi'),"
        f"('{in_id}','{pid}','rep@{C.dom}','{c['email']}','INBOUND','RECEIVED','Re: Hello from systest',NOW() - INTERVAL 1 HOUR,'Thanks')")
    sql(f"INSERT INTO email_events (event_id, message_id, event_type, event_time) VALUES "
        f"('{uuid.uuid4()}','{out_id}',2,NOW() - INTERVAL 100 MINUTE),('{uuid.uuid4()}','{out_id}',2,NOW() - INTERVAL 99 MINUTE),"
        f"('{uuid.uuid4()}','{out_id}',3,NOW() - INTERVAL 95 MINUTE)")
    C.email_out_id = out_id
    s, t = req("GET", f"/contacts/{pid}/timeline", None, C.owner)
    kinds = [i["kind"] for i in t["items"]]
    s, d = req("GET", f"/contacts/{pid}", None, C.owner)
    R.check("CM-030", "Campaign emails (sent, opened, clicked, replied) appear on the timeline (BR-CM-14, integration)",
            {"EMAIL_SENT", "EMAIL_OPENED", "EMAIL_CLICKED", "EMAIL_RECEIVED"} <= set(kinds)
            and kinds.count("EMAIL_OPENED") == 1 and d["stats"]["emails_sent"] == 1 and d["stats"]["replies"] == 1
            and d["last_contacted_at"],
            f"kinds={kinds} stats={d['stats']}")


@case("CM-031", "Tasks with due date, reminder, owner, priority and a My-tasks queue (BR-CM-15)")
def _():
    pid = C.acts_contact
    body = {"title": "Send proposal", "priority": "HIGH", "task_type": "EMAIL", "due_at": "2020-01-01T09:00:00Z",
            "reminder_at": "2020-01-01T08:00:00Z", "owner_id": C.agent1["user_id"], "prospect_id": pid}
    s, t = req("POST", "/tasks", body, C.owner)
    ok(s, t, 201)
    s, mine = req("GET", "/tasks?scope=mine", None, C.agent1["token"])
    found = next((x for x in mine["items"] if x["task_id"] == t["task_id"]), None)
    bad_p, _ = req("POST", "/tasks", {"title": "x", "priority": "URGENT"}, C.owner)
    no_title, _ = req("POST", "/tasks", {"priority": "LOW"}, C.owner)
    done_s, done = req("PATCH", f"/tasks/{t['task_id']}", {"status": "DONE"}, C.agent1["token"])
    s, mine_after = req("GET", "/tasks?scope=mine", None, C.agent1["token"])
    s, notes = req("GET", "/notifications", None, C.agent1["token"])
    R.check("CM-031", "Tasks with due date, reminder, owner, priority and a My-tasks queue (BR-CM-15)",
            found and found["overdue"] and found["reminder_due"] and found["priority"] == "HIGH"
            and mine["my_overdue"] >= 1 and bad_p == 400 and no_title == 400 and done_s == 200 and done["completed_at"]
            and not any(x["task_id"] == t["task_id"] for x in mine_after["items"]),
            f"found={bool(found)} {found and (found['overdue'], found['reminder_due'])} bad_priority={bad_p} "
            f"no_title={no_title} done={done_s}")


@case("CM-032", "Task visibility: an agent cannot see/edit a task of another agent (BR-CM-15/38)")
def _():
    s, t = req("POST", "/tasks", {"title": "Private task"}, C.agent2["token"])
    ok(s, t, 201)
    g, _ = req("PATCH", f"/tasks/{t['task_id']}", {"title": "hijack"}, C.agent1["token"])
    s, adm = req("GET", "/tasks?scope=all", None, C.owner)
    R.check("CM-032", "Task visibility: an agent cannot see/edit a task of another agent (BR-CM-15/38)",
            g == 404 and any(x["task_id"] == t["task_id"] for x in adm["items"]), f"agent1 patch={g}")


@case("CM-033", "@mention a colleague in a note notifies them (BR-CM-16)")
def _():
    pid = C.acts_contact
    s0, before = req("GET", "/notifications", None, C.admin["token"])
    n0 = len(before.get("items", before) if isinstance(before, dict) else before or [])
    ok(*req("POST", f"/contacts/{pid}/activities",
            {"activity_type": "NOTE", "body": f"@Ada{C.rid} Test please review this account"}, C.owner))
    s1, after = req("GET", "/notifications", None, C.admin["token"])
    items = after.get("items", after) if isinstance(after, dict) else after or []
    mention = [n for n in items if "mention" in json.dumps(n).lower() or "review this account" in json.dumps(n)]
    if mention:
        R.record("CM-033", "@mention a colleague in a note notifies them (BR-CM-16)", True)
    else:
        R.record("CM-033", "@mention a colleague in a note notifies them (BR-CM-16)", None,
                 f"no notification for mentioned user (before={n0}, after={len(items)}); no mention parsing in code",
                 "not-implemented")


@case("CM-034", "Only the author or a manager can edit/delete an activity (BR-CM-13)")
def _():
    c = C.agent1_contact
    s, a = req("POST", f"/contacts/{c['prospect_id']}/activities", {"activity_type": "NOTE", "body": "agent note"},
               C.agent1["token"])
    ok(s, a, 201)
    other, _ = req("PATCH", f"/contacts/activities/{a['activity_id']}", {"body": "x"}, C.agent2["token"])
    owner_edit, _ = req("PATCH", f"/contacts/activities/{a['activity_id']}", {"body": "edited by admin"}, C.owner)
    author_del, _ = req("DELETE", f"/contacts/activities/{a['activity_id']}", None, C.agent1["token"])
    R.check("CM-034", "Only the author or a manager can edit/delete an activity (BR-CM-13)",
            other in (403, 404) and owner_edit == 200 and author_del == 200,
            f"other agent={other} admin={owner_edit} author delete={author_del}")


# ════════════════════════════════════════════════════════════════════
# VIEWS, FILTERS, BOARD, SEARCH (BR-CM-17..21)
# ════════════════════════════════════════════════════════════════════
@case("CM-035", "Contacts table: sort, paging and column chooser metadata (BR-CM-17)")
def _():
    tag = f"sort-{C.rid}"
    names = ["Charlie", "alice", "Bravo", "Delta", "Echo"]
    for n in names:
        new_contact(C.owner, em(f"sort{n.lower()}"), first_name=n, last_name="Zed", tags=[tag])
    s, asc = req("GET", "/contacts" + q({"tag": tag, "sort_by": "name", "sort_order": "asc", "page_size": 2}), None, C.owner)
    s, p3 = req("GET", "/contacts" + q({"tag": tag, "sort_by": "name", "sort_order": "asc", "page_size": 2, "page": 3}), None, C.owner)
    s, desc = req("GET", "/contacts" + q({"tag": tag, "sort_by": "name", "sort_order": "desc", "page_size": 5}), None, C.owner)
    s, meta = req("GET", "/contacts/meta", None, C.owner)
    bad, _ = req("GET", "/contacts?sort_by=password_hash", None, C.owner)
    big, _ = req("GET", "/contacts?page_size=10000", None, C.owner)
    R.check("CM-035", "Contacts table: sort, paging and column chooser metadata (BR-CM-17)",
            asc["total"] == 5 and [x["first_name"] for x in asc["items"]] == ["alice", "Bravo"]
            and [x["first_name"] for x in p3["items"]] == ["Echo"]
            and [x["first_name"] for x in desc["items"]] == ["Echo", "Delta", "Charlie", "Bravo", "alice"]
            and len(meta["columns"]) >= 20 and bad == 422 and big == 422,
            f"asc={[x['first_name'] for x in asc['items']]} p3={[x['first_name'] for x in p3['items']]} "
            f"desc={[x['first_name'] for x in desc['items']]} bad_sort={bad} big_page={big}")


@case("CM-036", "Companies table: search, sort and paging (BR-CM-17)")
def _():
    for n in ("Zeta", "Alpha", "Mu"):
        ok(*req("POST", "/accounts", {"name": f"{n} Corp {C.rid}", "domain": f"{n.lower()}-{C.rid}.org"}, C.owner))
    s, a = req("GET", "/accounts" + q({"q": f"corp {C.rid}", "sort_by": "name", "page_size": 2}), None, C.owner)
    s, b = req("GET", "/accounts" + q({"q": f"corp {C.rid}", "sort_by": "name", "page_size": 2, "page": 2}), None, C.owner)
    s, d = req("GET", "/accounts" + q({"q": f"zeta-{C.rid}"}), None, C.owner)
    R.check("CM-036", "Companies table: search, sort and paging (BR-CM-17)",
            a["total"] == 3 and [x["name"].split()[0] for x in a["items"]] == ["Alpha", "Mu"]
            and [x["name"].split()[0] for x in b["items"]] == ["Zeta"] and d["total"] == 1,
            f"p1={[x['name'] for x in a['items']]} p2={[x['name'] for x in b['items']]} domain_q={d['total']}")


@case("CM-037", "Advanced AND / OR filters on any property incl. custom and company (BR-CM-18)")
def _():
    tag = f"flt-{C.rid}"
    new_contact(C.owner, em("f1"), first_name="F1", poc_country="India", lifecycle_stage="MQL", tags=[tag],
                custom_fields={C.fields["NUMBER"]: 10})
    new_contact(C.owner, em("f2"), first_name="F2", poc_country="India", lifecycle_stage="SQL", tags=[tag],
                custom_fields={C.fields["NUMBER"]: 50})
    new_contact(C.owner, em("f3"), first_name="F3", poc_country="Germany", lifecycle_stage="MQL", tags=[tag])
    base = {"field": "tags", "operator": "is", "value": tag}

    def names(spec):
        s, b = req("GET", "/contacts" + q({"filters": spec, "page_size": 50}), None, C.owner)
        ok(s, b)
        return sorted(x["first_name"] for x in b["items"])

    f_and = {"op": "AND", "conditions": [base, {"field": "poc_country", "operator": "is", "value": "india"},
                                         {"field": "lifecycle_stage", "operator": "is", "value": "MQL"}]}
    f_or = {"op": "AND", "conditions": [base, {"op": "OR", "conditions": [
        {"field": "poc_country", "operator": "is", "value": "Germany"},
        {"field": "lifecycle_stage", "operator": "is", "value": "SQL"}]}]}
    f_num = {"op": "AND", "conditions": [base, {"field": f"custom.{C.fields['NUMBER']}", "operator": "gt", "value": 20}]}
    f_empty = {"op": "AND", "conditions": [base, {"field": f"custom.{C.fields['NUMBER']}", "operator": "is_empty"}]}
    f_company = {"op": "AND", "conditions": [base, {"field": "company.domain", "operator": "is", "value": C.dom}]}
    r = (names(f_and), names(f_or), names(f_num), names(f_empty), names(f_company))
    bad_op, _ = req("GET", "/contacts" + q({"filters": {"field": "email", "operator": "regex", "value": "x"}}), None, C.owner)
    bad_field, _ = req("GET", "/contacts" + q({"filters": {"field": "password_hash", "operator": "is", "value": "x"}}), None, C.owner)
    bad_json, _ = req("GET", "/contacts?filters=%7Bnot-json", None, C.owner)
    R.check("CM-037", "Advanced AND / OR filters on any property incl. custom and company (BR-CM-18)",
            r == (["F1"], ["F2", "F3"], ["F2"], ["F3"], ["F1", "F2", "F3"]) and bad_op == 400 and bad_field == 400
            and bad_json == 400, f"results={r} bad_op={bad_op} bad_field={bad_field} bad_json={bad_json}")


@case("CM-038", "Saved views: personal vs shared; only admins share (BR-CM-19)")
def _():
    flt = {"field": "lifecycle_stage", "operator": "is", "value": "MQL"}
    s1, pv = req("POST", "/views", {"name": f"My MQLs {C.rid}", "filters": flt, "columns": ["full_name", "email"],
                                    "sort": {"by": "name", "order": "asc"}}, C.owner)
    s2, sv = req("POST", "/views", {"name": f"Team MQLs {C.rid}", "filters": flt, "shared": True}, C.owner)
    s3, _ = req("POST", "/views", {"name": f"Agent shared {C.rid}", "shared": True}, C.agent1["token"])
    s4, av = req("POST", "/views", {"name": f"Agent own {C.rid}"}, C.agent1["token"])
    s5, _ = req("POST", "/views", {"name": "bad", "filters": {"field": "nope", "operator": "is", "value": 1}}, C.owner)
    s, agent_list = req("GET", "/views", None, C.agent1["token"])
    ids = {v["view_id"] for v in agent_list}
    e1, _ = req("PATCH", f"/views/{sv['view_id']}", {"name": "hijack"}, C.agent1["token"])
    g, _ = req("PATCH", f"/views/{pv['view_id']}", {"name": "hijack"}, C.agent1["token"])
    R.check("CM-038", "Saved views: personal vs shared; only admins share (BR-CM-19)",
            s1 == s2 == s4 == 201 and s3 == 403 and s5 == 400 and sv["view_id"] in ids and pv["view_id"] not in ids
            and av["view_id"] in ids and e1 == 403 and g == 404,
            f"create={s1},{s2},{s4} agent_share={s3} bad_filter={s5} agent_sees_shared={sv['view_id'] in ids} "
            f"agent_sees_private={pv['view_id'] in ids} agent_edit_shared={e1} agent_edit_private={g}")


@case("CM-039", "Default views: All, My contacts, Unassigned, Recently created (BR-CM-19)")
def _():
    tag = f"dv-{C.rid}"
    mine = new_contact(C.owner, em("dv.mine"), tags=[tag])
    theirs = new_contact(C.owner, em("dv.theirs"), tags=[tag], owner_id=C.admin["user_id"])
    un = new_contact(C.owner, em("dv.un"), tags=[tag])
    ok(*req("POST", "/contacts/bulk", {"prospect_ids": [un["prospect_id"]], "action": "assign_owner", "owner_id": None}, C.owner))

    def ids(**p):
        s, b = req("GET", "/contacts" + q({"tag": tag, **p}), None, C.owner)
        return {x["prospect_id"] for x in b["items"]}
    allv, me, una = ids(), ids(owner="me"), ids(owner="unassigned")
    recent = ids(filters={"field": "created_at", "operator": "in_last_days", "value": 1})
    R.check("CM-039", "Default views: All, My contacts, Unassigned, Recently created (BR-CM-19)",
            len(allv) == 3 and me == {mine["prospect_id"]} and una == {un["prospect_id"]} and len(recent) == 3,
            f"all={len(allv)} me={len(me)} unassigned={len(una)} recent={len(recent)}")


@case("CM-040", "Board view by lifecycle stage / lead status; drag-and-drop moves a card (BR-CM-20)")
def _():
    tag = f"board-{C.rid}"
    c1 = new_contact(C.owner, em("board1"), tags=[tag], lifecycle_stage="LEAD")
    new_contact(C.owner, em("board2"), tags=[tag], lifecycle_stage="MQL")
    flt = {"field": "tags", "operator": "is", "value": tag}
    s, b1 = req("GET", "/contacts/board" + q({"group_by": "lifecycle_stage", "filters": flt}), None, C.owner)
    lanes1 = {l["value"]: l["total"] for l in b1["lanes"]}
    ok(*req("PATCH", f"/contacts/{c1['prospect_id']}", {"lifecycle_stage": "SQL"}, C.owner))  # the drop
    s, b2 = req("GET", "/contacts/board" + q({"group_by": "lifecycle_stage", "filters": flt}), None, C.owner)
    lanes2 = {l["value"]: l["total"] for l in b2["lanes"]}
    s, b3 = req("GET", "/contacts/board" + q({"group_by": "lead_status", "filters": flt}), None, C.owner)
    R.check("CM-040", "Board view by lifecycle stage / lead status; drag-and-drop moves a card (BR-CM-20)",
            len(b1["lanes"]) >= 8 and lanes1.get("LEAD") == 1 and lanes1.get("MQL") == 1 and lanes2.get("LEAD") == 0
            and lanes2.get("SQL") == 1 and {l["value"] for l in b3["lanes"]} >= set(STATUSES),
            f"before={lanes1} after={lanes2}")


@case("CM-041", "Global search by name, email, phone (any format), company and domain (BR-CM-21)")
def _():
    c = new_contact(C.owner, em("searchy"), first_name="Quentin", last_name=f"Zorblax{C.rid}",
                    phone="+1 (737) 555-0188", company_name=f"Initech {C.rid}")
    pid = c["prospect_id"]
    queries = {"name": f"zorblax{C.rid}", "email": f"searchy.{C.rid}", "phone_digits": "7375550188",
               "phone_formatted": "737-555-0188", "company": f"initech {C.rid}", "domain": C.dom}
    res = {}
    for k, term in queries.items():
        s, b = req("GET", "/search" + q({"q": term, "limit": 25}), None, C.owner)
        res[k] = s == 200 and any(x["prospect_id"] == pid for x in b["contacts"])
    s, b = req("GET", "/search" + q({"q": C.dom}), None, C.owner)
    res["company_by_domain"] = any(x["domain"] == C.dom for x in b["companies"])
    s, lst = req("GET", "/contacts" + q({"q": "737 555 0188"}), None, C.owner)
    res["list_q_phone"] = any(x["prospect_id"] == pid for x in lst["items"])
    short, _ = req("GET", "/search?q=a", None, C.owner)
    R.check("CM-041", "Global search by name, email, phone (any format), company and domain (BR-CM-21)",
            all(res.values()) and short == 422, f"{res} short={short}")


@case("CM-042", "Search & list respect visibility: agent sees only own contacts (BR-CM-21/38)")
def _():
    s, b = req("GET", "/search" + q({"q": f"zorblax{C.rid}"}), None, C.agent1["token"])
    s2, l2 = req("GET", "/contacts" + q({"q": f"zorblax{C.rid}"}), None, C.agent1["token"])
    s3, own = req("GET", "/search" + q({"q": f"agentlc.{C.rid}"}), None, C.agent1["token"])
    R.check("CM-042", "Search & list respect visibility: agent sees only own contacts (BR-CM-21/38)",
            not b["contacts"] and l2["total"] == 0 and len(own["contacts"]) == 1,
            f"search={len(b['contacts'])} list={l2['total']} own={len(own['contacts'])}")


# ════════════════════════════════════════════════════════════════════
# LISTS (BR-CM-22..24)
# ════════════════════════════════════════════════════════════════════
@case("CM-043", "Active list membership updates automatically from filter criteria (BR-CM-22)")
def _():
    tag = f"al-{C.rid}"
    flt = {"op": "AND", "conditions": [{"field": "tags", "operator": "is", "value": tag},
                                       {"field": "lifecycle_stage", "operator": "is", "value": "SQL"}]}
    s, al = req("POST", "/lists", {"list_name": f"Active SQLs {C.rid}", "list_type": "ACTIVE", "filters": flt}, C.owner)
    ok(s, al, 201)
    C.active_list = al
    c = new_contact(C.owner, em("al1"), tags=[tag], first_name="Active")
    new_contact(C.owner, em("al2"), tags=[tag], lifecycle_stage="SQL", first_name="Active2")
    s, l0 = req("GET", f"/lists/{al['list_id']}", None, C.owner)
    t0 = time.time()
    ok(*req("PATCH", f"/contacts/{c['prospect_id']}", {"lifecycle_stage": "SQL"}, C.owner))
    s, l1 = req("GET", f"/lists/{al['list_id']}", None, C.owner)
    lag = time.time() - t0
    s, members = req("GET", "/contacts" + q({"list_id": al["list_id"]}), None, C.owner)
    s, d = req("GET", f"/contacts/{c['prospect_id']}", None, C.owner)
    in_detail = any(x["list_id"] == al["list_id"] for x in d["lists"])
    ok(*req("PATCH", f"/contacts/{c['prospect_id']}", {"lifecycle_stage": "LEAD"}, C.owner))  # admin moves back
    s, l2 = req("GET", f"/lists/{al['list_id']}", None, C.owner)
    R.check("CM-043", "Active list membership updates automatically from filter criteria (BR-CM-22)",
            l0["member_count"] == 1 and l1["member_count"] == 2 and members["total"] == 2 and in_detail
            and l2["member_count"] == 1 and lag < 300,
            f"before={l0['member_count']} after={l1['member_count']} lag={lag:.2f}s detail={in_detail} back={l2['member_count']}")


@case("CM-044", "Active list rejects manual members and empty criteria (BR-CM-22)")
def _():
    s1, _ = req("POST", f"/lists/{C.active_list['list_id']}/members", {"prospect_ids": [C.jane["prospect_id"]]}, C.owner)
    s2, _ = req("POST", "/lists", {"list_name": f"Empty active {C.rid}", "list_type": "ACTIVE", "filters": {}}, C.owner)
    s3, _ = req("POST", "/lists", {"list_name": f"Agent list {C.rid}"}, C.agent1["token"])
    s4, _ = req("POST", "/lists", {"list_name": " "}, C.owner)
    R.check("CM-044", "Active list rejects manual members and empty criteria (BR-CM-22)",
            s1 == 400 and s2 == 400 and s3 == 403 and s4 == 400, f"manual_add={s1} empty={s2} agent={s3} blank={s4}")


@case("CM-045", "Static list holds a fixed set; add/remove; deleted contacts excluded (BR-CM-23)")
def _():
    s, sl = req("POST", "/lists", {"list_name": f"Static {C.rid}", "list_type": "STATIC"}, C.owner)
    ok(s, sl, 201)
    C.static_list = sl
    cs = [new_contact(C.owner, em(f"st{i}"), first_name=f"St{i}") for i in range(4)]
    C.static_contacts = cs
    ids = [c["prospect_id"] for c in cs]
    s, add = req("POST", f"/lists/{sl['list_id']}/members", {"prospect_ids": ids + [str(uuid.uuid4())]}, C.owner)
    s, again = req("POST", f"/lists/{sl['list_id']}/members", {"prospect_ids": ids[:1]}, C.owner)
    s, rem = req("POST", f"/lists/{sl['list_id']}/members/remove", {"prospect_ids": ids[3:]}, C.owner)
    ok(*req("PATCH", f"/contacts/{ids[0]}", {"lifecycle_stage": "CUSTOMER"}, C.owner))  # no effect on static membership
    ok(*req("DELETE", f"/contacts/{ids[2]}", None, C.owner))
    s, after = req("GET", f"/lists/{sl['list_id']}", None, C.owner)
    s, members = req("GET", "/contacts" + q({"list_id": sl["list_id"]}), None, C.owner)
    R.check("CM-045", "Static list holds a fixed set; add/remove; deleted contacts excluded (BR-CM-23)",
            add["added"] == 4 and add["not_found"] == 1 and again["already_in_list"] == 1 and rem["removed"] == 1
            and after["member_count"] == 2 and members["total"] == 2,
            f"add={add} again={again} rem={rem} count={after['member_count']} members={members['total']}")


@case("CM-046", "Static and active lists can be used as a campaign audience (BR-CM-24, integration)")
def _():
    s, camp = req("POST", "/campaigns", {"campaign_name": f"CM audience {C.rid}"}, C.owner)
    ok(s, camp, 201)
    C.campaign = camp
    # unsubscribe one static member: must be rejected, not enrolled
    unsub_id = C.static_contacts[1]["prospect_id"]
    ok(*req("PATCH", f"/contacts/{unsub_id}", {"consent_status": "UNSUBSCRIBED"}, C.owner))
    s, r = req("POST", f"/campaigns/{camp['campaign_id']}/prospects",
               {"list_ids": [C.static_list["list_id"], C.active_list["list_id"]]}, C.owner)
    ok(s, r)
    enrolled = set(sql(f"SELECT prospect_id FROM campaign_prospects WHERE campaign_id='{camp['campaign_id']}'").split())
    s, al_members = req("GET", "/contacts" + q({"list_id": C.active_list["list_id"]}), None, C.owner)
    expected = {C.static_contacts[0]["prospect_id"]} | {x["prospect_id"] for x in al_members["items"]}
    deleted_id = C.static_contacts[2]["prospect_id"]
    # the campaign wizard's audience picker (LeadListTab) reads /prospect-lists
    s, picker = req("GET", "/prospect-lists?page_size=100", None, C.owner)
    listed = {x["list_id"]: x["prospect_count"] for x in picker["items"]}
    s_old, _ = req("GET", "/campaigns/lists", None, C.owner)
    R.check("CM-046b", "Campaign wizard list endpoint GET /campaigns/lists is reachable (BR-CM-24)", s_old == 200,
            f"GET /campaigns/lists -> {s_old} 'Campaign not found' (shadowed by GET /campaigns/{{campaign_id}})")
    R.check("CM-046", "Static and active lists can be used as a campaign audience (BR-CM-24, integration)",
            enrolled == expected and unsub_id not in enrolled and deleted_id not in enrolled
            and {C.static_list["list_id"], C.active_list["list_id"]} <= set(listed)
            and listed.get(C.active_list["list_id"]) == len(al_members["items"]),
            f"enrolled={len(enrolled)} expected={len(expected)} unsub_enrolled={unsub_id in enrolled} "
            f"deleted_enrolled={deleted_id in enrolled} resp={str(r)[:200]} lists_in_picker={len(listed)}")


# ════════════════════════════════════════════════════════════════════
# IMPORT & EXPORT (BR-CM-25..31)
# ════════════════════════════════════════════════════════════════════
IMPORT_HEADER = ["Email", "First Name", "Last Name", "Job Title", "Phone", "Company", "Company Domain", "Industry",
                 "Lifecycle Stage", "Lead Status", "Tags", "Seats", "Owner Email"]


def journey_rows():
    r = C.rid
    rows = []
    for i in range(6):
        rows.append([f"p{i}.{r}@wayne-{r}.com", f"Person{i}", "Wayne", "Engineer", f"+1 415 555 01{i:02d}",
                     f"Wayne Enterprises {r}", f"wayne-{r}.com", "Manufacturing", "Lead", "New", f"imp-{r};wave1", str(i),
                     C.agent1["email"] if i % 2 else ""])
    for i in range(6):
        rows.append([f"q{i}.{r}@stark-{r}.com", f"Q{i}", "Stark", "Analyst", "", f"Stark Industries {r}",
                     "", "Defense", "MQL", "Open", f"imp-{r}", "", ""])
    for i in range(3):  # company derived from email domain only
        rows.append([f"u{i}.{r}@umbrella-{r}.net", f"U{i}", "Umbrella", "", "", "", "", "", "", "", f"imp-{r}", "", ""])
    rows += [  # rejected rows
        ["not-an-email", "Bad", "Email", "", "", "", "", "", "", "", "", "", ""],
        [f"p0.{r}@WAYNE-{r}.com", "Dup", "InFile", "", "", "", "", "", "", "", "", "", ""],
        [f"bad.stage.{r}@wayne-{r}.com", "Bad", "Stage", "", "", "", "", "", "Prospect", "", "", "", ""],
        [f"bad.owner.{r}@wayne-{r}.com", "Bad", "Owner", "", "", "", "", "", "", "", "", "", "nobody@nowhere.example"],
        [f"bad.seats.{r}@wayne-{r}.com", "Bad", "Seats", "", "", "", "", "", "", "", "", "many", ""],
    ]
    return rows


@case("CM-047", "Import preview suggests a column mapping (BR-CM-26)")
def _():
    s, p = req("POST", "/imports/preview", files={"file": ("journey.csv", csv_bytes(IMPORT_HEADER, journey_rows()))},
               token=C.owner)
    sm = p.get("suggested_mapping", {}) if isinstance(p, dict) else {}
    R.check("CM-047", "Import preview suggests a column mapping (BR-CM-26)",
            s == 200 and p["row_count"] == 20 and sm.get("Email") == "contact.email" and sm.get("Company") == "company.name"
            and sm.get("Company Domain") == "company.domain" and sm.get("Job Title") == "contact.designation"
            and sm.get("Owner Email") == "contact.owner_email" and len(p["sample"]) == 5,
            f"{s} {sm}")


@case("CM-048", "Import contacts + companies in one CSV with associations, new property, rejected-row reasons (BR-CM-25/26/29)")
def _():
    mapping = {"Email": "contact.email", "First Name": "contact.first_name", "Last Name": "contact.last_name",
               "Job Title": "contact.designation", "Phone": "contact.phone", "Company": "company.name",
               "Company Domain": "company.domain", "Industry": "company.industry", "Lifecycle Stage": "contact.lifecycle_stage",
               "Lead Status": "contact.lead_status", "Tags": "contact.tags", "Seats": f"new:NUMBER:Imported Seats {C.rid}",
               "Owner Email": "contact.owner_email"}
    st, job, secs = run_import(C.owner, csv_bytes(IMPORT_HEADER, journey_rows()), mapping, fname="journey.csv")
    C.journey_job = job
    ok(st, job, 201)
    reasons = {e["row"]: e["reason"] for e in job["errors"]}
    wayne = sql(f"SELECT account_id FROM accounts WHERE tenant_id='{W.tenant_id}' AND domain='wayne-{C.rid}.com'")
    n_wayne = count(f"SELECT COUNT(*) FROM prospects WHERE account_id='{wayne}'") if wayne else 0
    stark = sql(f"SELECT account_id FROM accounts WHERE tenant_id='{W.tenant_id}' AND name='Stark Industries {C.rid}'")
    n_stark = count(f"SELECT COUNT(*) FROM prospects WHERE account_id='{stark}'") if stark else 0
    umb = count(f"SELECT COUNT(*) FROM prospects p JOIN accounts a ON a.account_id=p.account_id "
                f"WHERE p.tenant_id='{W.tenant_id}' AND a.domain='umbrella-{C.rid}.net'")
    s, fields = req("GET", "/contacts/fields", None, C.owner)
    seats = next((f for f in fields if f["label"] == f"Imported Seats {C.rid}"), None)
    C.seats_key = seats and seats["field_key"]
    s, p3 = req("GET", "/contacts" + q({"q": f"p3.{C.rid}@wayne"}), None, C.owner)
    p3c = p3["items"][0] if p3["items"] else {}
    R.check("CM-048", "Import contacts + companies in one CSV with associations, new property, rejected-row reasons (BR-CM-25/26/29)",
            job["status"] == "COMPLETED" and job["contacts_created"] == 15 and job["companies_created"] == 3
            and job["error_count"] == 5 and n_wayne == 6 and n_stark == 6 and umb == 3 and seats
            and seats["field_type"] == "NUMBER" and p3c.get("custom_fields", {}).get(C.seats_key) == 3
            and p3c.get("owner_id") == C.agent1["user_id"] and p3c.get("lifecycle_stage") == "LEAD"
            and sorted(p3c.get("tags", [])) == sorted([f"imp-{C.rid}", "wave1"]) and len(reasons) == 5,
            f"status={job['status']} created={job['contacts_created']} companies={job['companies_created']} errors={reasons} "
            f"assoc wayne={n_wayne} stark={n_stark} umbrella={umb} seats={seats} p3={ {k: p3c.get(k) for k in ('owner_id', 'custom_fields', 'tags')} } "
            f"secs={secs:.2f}")


@case("CM-049", "Import history and downloadable error file per import (BR-CM-29)")
def _():
    job = C.journey_job
    s, hist = req("GET", "/imports", None, C.owner)
    s2, det = req("GET", f"/imports/{job['import_id']}", None, C.owner)
    s3, raw = req("GET", f"/imports/{job['import_id']}/errors.csv", None, C.owner, raw=True)
    rows = parse_csv(raw) if s3 == 200 else []
    s4, _ = req("GET", f"/imports/{job['import_id']}", None, C.agent1["token"])
    s5, _ = req("GET", f"/imports/{job['import_id']}/errors.csv", None, W2.token)
    reasons = " | ".join(r[1] for r in rows[1:])
    R.check("CM-049", "Import history and downloadable error file per import (BR-CM-29)",
            any(h["import_id"] == job["import_id"] for h in hist["items"]) and det["error_count"] == 5
            and rows and rows[0][:2] == ["Row", "Error"] and len(rows) == 6 and "Email" in rows[0]
            and "Duplicate of row" in reasons and "Lifecycle stage" in reasons and "Owner" in reasons
            and "number" in reasons and s4 == 404 and s5 == 404,
            f"hist={s} detail={s2} csv={s3} rows={len(rows)} header={rows[0] if rows else None} reasons={reasons} "
            f"agent={s4} other_tenant={s5}")


@case("CM-050", "Re-import updates existing contacts matched by email (case-insensitive) and companies by domain (BR-CM-27)")
def _():
    r = C.rid
    before = count(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{W.tenant_id}'")
    acc_before = count(f"SELECT COUNT(*) FROM accounts WHERE tenant_id='{W.tenant_id}'")
    rows = [[f"P{i}.{r}@WAYNE-{r}.COM", "Senior Engineer", f"wayne-{r}.com", "1000+"] for i in range(6)]
    st, job, _ = run_import(C.owner, csv_bytes(["Email", "Job Title", "Website", "Company employees"], rows),
                            {"Email": "contact.email", "Job Title": "contact.designation", "Website": "company.website",
                             "Company employees": "company.emp_band"})
    after = count(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{W.tenant_id}'")
    acc_after = count(f"SELECT COUNT(*) FROM accounts WHERE tenant_id='{W.tenant_id}'")
    s, p = req("GET", "/contacts" + q({"q": f"p2.{r}@wayne"}), None, C.owner)
    pc = p["items"][0]
    s, h = req("GET", f"/contacts/{pc['prospect_id']}/history?field=designation", None, C.owner)
    emp = sql(f"SELECT emp_band FROM accounts WHERE tenant_id='{W.tenant_id}' AND domain='wayne-{r}.com'")
    R.check("CM-050", "Re-import updates existing contacts matched by email (case-insensitive) and companies by domain (BR-CM-27)",
            st == 201 and job["contacts_created"] == 0 and job["contacts_updated"] == 6 and before == after
            and acc_before == acc_after and pc["designation"] == "Senior Engineer" and emp == "1000+"
            and h["items"] and h["items"][0]["source"] == "IMPORT",
            f"st={st} created={job.get('contacts_created')} updated={job.get('contacts_updated')} rows {before}->{after} "
            f"accounts {acc_before}->{acc_after} designation={pc['designation']} emp={emp} hist={h['items'][:1]}")


@case("CM-051", "Import with update_existing=false only fills blanks (BR-CM-27)")
def _():
    r = C.rid
    rows = [[f"p1.{r}@wayne-{r}.com", "Overwritten Title", "https://linkedin.com/in/p1"]]
    st, job, _ = run_import(C.owner, csv_bytes(["Email", "Job Title", "LinkedIn"], rows),
                            {"Email": "contact.email", "Job Title": "contact.designation", "LinkedIn": "contact.linkedin_url"},
                            {"update_existing": False})
    s, p = req("GET", "/contacts" + q({"q": f"p1.{r}@wayne"}), None, C.owner)
    pc = p["items"][0]
    R.check("CM-051", "Import with update_existing=false only fills blanks (BR-CM-27)",
            st == 201 and pc["designation"] == "Senior Engineer" and pc["linkedin_url"] == "https://linkedin.com/in/p1",
            f"designation={pc['designation']} linkedin={pc['linkedin_url']}")


@case("CM-052", "Import sets owner and adds contacts to a new static list (BR-CM-30)")
def _():
    r = C.rid
    rows = [[f"own{i}.{r}@cyberdyne-{r}.ai", f"Own{i}"] for i in range(5)]
    st, job, _ = run_import(C.owner, csv_bytes(["Email", "First Name"], rows),
                            {"Email": "contact.email", "First Name": "contact.first_name"},
                            {"owner_id": C.agent2["user_id"], "new_list_name": f"Imported list {r}"})
    lid = (job.get("options") or {}).get("list_id")
    s, lst = req("GET", f"/lists/{lid}", None, C.owner) if lid else (0, {})
    owners = set(sql(f"SELECT DISTINCT owner_id FROM prospects WHERE tenant_id='{W.tenant_id}' AND email LIKE 'own%.{r}@cyberdyne-{r}.ai'").split())
    s2, bad = req("POST", "/imports", files={"file": ("x.csv", csv_bytes(["Email"], [["a@b.co"]]))},
                  form={"mapping": json.dumps({"Email": "contact.email"}), "options": json.dumps({"owner_id": W2.user_id})},
                  token=C.owner)
    R.check("CM-052", "Import sets owner and adds contacts to a new static list (BR-CM-30)",
            st == 201 and lst.get("list_type") == "STATIC" and lst.get("member_count") == 5
            and owners == {C.agent2["user_id"]} and s2 == 400,
            f"st={st} list={lst.get('list_type')}/{lst.get('member_count')} owners={owners} foreign_owner={s2}")


@case("CM-053", "Import an XLSX file (BR-CM-25)")
def _():
    r = C.rid
    content = xlsx_bytes(["Email", "First Name", "Company"], [[f"x{i}.{r}@oscorp-{r}.com", f"X{i}", f"Oscorp {r}"] for i in range(4)])
    s, prev = req("POST", "/imports/preview", files={"file": ("book.xlsx", content)}, token=C.owner)
    st, job, _ = run_import(C.owner, content, {"Email": "contact.email", "First Name": "contact.first_name",
                                               "Company": "company.name"}, fname="book.xlsx")
    R.check("CM-053", "Import an XLSX file (BR-CM-25)",
            s == 200 and prev.get("row_count") == 4 and st == 201 and job["contacts_created"] == 4 and job["companies_created"] == 1,
            f"preview={s} {str(prev)[:150]} import={st} {str(job)[:200]}")


@case("CM-054", "Companies-only import creates/updates companies (BR-CM-25)")
def _():
    r = C.rid
    rows = [[f"Tyrell {r}", f"tyrell-{r}.com", "Biotech", "5,000,000"], [f"Soylent {r}", f"soylent-{r}.com", "Food", ""],
            [f"Globex {r}", f"globex-{r}.com", "Software & AI", ""], ["", "", "Nothing", ""]]
    st, job, _ = run_import(C.owner, csv_bytes(["Company", "Domain", "Industry", "Annual revenue"], rows),
                            {"Company": "company.name", "Domain": "company.domain", "Industry": "company.industry",
                             "Annual revenue": "company.annual_revenue"})
    ind = sql(f"SELECT industry FROM accounts WHERE tenant_id='{W.tenant_id}' AND domain='globex-{r}.com'")
    R.check("CM-054", "Companies-only import creates/updates companies (BR-CM-25)",
            st == 201 and job["companies_created"] == 2 and job["companies_updated"] == 1 and job["error_count"] == 1
            and ind == "Software & AI",
            f"st={st} created={job.get('companies_created')} updated={job.get('companies_updated')} errors={job.get('errors')} industry={ind}")


@case("CM-055", "Import validation: no email/company column, bad mapping, agent, >10k rows (BR-CM-25/26)")
def _():
    f = csv_bytes(["Email", "Name"], [["a@b.co", "A"]])
    s1, b1, _ = run_import(C.owner, f, {"Name": "contact.first_name"})
    s2, b2, _ = run_import(C.owner, f, {"Email": "contact.password"})
    s3, b3, _ = run_import(C.agent1["token"], f, {"Email": "contact.email"})
    big = csv_bytes(["Email"], [[f"big{i}.{C.rid}@bigco-{C.rid}.com"] for i in range(10001)])
    s4, b4, _ = run_import(C.owner, big, {"Email": "contact.email"})
    s5, b5, _ = run_import(C.owner, b"\x00\x01garbage", {"Email": "contact.email"}, fname="x.xlsx")
    R.check("CM-055", "Import validation: no email/company column, bad mapping, agent, >10k rows (BR-CM-25/26)",
            s1 == 400 and s2 == 400 and s3 == 403 and s4 == 400 and "10,000" in str(b4) and s5 == 400,
            f"no_key={s1} bad_target={s2} agent={s3} 10001rows={s4} {str(b4)[:100]} garbage={s5}")


@case("CM-056", "Simultaneous imports by several users never create duplicate contacts/companies (BR-CM-28)")
def _():
    r = C.rid
    emails = [f"conc{i}.{r}@conc{i % 7}-{r}.com" for i in range(300)]
    users = [C.owner, C.admin["token"], C.agent_mp["token"]]
    results = [None] * len(users)

    def go(i):
        order = emails[i * 100:] + emails[:i * 100]
        results[i] = run_import(users[i], csv_bytes(["Email", "First Name"], [[e, f"U{i}"] for e in order]),
                                {"Email": "contact.email", "First Name": "contact.first_name"}, fname=f"conc{i}.csv")
    ths = [threading.Thread(target=go, args=(i,)) for i in range(len(users))]
    [t.start() for t in ths]
    [t.join(600) for t in ths]
    rows = count(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{W.tenant_id}' AND email LIKE 'conc%.{r}@conc%'")
    distinct = count(f"SELECT COUNT(DISTINCT LOWER(email)) FROM prospects WHERE tenant_id='{W.tenant_id}' AND email LIKE 'conc%.{r}@conc%'")
    accs = count(f"SELECT COUNT(*) FROM accounts WHERE tenant_id='{W.tenant_id}' AND domain LIKE 'conc_-{r}.com'")
    created = sum((x[1].get("contacts_created", 0) if x and isinstance(x[1], dict) else 0) for x in results)
    statuses = [x and x[0] for x in results]
    errs = [x[1].get("error_count") if x and isinstance(x[1], dict) else str(x)[:100] for x in results]
    R.check("CM-056", "Simultaneous imports by several users never create duplicate contacts/companies (BR-CM-28)",
            statuses == [201, 201, 201] and rows == 300 and distinct == 300 and accs == 7 and created == 300
            and errs == [0, 0, 0],
            f"statuses={statuses} rows={rows} distinct={distinct} companies={accs} created_sum={created} errors={errs} "
            f"secs={[round(x[2], 1) for x in results if x]}")


@case("CM-057", "Import → property history → search index (integration)")
def _():
    r = C.rid
    st, job, _ = run_import(C.owner, csv_bytes(["Email", "Name", "Mobile"], [[f"idx.{r}@hooli-{r}.com", f"Gavin Belson{r}", "+1 650 555 0177"]]),
                            {"Email": "contact.email", "Name": "contact.full_name", "Mobile": "contact.mobile_phone"})
    s, sr = req("GET", "/search" + q({"q": "6505550177"}), None, C.owner)
    hit = next((x for x in sr["contacts"] if x["email"] == f"idx.{r}@hooli-{r}.com"), None)
    s, sn = req("GET", "/search" + q({"q": f"belson{r}"}), None, C.owner)
    s, h = req("GET", f"/contacts/{hit['prospect_id']}/history", None, C.owner) if hit else (0, {"items": []})
    created = [i for i in h["items"] if i["field"] == "created"]
    R.check("CM-057", "Import → property history → search index (integration)",
            st == 201 and hit and sn["contacts"] and sn["contacts"][0]["full_name"] == f"Gavin Belson{r}"
            and created and created[0]["source"] == "IMPORT",
            f"phone_hit={bool(hit)} name_hit={len(sn['contacts'])} history={created}")


@case("CM-058", "Export a filtered view to CSV with chosen columns (BR-CM-31)")
def _():
    tag = f"imp-{C.rid}"
    s, lst = req("GET", "/contacts" + q({"tag": tag, "lifecycle_stage": "MQL", "page_size": 200}), None, C.owner)
    s, raw = req("GET", "/contacts/export" + q({"format": "csv", "tag": tag, "lifecycle_stage": "MQL",
                                                 "columns": "full_name,email,lifecycle_stage,company_name"}), None, C.owner, raw=True)
    rows = parse_csv(raw) if s == 200 else []
    hdr = rows[0] if rows else []
    emails = {r[1] for r in rows[1:]}
    R.check("CM-058", "Export a filtered view to CSV with chosen columns (BR-CM-31)",
            s == 200 and hdr[:4] == ["Name", "Email", "Lifecycle stage", "Company"] and len(rows) - 1 == lst["total"] == 6
            and emails == {x["email"] for x in lst["items"]} and all(r[2] == "Marketing qualified lead" for r in rows[1:]),
            f"status={s} header={hdr} rows={len(rows) - 1} list_total={lst['total']}")


@case("CM-059", "Export to XLSX (BR-CM-31)")
def _():
    s, raw = req("GET", "/contacts/export" + q({"format": "xlsx", "tag": f"imp-{C.rid}"}), None, C.owner, raw=True)
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        n_rows = sheet.count("<row ")
    except Exception as exc:  # noqa: BLE001
        n_rows = f"bad xlsx: {exc}"
    s2, lst = req("GET", "/contacts" + q({"tag": f"imp-{C.rid}"}), None, C.owner)
    R.check("CM-059", "Export to XLSX (BR-CM-31)", s == 200 and n_rows == lst["total"] + 1,
            f"status={s} rows_incl_header={n_rows} view_total={lst['total']}")


@case("CM-060", "Export restricted to admins and managers (BR-CM-31)")
def _():
    a, _ = req("GET", "/contacts/export", None, C.agent1["token"], raw=True)
    m, mb = req("GET", "/contacts/export", None, C.manager["token"], raw=True)
    ad, _ = req("GET", "/contacts/export", None, C.admin["token"], raw=True)
    R.check("CM-060", "Export restricted to admins and managers (BR-CM-31)",
            a == 403 and ad == 200 and m == 200,
            f"agent={a} admin={ad} manager(default perms)={m} {mb[:120] if isinstance(mb, bytes) else mb}")


@case("CM-061", "Export neutralises spreadsheet formula injection (BR-CM-31, security)")
def _():
    c = new_contact(C.owner, em("formula"), first_name="=HYPERLINK(\"http://evil\")", last_name="+cmd", tags=[f"fx-{C.rid}"])
    s, raw = req("GET", "/contacts/export" + q({"tag": f"fx-{C.rid}", "columns": "full_name,email"}), None, C.owner, raw=True)
    rows = parse_csv(raw)
    R.check("CM-061", "Export neutralises spreadsheet formula injection (BR-CM-31, security)",
            s == 200 and rows[1][0].startswith("'="), f"{rows[1] if len(rows) > 1 else rows}")


# ════════════════════════════════════════════════════════════════════
# DATA QUALITY (BR-CM-32..35)
# ════════════════════════════════════════════════════════════════════
@case("CM-062", "Duplicate detection: same email variants and similar name at same company (BR-CM-32)")
def _():
    r = C.rid
    a = new_contact(C.owner, f"john.smith.{r}@gmail.com", first_name="John", last_name="Smith")
    b = new_contact(C.owner, f"johnsmith.{r}@gmail.com", first_name="Johnny", last_name="Smith")
    c = new_contact(C.owner, f"robert.brown@dupco-{r}.com", first_name="Robert", last_name="Brown")
    d = new_contact(C.owner, f"rob.b@dupco-{r}.com", first_name="Rob", last_name="Brown")
    s, dup = req("GET", "/contacts/duplicates?limit=200", None, C.owner)
    groups = [{x["prospect_id"] for x in g["contacts"]} | {"reasons:" + "|".join(g["reasons"])} for g in dup["groups"]]
    g1 = next((g for g in groups if a["prospect_id"] in g), set())
    g2 = next((g for g in groups if c["prospect_id"] in g), set())
    s2, _ = req("GET", "/contacts/duplicates", None, C.agent1["token"])
    C.dups = (a, b, c, d)
    R.check("CM-062", "Duplicate detection: same email variants and similar name at same company (BR-CM-32)",
            b["prospect_id"] in g1 and d["prospect_id"] in g2 and s2 == 403,
            f"email_group={b['prospect_id'] in g1} name_group={d['prospect_id'] in g2} reasons={[x for x in g1 | g2 if x.startswith('reasons')]} agent={s2}")


@case("CM-063", "Merge keeps chosen values, combines timeline, tasks, lists, tags, history (BR-CM-33, integration)")
def _():
    a, b, _, _ = C.dups
    pa, pb = a["prospect_id"], b["prospect_id"]
    ok(*req("PATCH", f"/contacts/{pb}", {"designation": "Director", "phone": "+1 212 555 0101", "tags": ["dup-b"]}, C.owner))
    ok(*req("PATCH", f"/contacts/{pa}", {"designation": "Manager", "tags": ["dup-a"]}, C.owner))
    ok(*req("POST", f"/contacts/{pb}/activities", {"activity_type": "CALL", "subject": "dup call"}, C.owner))
    ok(*req("POST", f"/contacts/{pa}/activities", {"activity_type": "NOTE", "body": "primary note"}, C.owner))
    s, t = req("POST", "/tasks", {"title": "dup task", "prospect_id": pb}, C.owner)
    ok(*req("POST", f"/lists/{C.static_list['list_id']}/members", {"prospect_ids": [pb]}, C.owner))
    ok(*req("POST", f"/campaigns/{C.campaign['campaign_id']}/prospects", {"prospect_ids": [pb]}, C.owner))
    s, m = req("POST", "/contacts/merge", {"primary_id": pa, "duplicate_ids": [pb], "choices": {"designation": pb}}, C.owner)
    ok(s, m)
    s, d = req("GET", f"/contacts/{pa}", None, C.owner)
    gone, _ = req("GET", f"/contacts/{pb}", None, C.owner)
    s, tl = req("GET", f"/contacts/{pa}/timeline", None, C.owner)
    kinds = [i["kind"] for i in tl["items"]]
    task_owner = sql(f"SELECT prospect_id FROM crm_tasks WHERE task_id='{t['task_id']}'")
    orphan_hist = count(f"SELECT COUNT(*) FROM property_changes WHERE object_id='{pb}'")
    R.check("CM-063", "Merge keeps chosen values, combines timeline, tasks, lists, tags, history (BR-CM-33, integration)",
            d["designation"] == "Director" and d["phone"] == "+1 212 555 0101" and sorted(d["tags"]) == ["dup-a", "dup-b"]
            and gone == 404 and "CALL" in kinds and "NOTE" in kinds and task_owner == pa
            and any(x["list_id"] == C.static_list["list_id"] for x in d["lists"])
            and any(x["campaign_id"] == C.campaign["campaign_id"] for x in d["campaigns"]) and orphan_hist == 0,
            f"designation={d['designation']} phone={d['phone']} tags={d['tags']} dup_get={gone} kinds={kinds} "
            f"task->{task_owner == pa} lists={[x['list_name'] for x in d['lists']]} campaigns={len(d['campaigns'])} orphan_hist={orphan_hist}")


@case("CM-064", "Merge can keep the duplicate's email; agent cannot merge; self-merge rejected (BR-CM-33)")
def _():
    _, _, c, d = C.dups
    s0, _ = req("POST", "/contacts/merge", {"primary_id": c["prospect_id"], "duplicate_ids": [c["prospect_id"]]}, C.owner)
    s1, _ = req("POST", "/contacts/merge", {"primary_id": c["prospect_id"], "duplicate_ids": [d["prospect_id"]]}, C.agent1["token"])
    s2, m = req("POST", "/contacts/merge", {"primary_id": c["prospect_id"], "duplicate_ids": [d["prospect_id"]],
                                            "choices": {"email": d["prospect_id"]}}, C.owner)
    email = m.get("contact", {}).get("email") if isinstance(m, dict) else None
    s3, _ = req("POST", "/contacts", {"email": c["email"]}, C.owner)  # old address is free again
    R.check("CM-064", "Merge can keep the duplicate's email; agent cannot merge; self-merge rejected (BR-CM-33)",
            s0 == 400 and s1 == 403 and s2 == 200 and email == d["email"] and s3 == 201,
            f"self={s0} agent={s1} merge={s2} email={email} recreate_old_email={s3}")


@case("CM-065", "Data-quality flags: phone format and name capitalisation (BR-CM-34)")
def _():
    c = new_contact(C.owner, em("quality"), first_name="john", last_name="SMITHSON", phone="12ab")
    good = new_contact(C.owner, em("quality.ok"), first_name="Mary", last_name="O'Neil", phone="+44 20 7946 0958")
    flags = c["quality_flags"]
    R.check("CM-065", "Data-quality flags: phone format and name capitalisation (BR-CM-34)",
            any("Phone" in f for f in flags) and any("First name" in f for f in flags) and any("Last name" in f for f in flags)
            and good["quality_flags"] == [], f"bad={flags} good={good['quality_flags']}")


@case("CM-066", "Soft delete hides the contact everywhere; restore within 90 days (BR-CM-35)")
def _():
    c = new_contact(C.owner, em("restoreme"), first_name="Restore", last_name=f"Me{C.rid}")
    pid = c["prospect_id"]
    s, dl = req("DELETE", f"/contacts/{pid}", None, C.owner)
    g, _ = req("GET", f"/contacts/{pid}", None, C.owner)
    s, srch = req("GET", "/search" + q({"q": f"me{C.rid}"}), None, C.owner)
    s, lst = req("GET", "/contacts" + q({"q": f"restoreme.{C.rid}"}), None, C.owner)
    s, deleted = req("GET", "/contacts/deleted" + q({"q": f"restoreme.{C.rid}"}), None, C.owner)
    item = deleted["items"][0] if deleted["items"] else {}
    dup, dupb = req("POST", "/contacts", {"email": c["email"]}, C.owner)
    s, rs = req("POST", "/contacts/restore", {"prospect_ids": [pid]}, C.owner)
    g2, _ = req("GET", f"/contacts/{pid}", None, C.owner)
    s, h = req("GET", f"/contacts/{pid}/history?field=deleted", None, C.owner)
    R.check("CM-066", "Soft delete hides the contact everywhere; restore within 90 days (BR-CM-35)",
            dl["restorable_days"] == 90 and g == 404 and not any(x["prospect_id"] == pid for x in srch["contacts"])
            and lst["total"] == 0 and item.get("purge_at") and item.get("deleted_by_name") and dup == 409
            and dupb["detail"].get("deleted") is True and rs["restored"] == 1 and g2 == 200 and len(h["items"]) == 2,
            f"get_after_delete={g} in_deleted={bool(item)} recreate={dup} restored={rs} get_after_restore={g2} hist={len(h['items'])}")


@case("CM-067", "Delete/restore permissions: agents cannot delete or see the recycle bin (BR-CM-35/38)")
def _():
    own = C.agent1_contact["prospect_id"]
    d1, _ = req("DELETE", f"/contacts/{own}", None, C.agent1["token"])
    d2, _ = req("POST", "/contacts/bulk", {"prospect_ids": [own], "action": "delete"}, C.agent1["token"])
    d3, _ = req("GET", "/contacts/deleted", None, C.agent1["token"])
    d4, _ = req("POST", "/contacts/restore", {"prospect_ids": [own]}, C.agent1["token"])
    R.check("CM-067", "Delete/restore permissions: agents cannot delete or see the recycle bin (BR-CM-35/38)",
            d1 == 403 and d2 == 403 and d3 == 403 and d4 == 403, f"delete={d1} bulk={d2} bin={d3} restore={d4}")


@case("CM-068", "After 90 days: no restore; purge job removes contact and dependents (BR-CM-35, integration)")
def _():
    c = new_contact(C.owner, em("purgeme"), first_name="Purge")
    pid = c["prospect_id"]
    ok(*req("POST", f"/contacts/{pid}/activities", {"activity_type": "NOTE", "body": "to be purged"}, C.owner))
    ok(*req("POST", "/tasks", {"title": "purge task", "prospect_id": pid}, C.owner))
    ok(*req("POST", f"/lists/{C.static_list['list_id']}/members", {"prospect_ids": [pid]}, C.owner))
    ok(*req("DELETE", f"/contacts/{pid}", None, C.owner))
    sql(f"UPDATE prospects SET deleted_at = NOW() - INTERVAL 91 DAY WHERE prospect_id='{pid}' AND tenant_id='{W.tenant_id}'")
    s, deleted = req("GET", "/contacts/deleted" + q({"q": f"purgeme.{C.rid}"}), None, C.owner)
    s, rs = req("POST", "/contacts/restore", {"prospect_ids": [pid]}, C.owner)
    out = subprocess.run(["docker", "exec", "vector-api-1", "python", "-c",
                          "from app.core.database import SessionLocal; from app.services.crm import purge_expired; "
                          "db=SessionLocal(); print(purge_expired(db)); db.commit()"], capture_output=True, text=True, timeout=300)
    left = {t: count(f"SELECT COUNT(*) FROM {t} WHERE {col}='{pid}'") for t, col in (
        ("prospects", "prospect_id"), ("contact_activities", "prospect_id"), ("crm_tasks", "prospect_id"),
        ("prospect_list_members", "prospect_id"), ("property_changes", "object_id"))}
    R.check("CM-068", "After 90 days: no restore; purge job removes contact and dependents (BR-CM-35, integration)",
            deleted["total"] == 0 and rs["restored"] == 0 and out.returncode == 0 and all(v == 0 for v in left.values()),
            f"bin={deleted['total']} restore={rs} purge rc={out.returncode} {out.stdout.strip()[-80:]} {out.stderr.strip()[-200:]} left={left}")


@case("CM-069", "Company soft delete/restore; purge detaches contacts and removes company tasks (BR-CM-35, integration)")
def _():
    acc = ok(*req("POST", "/accounts", {"name": f"Doomed {C.rid}", "domain": f"doomed-{C.rid}.com"}, C.owner))
    aid = acc["account_id"]
    c = new_contact(C.owner, f"emp@doomed-{C.rid}.com")
    t = ok(*req("POST", "/tasks", {"title": "company task", "account_id": aid}, C.owner))
    ok(*req("DELETE", f"/accounts/{aid}", None, C.owner))
    s, dl = req("GET", "/accounts/deleted", None, C.owner)
    in_bin = any(x["account_id"] == aid for x in dl["items"])
    s, rs = req("POST", f"/accounts/{aid}/restore", None, C.owner)
    ok(*req("DELETE", f"/accounts/{aid}", None, C.owner))
    sql(f"UPDATE accounts SET deleted_at = NOW() - INTERVAL 91 DAY WHERE account_id='{aid}' AND tenant_id='{W.tenant_id}'")
    out = subprocess.run(["docker", "exec", "vector-api-1", "python", "-c",
                          "from app.core.database import SessionLocal; from app.services.crm import purge_expired; "
                          "db=SessionLocal(); print(purge_expired(db)); db.commit()"], capture_output=True, text=True, timeout=300)
    acc_left = count(f"SELECT COUNT(*) FROM accounts WHERE account_id='{aid}'")
    contact_acc = sql(f"SELECT IFNULL(account_id,'NULL') FROM prospects WHERE prospect_id='{c['prospect_id']}'")
    task_left = count(f"SELECT COUNT(*) FROM crm_tasks WHERE task_id='{t['task_id']}'")
    R.check("CM-069", "Company soft delete/restore; purge detaches contacts and removes company tasks (BR-CM-35, integration)",
            in_bin and rs.get("status") == "restored" and out.returncode == 0 and acc_left == 0 and contact_acc == "NULL"
            and task_left == 0,
            f"in_bin={in_bin} restore={rs} purge rc={out.returncode} {out.stderr.strip()[-200:]} account_left={acc_left} "
            f"contact.account={contact_acc} task_left={task_left}")


# ════════════════════════════════════════════════════════════════════
# OWNERSHIP, ACCESS, BULK (BR-CM-36..38)
# ════════════════════════════════════════════════════════════════════
@case("CM-070", "Bulk reassign owner; logged in history as BULK (BR-CM-36)")
def _():
    cs = [new_contact(C.owner, em(f"bulkown{i}")) for i in range(3)]
    ids = [c["prospect_id"] for c in cs]
    s, r = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "assign_owner", "owner_id": C.agent2["user_id"]}, C.owner)
    s, seen = req("GET", "/contacts" + q({"q": f"bulkown", "page_size": 50}), None, C.agent2["token"])
    s, h = req("GET", f"/contacts/{ids[0]}/history?field=owner_id", None, C.owner)
    bad, _ = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "assign_owner", "owner_id": W2.user_id}, C.owner)
    R.check("CM-070", "Bulk reassign owner; logged in history as BULK (BR-CM-36)",
            r["updated"] == 3 and {x["prospect_id"] for x in seen["items"]} >= set(ids) and h["items"][0]["source"] == "BULK"
            and bad == 400, f"resp={r} agent2_sees={len(seen['items'])} hist={h['items'][:1]} foreign_owner={bad}")


@case("CM-071", "Bulk edit property, tags, add to (new) list, enroll in campaign and delete (BR-CM-37)")
def _():
    cs = [new_contact(C.owner, em(f"bulk{i}"), first_name=f"Bulk{i}") for i in range(4)]
    ids = [c["prospect_id"] for c in cs]
    s1, r1 = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "set_property", "field": "lifecycle_stage", "value": "SQL"}, C.owner)
    s2, r2 = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "set_property", "field": "lead_status", "value": "BOGUS"}, C.owner)
    s3, r3 = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "add_tags", "tags": ["vip", "VIP", " q4 "]}, C.owner)
    s4, r4 = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "add_to_list", "new_list_name": f"Bulk list {C.rid}"}, C.owner)
    s5, r5 = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "set_property", "field": f"custom.{C.fields['SELECT']}", "value": "Silver"}, C.owner)
    s6, r6 = req("POST", "/contacts/bulk", {"prospect_ids": ids[:2], "action": "enroll", "campaign_id": C.campaign["campaign_id"]}, C.owner)
    s7, r7 = req("POST", "/contacts/bulk", {"prospect_ids": ids[3:], "action": "delete"}, C.owner)
    s8, _ = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "set_property", "field": "email", "value": "x@y.z"}, C.owner)
    s9, _ = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "explode"}, C.owner)
    s, d = req("GET", f"/contacts/{ids[0]}", None, C.owner)
    R.check("CM-071", "Bulk edit property, tags, add to (new) list, enroll in campaign and delete (BR-CM-37)",
            r1["updated"] == 4 and r2["updated"] == 0 and len(r2["skipped"]) == 4 and r3["updated"] == 4
            and r4["updated"] == 4 and r5["updated"] == 4 and r6.get("enrolled_count") == 2 and r7["updated"] == 1
            and s8 == 400 and s9 == 400 and d["lifecycle_stage"] == "SQL" and sorted(d["tags"]) == ["q4", "vip"]
            and d["custom_fields"].get(C.fields["SELECT"]) == "Silver"
            and any(x["list_name"] == f"Bulk list {C.rid}" for x in d["lists"]),
            f"set={r1.get('updated')} bogus={r2.get('updated')}/{len(r2.get('skipped', []))} tags={d['tags']} list={r4} "
            f"custom={r5.get('updated')} enroll={str(r6)[:150]} delete={r7} email_field={s8} unknown={s9}")


@case("CM-072", "User role (agent) sees/edits only own contacts; cannot reassign or bulk (BR-CM-38)")
def _():
    other = C.jane["prospect_id"]
    own = C.agent1_contact["prospect_id"]
    g, _ = req("GET", f"/contacts/{other}", None, C.agent1["token"])
    p, _ = req("PATCH", f"/contacts/{other}", {"first_name": "x"}, C.agent1["token"])
    t, _ = req("GET", f"/contacts/{other}/timeline", None, C.agent1["token"])
    a, _ = req("POST", f"/contacts/{other}/activities", {"activity_type": "NOTE", "body": "x"}, C.agent1["token"])
    ro, _ = req("PATCH", f"/contacts/{own}", {"owner_id": C.agent2["user_id"]}, C.agent1["token"])
    b, _ = req("POST", "/contacts/bulk", {"prospect_ids": [own], "action": "add_tags", "tags": ["x"]}, C.agent1["token"])
    s, lst = req("GET", "/contacts?page_size=200", None, C.agent1["token"])
    foreign = [x for x in lst["items"] if x["owner_id"] != C.agent1["user_id"]]
    dup, dupb = req("POST", "/contacts", {"email": C.jane["email"]}, C.agent1["token"])
    own_edit, _ = req("PATCH", f"/contacts/{own}", {"designation": "Analyst"}, C.agent1["token"])
    R.check("CM-072", "User role (agent) sees/edits only own contacts; cannot reassign or bulk (BR-CM-38)",
            g == 404 and p == 404 and t == 404 and a == 404 and ro == 403 and b == 403 and not foreign
            and dup == 409 and dupb["detail"]["prospect_id"] is None and own_edit == 200,
            f"get={g} patch={p} timeline={t} activity={a} reassign={ro} bulk={b} foreign_in_list={len(foreign)} "
            f"dup={dup} leak_id={dupb['detail'].get('prospect_id') if isinstance(dupb, dict) else dupb} own_edit={own_edit}")


@case("CM-073", "Admin role sees every contact; manage_prospects permission grants admin-level access (BR-CM-38)")
def _():
    s, a = req("GET", "/contacts?page_size=1", None, C.admin["token"])
    s, o = req("GET", "/contacts?page_size=1", None, C.owner)
    s, mp = req("GET", f"/contacts/{C.agent1_contact['prospect_id']}", None, C.agent_mp["token"])
    s2, m2 = req("GET", "/contacts/meta", None, C.agent_mp["token"])
    R.check("CM-073", "Admin role sees every contact; manage_prospects permission grants admin-level access (BR-CM-38)",
            a["total"] == o["total"] and mp.get("prospect_id") == C.agent1_contact["prospect_id"] and m2["can_manage"],
            f"admin_total={a['total']} owner_total={o['total']} mp_get={s}")


@case("CM-074", "Tenant isolation: another workspace cannot read or change these records (§6 security)")
def _():
    t = W2.token
    pid, aid = C.jane["prospect_id"], C.jane["account_id"]
    res = {
        "get": req("GET", f"/contacts/{pid}", None, t)[0],
        "patch": req("PATCH", f"/contacts/{pid}", {"first_name": "pwn"}, t)[0],
        "delete": req("DELETE", f"/contacts/{pid}", None, t)[0],
        "timeline": req("GET", f"/contacts/{pid}/timeline", None, t)[0],
        "history": req("GET", f"/contacts/{pid}/history", None, t)[0],
        "account": req("GET", f"/accounts/{aid}", None, t)[0],
        "account_patch": req("PATCH", f"/accounts/{aid}", {"name": "pwn"}, t)[0],
        "list": req("GET", f"/lists/{C.static_list['list_id']}", None, t)[0],
        "import": req("GET", f"/imports/{C.journey_job['import_id']}", None, t)[0],
        "bulk": req("POST", "/contacts/bulk", {"prospect_ids": [pid], "action": "add_tags", "tags": ["x"]}, t)[0],
        "merge": req("POST", "/contacts/merge", {"primary_id": pid, "duplicate_ids": [C.custom_contact]}, t)[0],
        "restore": req("POST", "/contacts/restore", {"prospect_ids": [pid]}, t)[1].get("restored"),
        "task_on_contact": req("POST", "/tasks", {"title": "x", "prospect_id": pid}, t)[0],
        "erase": req("DELETE", f"/prospects/{pid}", None, t)[0],
    }
    s, own_list = ok(*req("POST", "/lists", {"list_name": "B list"}, t)), None
    add = req("POST", f"/lists/{s['list_id']}/members", {"prospect_ids": [pid]}, t)[1]
    res["add_foreign_member"] = add.get("added")
    srch = req("GET", "/search" + q({"q": C.jane["email"]}), None, t)[1]
    res["search"] = len(srch["contacts"]) + len(srch["companies"])
    same_email = req("POST", "/contacts", {"email": C.jane["email"]}, t)[0]
    still = sql(f"SELECT first_name FROM prospects WHERE prospect_id='{pid}'")
    expect_404 = ("get", "patch", "timeline", "history", "account", "account_patch", "list", "import", "task_on_contact", "erase")
    bad = {k: v for k, v in res.items() if (k in expect_404 and v != 404)
           or (k in ("bulk", "merge") and v != 404) or (k == "delete" and v not in (403, 404))
           or (k in ("restore", "add_foreign_member", "search") and v != 0)}
    R.check("CM-074", "Tenant isolation: another workspace cannot read or change these records (§6 security)",
            not bad and same_email == 201 and still == "Jane", f"leaks={bad} same_email_other_tenant={same_email} name={still}")


@case("CM-075", "Owner assignment restricted to users of the same workspace (BR-CM-36)")
def _():
    s1, _ = req("POST", "/contacts", {"email": em("foreignowner"), "owner_id": W2.user_id}, C.owner)
    s2, _ = req("PATCH", f"/contacts/{C.jane['prospect_id']}", {"owner_id": W2.user_id}, C.owner)
    s3, _ = req("POST", "/contacts", {"email": em("agentassign"), "owner_id": C.agent2["user_id"]}, C.agent1["token"])
    R.check("CM-075", "Owner assignment restricted to users of the same workspace (BR-CM-36)",
            s1 == 400 and s2 == 400 and s3 == 403, f"create={s1} patch={s2} agent_assign_other={s3}")


# ════════════════════════════════════════════════════════════════════
# COMMUNICATION PREFERENCES & COMPLIANCE (BR-CM-39/40, §7)
# ════════════════════════════════════════════════════════════════════
def unsub_row(email):
    return count(f"SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id='{W.tenant_id}' AND LOWER(email)='{email.lower()}'")


@case("CM-076", "Subscription status change syncs with the global unsubscribe list (BR-CM-39)")
def _():
    c = new_contact(C.owner, em("subs"))
    pid = c["prospect_id"]
    ok(*req("PATCH", f"/contacts/{pid}", {"consent_status": "UNSUBSCRIBED"}, C.owner))
    on = unsub_row(c["email"])
    s, d = req("GET", f"/contacts/{pid}", None, C.owner)
    ok(*req("PATCH", f"/contacts/{pid}", {"consent_status": "OPT_IN"}, C.owner))
    off = unsub_row(c["email"])
    bad, _ = req("PATCH", f"/contacts/{pid}", {"consent_status": "MAYBE"}, C.owner)
    cs = [new_contact(C.owner, em(f"bulksub{i}")) for i in range(2)]
    ok(*req("POST", "/contacts/bulk", {"prospect_ids": [x["prospect_id"] for x in cs], "action": "set_property",
                                       "field": "consent_status", "value": "UNSUBSCRIBED"}, C.owner))
    bulk_rows = sum(unsub_row(x["email"]) for x in cs)
    R.check("CM-076", "Subscription status change syncs with the global unsubscribe list (BR-CM-39)",
            on == 1 and d["consent_source"] == "MANUAL" and d["consent_timestamp"] and off == 0 and bad == 400 and bulk_rows == 2,
            f"unsub_row={on} source={d['consent_source']} after_optin={off} invalid={bad} bulk={bulk_rows}")


@case("CM-077", "Globally unsubscribed address stays unsubscribed when created via UI or import (BR-CM-39, §7)")
def _():
    e1, e2 = em("pre.unsub1"), em("pre.unsub2")
    sql(f"INSERT INTO global_unsubscribes (tenant_id, email, reason) VALUES ('{W.tenant_id}','{e1}','systest'),('{W.tenant_id}','{e2}','systest')")
    c = new_contact(C.owner, e1.upper(), consent_status="OPT_IN")
    st, job, _ = run_import(C.owner, csv_bytes(["Email", "First Name"], [[e2, "Pre"]]),
                            {"Email": "contact.email", "First Name": "contact.first_name"})
    imp = sql(f"SELECT consent_status FROM prospects WHERE tenant_id='{W.tenant_id}' AND email='{e2}'")
    R.check("CM-077", "Globally unsubscribed address stays unsubscribed when created via UI or import (BR-CM-39, §7)",
            c["consent_status"] == "UNSUBSCRIBED" and imp == "UNSUBSCRIBED", f"ui={c['consent_status']} import={imp}")


@case("CM-078", "Unsubscribe link in a campaign email updates the CRM contact (BR-CM-39, integration)")
def _():
    c = C.email_contact
    s, body = req("POST", f"/tracking/unsubscribe/{C.email_out_id}", form={"confirm": "1"})
    s2, d = req("GET", f"/contacts/{c['prospect_id']}", None, C.owner)
    s3, t = req("GET", f"/contacts/{c['prospect_id']}/timeline", None, C.owner)
    R.check("CM-078", "Unsubscribe link in a campaign email updates the CRM contact (BR-CM-39, integration)",
            s == 200 and d["consent_status"] == "UNSUBSCRIBED" and unsub_row(c["email"]) == 1
            and any(i["kind"] == "UNSUBSCRIBED" for i in t["items"]),
            f"post={s} consent={d['consent_status']} row={unsub_row(c['email'])} kinds={[i['kind'] for i in t['items']]}")


@case("CM-079", "Consent and legal basis recorded per contact (BR-CM-40)")
def _():
    c = new_contact(C.owner, em("legal"), legal_basis="CONSENT")
    s, d = req("PATCH", f"/contacts/{c['prospect_id']}", {"legal_basis": "LEGITIMATE_INTEREST"}, C.owner)
    bad, _ = req("PATCH", f"/contacts/{c['prospect_id']}", {"legal_basis": "BECAUSE"}, C.owner)
    s, h = req("GET", f"/contacts/{c['prospect_id']}/history?field=legal_basis", None, C.owner)
    R.check("CM-079", "Consent and legal basis recorded per contact (BR-CM-40)",
            c["legal_basis"] == "CONSENT" and d["legal_basis"] == "LEGITIMATE_INTEREST" and bad == 400 and h["items"]
            and c["consent_timestamp"] and c["consent_source"],
            f"created={c['legal_basis']} updated={d.get('legal_basis')} invalid={bad} hist={len(h['items'])}")


@case("CM-080", "Erasure on request removes the contact and all personal data; the deletion is logged (§7 GDPR/DPDP)")
def _():
    c = C.email_contact
    pid = c["prospect_id"]
    ok(*req("POST", f"/contacts/{pid}/activities", {"activity_type": "NOTE", "body": "personal data"}, C.owner))
    s, r = req("DELETE", f"/prospects/{pid}", None, C.owner)
    left = {t: count(f"SELECT COUNT(*) FROM {t} WHERE {col}='{pid}'") for t, col in (
        ("prospects", "prospect_id"), ("email_messages", "prospect_id"), ("contact_activities", "prospect_id"),
        ("property_changes", "object_id"), ("campaign_prospects", "prospect_id"))}
    ev = count(f"SELECT COUNT(*) FROM email_events WHERE message_id='{C.email_out_id}'")
    logged = count(f"SELECT COUNT(*) FROM audit_logs WHERE tenant_id='{W.tenant_id}' AND entity_id='{pid}'") + \
        count(f"SELECT COUNT(*) FROM property_changes WHERE tenant_id='{W.tenant_id}' AND object_id='{pid}'")
    erased = s == 200 and all(v == 0 for v in left.values()) and ev == 0
    R.check("CM-080", "Erasure removes the contact and all dependent personal data (§7 GDPR/DPDP)", erased,
            f"status={s} left={left} events={ev}")
    R.check("CM-080b", "Erasure is logged (who/when/which record) (§7 'deletion logged')", erased and logged > 0,
            f"audit_logs+property_changes rows for erased id={logged}; DELETE /prospects/{{id}} writes no log", "defect")


# ════════════════════════════════════════════════════════════════════
# RESEARCH WORKFLOW & OPTIONAL (BR-CM-41..43)
# ════════════════════════════════════════════════════════════════════
@case("CM-081", "Market Research journey: import → filter view → assign owners → list → campaign → export (BR-CM-41)")
def _():
    r = C.rid
    rows = [[f"mr{i}.{r}@research{i % 3}-{r}.com", f"MR{i}", "Research", f"Research Co {i % 3} {r}", "India" if i % 2 else "USA"]
            for i in range(12)]
    st, job, _ = run_import(C.manager["token"], csv_bytes(["Email", "First", "Last", "Company", "Country"], rows),
                            {"Email": "contact.email", "First": "contact.first_name", "Last": "contact.last_name",
                             "Company": "company.name", "Country": "contact.poc_country"})
    who = C.agent_mp["token"]  # research users need manage_prospects (as in the BRD research role)
    st, job, _ = run_import(who, csv_bytes(["Email", "First", "Last", "Company", "Country"], rows),
                            {"Email": "contact.email", "First": "contact.first_name", "Last": "contact.last_name",
                             "Company": "company.name", "Country": "contact.poc_country"})
    ok(st, job, 201)
    flt = {"op": "AND", "conditions": [{"field": "email", "operator": "contains", "value": f".{r}@research"},
                                       {"field": "poc_country", "operator": "is", "value": "India"}]}
    view = ok(*req("POST", "/views", {"name": f"India research {r}", "filters": flt, "shared": True}, C.owner))
    s, lst = req("GET", "/contacts" + q({"filters": view["filters"], "page_size": 100}), None, C.owner)
    ids = [x["prospect_id"] for x in lst["items"]]
    s1, ra = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "assign_owner", "owner_id": C.agent1["user_id"]}, C.owner)
    s2, rl = req("POST", "/contacts/bulk", {"prospect_ids": ids, "action": "add_to_list", "new_list_name": f"India outreach {r}"}, C.owner)
    camp = ok(*req("POST", "/campaigns", {"campaign_name": f"India outreach {r}"}, C.owner))
    s3, en = req("POST", f"/campaigns/{camp['campaign_id']}/prospects", {"list_ids": [rl["list_id"]]}, C.owner)
    s4, raw = req("GET", "/contacts/export" + q({"list_id": rl["list_id"], "columns": "email,owner_name"}), None, C.owner, raw=True)
    exp = parse_csv(raw) if s4 == 200 else []
    s5, mine = req("GET", "/contacts" + q({"list_id": rl["list_id"]}), None, C.agent1["token"])
    R.check("CM-081", "Market Research journey: import → filter view → assign owners → list → campaign → export (BR-CM-41)",
            job["contacts_created"] == 12 and job["companies_created"] == 3 and len(ids) == 6 and ra["updated"] == 6
            and rl["updated"] == 6 and en.get("enrolled_count") == 6 and len(exp) == 7
            and all(row[1] == f"Ann{r} Test" for row in exp[1:]) and mine["total"] == 6,
            f"import={job.get('contacts_created')}/{job.get('companies_created')} view={len(ids)} assign={ra.get('updated')} "
            f"list={rl.get('updated')} enrolled={en.get('enrolled_count')} export_rows={len(exp) - 1} agent_sees={mine['total']}")


@case("CM-082", "Company data enrichment from the domain (BR-CM-42, Could)")
def _():
    s, spec = req("GET", "/openapi.json")
    paths = [p for p in spec["paths"] if "enrich" in p.lower()]
    acc = ok(*req("POST", "/accounts", {"domain": "stripe.com", "name": f"Stripe {C.rid}"}, C.owner))
    if paths or acc.get("industry"):
        R.record("CM-082", "Company data enrichment from the domain (BR-CM-42, Could)", True, f"{paths}")
    else:
        R.record("CM-082", "Company data enrichment from the domain (BR-CM-42, Could)", None,
                 "no enrichment endpoint; company created from domain keeps industry/size/location empty", "not-implemented")


@case("CM-083", "Contact scoring on fit and engagement (BR-CM-43, Could)")
def _():
    s, d = req("GET", f"/contacts/{C.jane['prospect_id']}", None, C.owner)
    keys = [k for k in d if "score" in k.lower()]
    s, spec = req("GET", "/openapi.json")
    sort_score = "score" in json.dumps(spec["paths"]["/contacts"]["get"]["parameters"]).lower()
    if keys or sort_score:
        R.record("CM-083", "Contact scoring on fit and engagement (BR-CM-43, Could)", True, f"{keys}")
    else:
        R.record("CM-083", "Contact scoring on fit and engagement (BR-CM-43, Could)", None,
                 "no fit/engagement score on contact payload, filters or sort (only AI persona classification)",
                 "not-implemented")


# ════════════════════════════════════════════════════════════════════
# ACCEPTANCE: 1,000-row contact + company file < 10 s
# ════════════════════════════════════════════════════════════════════
PERF_OUT = {}


@case("CM-084", "Acceptance: 1,000-row contact+company file imports < 10 s with associations and per-row reasons (§5.2)")
def _():
    r = C.rid
    rows = [[f"k{i}.{r}@kilo{i % 50}-{r}.com", f"Kilo{i}", "Row", f"Kilo Company {i % 50} {r}", f"kilo{i % 50}-{r}.com",
             "Lead", f"+1 303 555 {i:04d}"] for i in range(990)]
    rows += [[f"broken{i}", "Bad", "Row", "", "", "", ""] for i in range(10)]
    st, job, secs = run_import(C.owner, csv_bytes(["Email", "First Name", "Last Name", "Company", "Company Domain",
                                                   "Lifecycle Stage", "Phone"], rows),
                               {"Email": "contact.email", "First Name": "contact.first_name", "Last Name": "contact.last_name",
                                "Company": "company.name", "Company Domain": "company.domain",
                                "Lifecycle Stage": "contact.lifecycle_stage", "Phone": "contact.phone"}, fname="kilo.csv")
    linked = count(f"SELECT COUNT(*) FROM prospects p JOIN accounts a ON a.account_id=p.account_id WHERE p.tenant_id='{W.tenant_id}' "
                   f"AND p.email LIKE 'k%.{r}@kilo%' AND a.domain = SUBSTRING_INDEX(p.email,'@',-1)")
    PERF_OUT["import_1k_seconds"] = round(secs, 2)
    R.check("CM-084", "Acceptance: 1,000-row contact+company file imports < 10 s with associations and per-row reasons (§5.2)",
            st == 201 and job["contacts_created"] == 990 and job["companies_created"] == 50 and job["error_count"] == 10
            and all(e["reason"] for e in job["errors"]) and linked == 990 and secs < 10,
            f"secs={secs:.2f} created={job.get('contacts_created')} companies={job.get('companies_created')} "
            f"errors={job.get('error_count')} linked={linked}")


@case("CM-085", "Single-column CSV (just an Email column) imports correctly (BR-CM-25)")
def _():
    content = ("Email\n" + "\n".join(f"solo{i}.{C.rid}@mono-{C.rid}.com" for i in range(3)) + "\n").encode()
    s, p = req("POST", "/imports/preview", files={"file": ("emails.csv", content)}, token=C.owner)
    st, job, _ = run_import(C.owner, content, {"Email": "contact.email"}, fname="emails.csv")
    R.check("CM-085", "Single-column CSV (just an Email column) imports correctly (BR-CM-25)",
            s == 200 and p.get("headers") == ["Email"] and st == 201 and job.get("contacts_created") == 3,
            f"preview headers={p.get('headers')} sample={str(p.get('sample'))[:120]}; import={st} {str(job)[:120]}")


# ════════════════════════════════════════════════════════════════════
# UI SYSTEM CASES (Playwright)
# ════════════════════════════════════════════════════════════════════
def ui_cases():
    base = os.environ.get("PW_BASE", "/tmp/claude-0/-home-user/b764cdf9-b625-5070-aae1-e6d130b3e7df/scratchpad")
    env = dict(os.environ, PW_BASE=base, WEB_URL=WEB, UI_EMAIL=W.email, UI_PASSWORD=W.password,
               UI_CONTACT_ID=C.jane["prospect_id"], UI_CONTACT_NAME="Jane", UI_SHOTS=os.path.join(HERE, "shots"))
    out = subprocess.run(["node", os.path.join(HERE, "ui_contacts.mjs")], capture_output=True, text=True, env=env, timeout=300)
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    if not lines:
        for cid in ("CM-UI-01", "CM-UI-02", "CM-UI-03", "CM-UI-04"):
            R.record(cid, "Playwright UI case", False, f"runner failed rc={out.returncode} {out.stderr[-300:]}", "test-issue")
        return
    for l in lines:
        r = json.loads(l)
        R.record(r["id"], r["title"], r["pass"], r.get("detail", ""), None if r["pass"] else r.get("category", "defect"))


if UI and (not ONLY or any(o.startswith("CM-UI") for o in ONLY)):
    try:
        ui_cases()
    except Exception as exc:  # noqa: BLE001
        R.record("CM-UI", "Playwright UI cases", False, f"exception {exc}", "test-issue")


# ════════════════════════════════════════════════════════════════════
# PERFORMANCE NFRs (§6): 10k-row import, search & list latency
# ════════════════════════════════════════════════════════════════════
def perf():
    P = W2  # tenant B doubles as the perf tenant (isolation cases already ran); register is limited to 10/h per IP
    r = P.rid
    first = ["Aarav", "Priya", "John", "Maria", "Wei", "Fatima", "Lukas", "Sofia", "Kenji", "Olu", "Emma", "Diego",
             "Anika", "Noah", "Zara", "Ivan", "Leila", "Mateo", "Hana", "Ravi"]
    last = ["Sharma", "Smith", "Garcia", "Chen", "Khan", "Müller", "Rossi", "Tanaka", "Okafor", "Brown", "Silva",
            "Ivanova", "Nguyen", "Patel", "Kim", "Lopez", "Cohen", "Haddad", "Dubois", "Jensen"]
    rows = []
    for i in range(10000):
        f, l = first[i % 20], last[(i * 7) % 20]
        d = f"perfco{i % 800}-{r}.com"
        rows.append([f"{f.lower()}.{l.lower()}.{i}@{d}", f, f"{l}{i}", f"Perf Company {i % 800} {r}", d,
                     f"+91 98{i:08d}", ["Lead", "MQL", "SQL", "Customer"][i % 4], ["India", "USA", "Germany"][i % 3]])
    content = csv_bytes(["Email", "First Name", "Last Name", "Company", "Company Domain", "Phone", "Lifecycle Stage",
                         "Country"], rows)
    st, job, secs = run_import(P.token, content, {
        "Email": "contact.email", "First Name": "contact.first_name", "Last Name": "contact.last_name",
        "Company": "company.name", "Company Domain": "company.domain", "Phone": "contact.phone",
        "Lifecycle Stage": "contact.lifecycle_stage", "Country": "contact.poc_country"}, fname="perf10k.csv", timeout=1800)
    PERF_OUT.update(import_10k_status=st, import_10k_seconds=round(secs, 1),
                    import_10k_created=job.get("contacts_created") if isinstance(job, dict) else str(job)[:200],
                    import_10k_companies=job.get("companies_created") if isinstance(job, dict) else None,
                    import_10k_rows_per_sec=round(10000 / secs, 1))
    R.check("CM-090", "NFR: a 10,000-row file imports successfully (§6 Scale)",
            st == 201 and isinstance(job, dict) and job["contacts_created"] == 10000 and job["companies_created"] == 800,
            f"status={st} secs={secs:.1f} created={PERF_OUT['import_10k_created']} companies={PERF_OUT['import_10k_companies']}")
    tenant_rows = count(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{P.tenant_id}'")
    total_rows = count("SELECT COUNT(*) FROM prospects")
    queries = ["priya", "sharma", "garcia42", "john.smith", "maria.chen.1", "9800001234", "98 0000 5678", "+91 9800009999",
               f"perf company 77 {r}", f"perfco123-{r}.com", "okafor", "kenji.tanaka", "zz-no-match-zz", "emma",
               "diego.lopez", "fatima", "4321", f"perfco7-{r}", "ivanova9", "leila.haddad"]
    lat_search, lat_list, lat_board, lat_views = [], [], [], []
    for qq in queries:  # warm-up once, then measure
        req("GET", "/search" + q({"q": qq}), None, P.token)
    hits = 0
    for qq in queries:
        s, b, ms = timed("GET", "/search" + q({"q": qq}), P.token)
        lat_search.append(ms)
        hits += 1 if s == 200 and b["contacts"] else 0
        s, b, ms = timed("GET", "/contacts" + q({"q": qq, "page_size": 25}), P.token)
        lat_list.append(ms)
    view_specs = [{}, {"sort_by": "name", "sort_order": "asc"}, {"lifecycle_stage": "MQL"}, {"page": 200},
                  {"filters": {"op": "AND", "conditions": [{"field": "poc_country", "operator": "is", "value": "India"},
                                                           {"field": "lifecycle_stage", "operator": "in", "value": ["SQL", "MQL"]}]}},
                  {"owner": "me", "sort_by": "company"}]
    for _ in range(3):
        for spec in view_specs:
            s, b, ms = timed("GET", "/contacts" + q({**spec, "page_size": 25}), P.token)
            lat_views.append(ms)
        s, b, ms = timed("GET", "/contacts/board", P.token)
        lat_board.append(ms)
    # same search on a small tenant for a scaling estimate
    small = []
    for qq in ["jane", "zorblax", "acme", "5550143", "kilo12", "wayne"]:
        req("GET", "/search" + q({"q": qq}), None, C.owner)  # warm-up
    for qq in ["jane", "zorblax", "acme", "5550143", "kilo12", "wayne"]:
        s, b, ms = timed("GET", "/search" + q({"q": qq}), C.owner)
        small.append(ms)
    small_rows = count(f"SELECT COUNT(*) FROM prospects WHERE tenant_id='{W.tenant_id}'")
    p50s, p95s = pct(lat_search, 50), pct(lat_search, 95)
    slope = (p50s - pct(small, 50)) / max(tenant_rows - small_rows, 1)  # ms per contact
    est100k = pct(small, 50) + slope * 100000
    PERF_OUT.update(perf_tenant=P.tenant_id, tenant_contacts=tenant_rows, db_total_contacts=total_rows,
                    search_queries=len(queries), search_hits=hits,
                    search_p50_ms=round(p50s, 1), search_p95_ms=round(p95s, 1), search_max_ms=round(max(lat_search), 1),
                    list_q_p50_ms=round(pct(lat_list, 50), 1), list_q_p95_ms=round(pct(lat_list, 95), 1),
                    views_p50_ms=round(pct(lat_views, 50), 1), views_p95_ms=round(pct(lat_views, 95), 1),
                    board_p50_ms=round(pct(lat_board, 50), 1),
                    small_tenant_contacts=small_rows, small_tenant_search_p50_ms=round(pct(small, 50), 1),
                    est_search_p50_at_100k_ms=round(est100k, 0))
    R.check("CM-091", "NFR: global search p95 < 1 s (measured at ~10k contacts; 100k target extrapolated)",
            p95s < 1000, f"p50={p50s:.0f}ms p95={p95s:.0f}ms at {tenant_rows} contacts; linear est. p50 at 100k ≈ {est100k:.0f}ms")
    R.check("CM-092", "NFR: contact list search (q=) p95 < 1 s at ~10k contacts",
            pct(lat_list, 95) < 1000, f"p50={pct(lat_list, 50):.0f}ms p95={pct(lat_list, 95):.0f}ms")
    R.check("CM-093", "NFR: list views (sort/filter/deep page) and board p95 < 1 s at ~10k contacts",
            pct(lat_views, 95) < 1000 and max(lat_board) < 1000,
            f"views p50={pct(lat_views, 50):.0f}ms p95={pct(lat_views, 95):.0f}ms board max={max(lat_board):.0f}ms")
    R.check("CM-094", "NFR: extrapolated search p50 at 100k contacts < 1 s (flag)", est100k < 1000,
            f"linear extrapolation from {small_rows}→{tenant_rows} rows: ≈{est100k:.0f}ms (not measured at 100k)")


if PERF and (not ONLY or any(o.startswith("CM-09") for o in ONLY)):
    try:
        perf()
    except Exception as exc:  # noqa: BLE001
        R.record("CM-090", "Performance run", False, f"exception {exc} {traceback.format_exc()[-300:]}", "test-issue")

if PERF_OUT:
    with open(os.path.join(HERE, "perf.json"), "w") as fh:
        json.dump(PERF_OUT, fh, indent=2)
    print("PERF", json.dumps(PERF_OUT))

counts = R.write()
print("COUNTS", counts, f"tenant={W.tenant_id}")
