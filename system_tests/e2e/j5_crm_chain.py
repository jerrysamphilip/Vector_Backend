"""J5 - Import/CRM chain: import (mapping + custom field) -> lists -> bulk edit -> campaign enrolment -> lead/deal/task/activity ->
merge duplicates -> recycle bin -> restore -> purge. Every dependent record must follow."""
import csv
import io
import json
import subprocess
from datetime import date, timedelta

from .flows import contact_by_email, create_campaign, enroll, generate_content, mk_inbox, add_missing_step_templates, launch, msgs
from .helpers import API_CONTAINER, add_user, new_workspace, q, req, rows, scalar, sql

HEAD = ["Email", "First Name", "Last Name", "Job Title", "Company", "Company Domain", "Tags", "Seats"]


def _csv(rows_):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(HEAD)
    w.writerows(rows_)
    return out.getvalue().encode()


def run(R):
    ws = new_workspace("j5")
    rid = ws.rid
    ws.set_sender_identity()
    owner = ws.token
    rows_ = []
    for i in range(6):
        rows_.append([f"w{i}@wayne-{rid}.com", f"Wayne{i}", "Person", "Engineer", f"Wayne Enterprises {rid}", f"wayne-{rid}.com", f"imp-{rid};wave1", str(i + 1)])
    for i in range(4):
        rows_.append([f"s{i}@stark-{rid}.com", f"Stark{i}", "Person", "Analyst", f"Stark Industries {rid}", f"stark-{rid}.com", f"imp-{rid}", ""])
    rows_ += [["not-an-email", "Bad", "Row", "", "", "", "", ""], [f"w0@WAYNE-{rid}.com", "Dup", "InFile", "", "", "", "", ""]]
    s, prev = req("POST", "/imports/preview", files={"file": ("book.csv", _csv(rows_))}, token=owner)
    sm = prev.get("suggested_mapping", {}) if isinstance(prev, dict) else {}
    R.check("E2E-J5-01", "Import preview: 12 rows detected and columns auto-mapped (email, name, company, domain, title)",
            s == 200 and prev.get("row_count") == 12 and sm.get("Email") == "contact.email" and sm.get("Company") == "company.name"
            and sm.get("Company Domain") == "company.domain", f"{s} {sm}")
    mapping = {"Email": "contact.email", "First Name": "contact.first_name", "Last Name": "contact.last_name",
               "Job Title": "contact.designation", "Company": "company.name", "Company Domain": "company.domain",
               "Tags": "contact.tags", "Seats": f"new:NUMBER:Seats {rid}"}
    s, job = req("POST", "/imports", files={"file": ("book.csv", _csv(rows_))}, form={"mapping": json.dumps(mapping), "options": json.dumps({})}, token=owner)
    errs = {e["row"]: e["reason"] for e in job.get("errors", [])} if s == 201 else {}
    R.check("E2E-J5-02", "Import: 10 contacts + 2 companies created, 2 rejected rows with reasons (invalid email, in-file duplicate)",
            s == 201 and job["contacts_created"] == 10 and job["companies_created"] == 2 and job["error_count"] == 2 and len(errs) == 2,
            f"{s} {str(job)[:300]}")
    fields = req("GET", "/contacts/fields", token=owner)[1]
    seat = next((f for f in fields if f["label"] == f"Seats {rid}"), None)
    c3 = contact_by_email(owner, f"w2@wayne-{rid}.com")
    det = req("GET", f"/contacts/{c3['prospect_id']}", token=owner)[1]
    wayne = scalar(f"SELECT account_id FROM accounts WHERE tenant_id={q(ws.tenant_id)} AND domain={q(f'wayne-{rid}.com')}")
    R.check("E2E-J5-03", "Mapped custom field created (NUMBER) and populated (w2 -> 3); contacts linked to the company by domain; tags split",
            seat and seat["field_type"] == "NUMBER" and det.get("custom_fields", {}).get(seat["field_key"]) == 3
            and scalar(f"SELECT COUNT(*) FROM prospects WHERE account_id={q(wayne)}") == "6" and sorted(det.get("tags", [])) == sorted([f"imp-{rid}", "wave1"]),
            f"{seat} {det.get('custom_fields')} {det.get('tags')}")
    ids = {e: contact_by_email(owner, e)["prospect_id"] for e in [r[0] for r in rows_[:10]]}
    w_ids = [ids[f"w{i}@wayne-{rid}.com"] for i in range(6)]
    s_ids = [ids[f"s{i}@stark-{rid}.com"] for i in range(4)]
    # lists
    s, st = req("POST", "/lists", {"list_name": f"Static {rid}", "list_type": "STATIC"}, owner)
    req("POST", f"/lists/{st['list_id']}/members", {"prospect_ids": w_ids}, owner)
    flt = {"op": "AND", "conditions": [{"field": "tags", "operator": "is", "value": f"imp-{rid}"}]}
    s2, act = req("POST", "/lists", {"list_name": f"Active {rid}", "list_type": "ACTIVE", "filters": flt}, owner)
    a0 = req("GET", f"/lists/{act['list_id']}", token=owner)[1]
    s_l = req("GET", f"/lists/{st['list_id']}", token=owner)[1]
    R.check("E2E-J5-04", "Static list holds the 6 Wayne contacts; active list (tag imp-<run>) resolves to all 10 from its filter",
            s == 201 and s2 == 201 and s_l.get("member_count", s_l.get("total")) == 6 and a0.get("member_count", a0.get("total")) == 10,
            f"static={ {k: s_l.get(k) for k in ('member_count', 'total')} } active={ {k: a0.get(k) for k in ('member_count', 'total')} }")
    # bulk edit
    agent = add_user(ws, "AGENT", f"Agt{rid}")
    s1, b1 = req("POST", "/contacts/bulk", {"prospect_ids": s_ids, "action": "add_tags", "tags": ["bulk-edited"]}, owner)
    s2, b2 = req("POST", "/contacts/bulk", {"prospect_ids": s_ids[:2], "action": "assign_owner", "owner_id": agent["user_id"]}, owner)
    t = req("GET", f"/contacts/{s_ids[0]}", token=owner)[1]
    hist = req("GET", f"/contacts/{s_ids[0]}/history", token=owner)[1]
    hl = hist if isinstance(hist, list) else hist.get("items", [])
    a1 = req("GET", f"/lists/{act['list_id']}", token=owner)[1]
    R.check("E2E-J5-05", "Bulk edit: tag added to 4 contacts, 2 reassigned to the agent; history shows both changes; active list still resolves 10",
            s1 == 200 and s2 == 200 and "bulk-edited" in t["tags"] and t["owner_id"] == agent["user_id"]
            and {"tags", "owner_id"} <= {h.get("field") for h in hl} and a1.get("member_count", a1.get("total")) == 10,
            f"{s1} {s2} tags={t.get('tags')} owner={t.get('owner_id')} hist={[h.get('field') for h in hl]}")
    # dependents: lead + deal + task + activity + campaign enrolment on duplicate-to-be
    dup_a = req("POST", "/contacts", {"email": f"dupe.a@merge-{rid}.com", "first_name": "Dee", "last_name": "Dupe", "company_name": "Merge Co"}, owner)[1]["prospect_id"]
    dup_b = req("POST", "/contacts", {"email": f"dupe.b@merge-{rid}.com", "first_name": "Dee", "last_name": "Dupe", "designation": "CTO", "company_name": "Merge Co"}, owner)[1]["prospect_id"]
    sd, dups = req("GET", "/contacts/duplicates", token=owner)
    flat = json.dumps(dups)
    R.check("E2E-J5-06", "Duplicate detection lists the two 'Dee Dupe' contacts as one group", sd == 200 and dup_a in flat and dup_b in flat, f"{sd} {flat[:200]}")
    lead = req("POST", "/leads", {"prospect_id": dup_b, "source": "Manual"}, owner)[1]
    opp = req("POST", "/opportunities", {"name": f"Merge deal {rid}", "prospect_id": dup_b, "amount": 5000}, owner)[1]
    task = req("POST", "/tasks", {"title": "call Dee", "prospect_id": dup_b}, owner)[1]
    act_ = req("POST", f"/contacts/{dup_b}/activities", {"activity_type": "NOTE", "body": "met at expo"}, owner)[1]
    req("POST", f"/lists/{st['list_id']}/members", {"prospect_ids": [dup_b]}, owner)
    ib, ab, _ = mk_inbox(ws, f"j5.{rid}", f"j5mail-{rid}.example", delay=2)
    cid, _ = create_campaign(ws, f"J5 camp {rid}", [ib], waits=(0,))
    s, lst_b = req("POST", "/lists", {"list_name": f"Dee list {rid}", "list_type": "STATIC"}, owner)
    req("POST", f"/lists/{lst_b['list_id']}/members", {"prospect_ids": [dup_b]}, owner)
    sc, gen = generate_content(ws, cid, lst_b["list_id"], (0,))
    se, en = enroll(ws, cid, lst_b["list_id"])
    req("POST", f"/campaigns/{cid}/launch", None, owner)
    pend_before = len(msgs(cid, status=["SCHEDULED", "QUEUED"]))
    sm_, mr = req("POST", "/contacts/merge", {"primary_id": dup_a, "duplicate_ids": [dup_b], "choices": {"designation": dup_b}}, owner)
    prim = req("GET", f"/contacts/{dup_a}", token=owner)[1]
    cnt = lambda t_, col, pid: scalar(f"SELECT COUNT(*) FROM {t_} WHERE {col}={q(pid)}")  # noqa: E731
    moved = {"lead": cnt("leads", "prospect_id", dup_a), "opp": cnt("opportunities", "prospect_id", dup_a),
             "task": cnt("crm_tasks", "prospect_id", dup_a), "activity": cnt("contact_activities", "prospect_id", dup_a),
             "list": cnt("prospect_list_members", "prospect_id", dup_a), "enrol": cnt("campaign_prospects", "prospect_id", dup_a)}
    left = {"lead": cnt("leads", "prospect_id", dup_b), "opp": cnt("opportunities", "prospect_id", dup_b),
            "task": cnt("crm_tasks", "prospect_id", dup_b), "activity": cnt("contact_activities", "prospect_id", dup_b),
            "enrol_active": scalar(f"SELECT COUNT(*) FROM campaign_prospects WHERE prospect_id={q(dup_b)} AND status='ACTIVE'")}
    R.check("E2E-J5-07", "Merge: duplicate's lead, deal, task, activity, list membership and campaign enrolment are re-pointed to the surviving contact; chosen field (CTO) wins",
            sm_ == 200 and prim.get("designation") == "CTO" and all(v == "1" for k, v in moved.items() if k not in ("list", "activity")) and int(moved["activity"]) >= 1 and moved["list"] in ("2", "3"),
            f"merge={sm_} {str(mr)[:150]} moved={moved} left_on_duplicate={left}")
    pend_after = msgs(cid, status=["SCHEDULED", "QUEUED"])
    R.check("E2E-J5-08", "After merge the campaign has exactly one pending email, addressed to the surviving contact (no double send, none to the merged address)",
            len(pend_after) == 1 and pend_after[0]["email"] == f"dupe.a@merge-{rid}.com", f"before={pend_before} after={[(m['email'], m['step']) for m in pend_after]}")
    gone = req("GET", f"/contacts/{dup_b}", token=owner)[0]
    R.check("E2E-J5-09", "Merged duplicate no longer exists (404, row removed - the survivor keeps the data)",
            gone == 404 and scalar(f"SELECT COUNT(*) FROM prospects WHERE prospect_id={q(dup_b)}") == "0", f"{gone}")
    # recycle bin
    tgt = w_ids[5]
    req("POST", f"/leads", {"prospect_id": tgt}, owner)
    req("POST", "/tasks", {"title": "bin task", "prospect_id": tgt}, owner)
    sdel, _ = req("DELETE", f"/contacts/{tgt}", None, owner)
    binl = req("GET", "/contacts/deleted?page_size=50", token=owner)[1]
    in_bin = [i for i in binl.get("items", []) if i.get("prospect_id") == tgt]
    listed = req("GET", f"/lists/{st['list_id']}", token=owner)[1]
    activel = req("GET", f"/lists/{act['list_id']}", token=owner)[1]
    R.check("E2E-J5-10", "Soft delete: contact hidden (404), appears in the recycle bin with purge date + who deleted; active list drops it (9), nothing else removed",
            sdel == 200 and req("GET", f"/contacts/{tgt}", token=owner)[0] == 404 and in_bin and in_bin[0].get("purge_at")
            and activel.get("member_count", activel.get("total")) == 9 and cnt("leads", "prospect_id", tgt) == "1",
            f"{sdel} bin={in_bin[:1]} active={activel.get('member_count', activel.get('total'))}")
    sr, rs = req("POST", "/contacts/restore", {"prospect_ids": [tgt]}, owner)
    activel = req("GET", f"/lists/{act['list_id']}", token=owner)[1]
    R.check("E2E-J5-11", "Restore brings the contact back with its lead, task and list memberships; active list returns to 10",
            sr == 200 and rs.get("restored") == 1 and req("GET", f"/contacts/{tgt}", token=owner)[0] == 200
            and cnt("leads", "prospect_id", tgt) == "1" and cnt("crm_tasks", "prospect_id", tgt) == "1"
            and activel.get("member_count", activel.get("total")) == 10, f"{sr} {rs}")
    # purge after 90 days (test clock on deleted_at, then the real purge job code)
    req("DELETE", f"/contacts/{tgt}", None, owner)
    sql(f"UPDATE prospects SET deleted_at = NOW() - INTERVAL 91 DAY WHERE prospect_id={q(tgt)}")
    sr2, rs2 = req("POST", "/contacts/restore", {"prospect_ids": [tgt]}, owner)
    out = subprocess.run(["docker", "exec", API_CONTAINER, "python", "-c",
                          "from app.core.database import SessionLocal; from app.services.crm import purge_expired; "
                          "db=SessionLocal(); print(purge_expired(db)); db.commit()"], capture_output=True, text=True, timeout=300)
    left = {t_: cnt(t_, col, tgt) for t_, col in (("prospects", "prospect_id"), ("leads", "prospect_id"), ("crm_tasks", "prospect_id"),
                                                  ("prospect_list_members", "prospect_id"), ("property_changes", "object_id"))}
    R.check("E2E-J5-12", "After 90 days restore is refused and the purge job removes the contact with its lead, task, list memberships and history; an audit row remains",
            rs2.get("restored") == 0 and out.returncode == 0 and all(v == "0" for v in left.values())
            and int(scalar(f"SELECT COUNT(*) FROM audit_logs WHERE entity_id={q(tgt)}") or 0) >= 1, f"restore={rs2} rc={out.returncode} left={left}")
    # purge keeps the other 9 + the company
    R.check("E2E-J5-13", "Purge touched nothing else: 9 imported contacts + the company still exist",
            scalar(f"SELECT COUNT(*) FROM prospects WHERE tenant_id={q(ws.tenant_id)} AND tags LIKE {q('%imp-' + rid + '%')} AND deleted_at IS NULL") == "9"
            and wayne and scalar(f"SELECT COUNT(*) FROM accounts WHERE account_id={q(wayne)} AND deleted_at IS NULL") == "1", "")
