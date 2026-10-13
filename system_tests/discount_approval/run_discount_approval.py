"""Quote discount approval (BRD v2.0 BR-SF-17 / RP-103) against the live stack.

Creates its own workspace: owner (SUPER_ADMIN), admin, manager (level 2) with a rep (level 3)
reporting to them, an unrelated manager, plus a second workspace for isolation checks.
python3 run_discount_approval.py   -> results.json / results.md next to this file.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("discount_approval", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("disc")
OWNER = W.token
ADMIN = W.add_user("ADMIN", "Dana")
MGR = W.add_user("MANAGER", "Milo", sales_level=2)
REP = W.add_user("AGENT", "Rita", manager_id=MGR["user_id"], sales_level=3)
OTHER = W.add_user("MANAGER", "Otto", sales_level=2)
W2 = Workspace("disc-b")
rid = W.rid


def threshold(value, tok=OWNER):
    return req("PUT", "/quotes/approval-settings", {"discount_approval_threshold": value}, tok)


def new_quote(tok, title, discount):
    s, stages = req("GET", "/sales/stages", None, tok)
    stage = next(x for x in stages if x["status"] == "OPEN")
    s, opp = req("POST", "/opportunities", {"name": f"Deal {title} {rid}", "stage_id": stage["stage_id"]}, tok)
    assert s == 201, (s, opp)
    s, p = req("POST", "/proposals", {"opportunity_id": opp["opportunity_id"], "title": f"{title} {rid}"}, tok)
    assert s == 201, (s, p)
    set_lines(tok, p["proposal_id"], discount)
    return p["proposal_id"]


def set_lines(tok, pid, discount):
    return req("PUT", f"/proposals/{pid}/lines", {"lines": [
        {"description": "Seats", "quantity": 10, "unit_price": 100, "discount_pct": 5},
        {"description": "Onboarding", "quantity": 1, "unit_price": 1000, "discount_pct": discount}]}, tok)


def status(tok, pid, value):
    return req("PATCH", f"/proposals/{pid}", {"status": value}, tok)[0]


def count_notes(user_id, kind):
    return int(sql(f"SELECT COUNT(*) FROM notifications WHERE user_id='{user_id}' AND kind='{kind}'") or 0)


# ── Settings ─────────────────────────────────────────────────
s, b = req("GET", "/quotes/approval-settings", None, OWNER)
R.check("DAS-001", "Threshold is off by default", s == 200 and b.get("discount_approval_threshold") is None, (s, b))
s, b = threshold(10, REP["token"])
R.check("DAS-002", "A rep cannot set the threshold (403)", s == 403, (s, b))
s1, _ = threshold(-1)
s2, _ = threshold(100.5)
s3, _ = threshold("abc")
R.check("DAS-003", "Out-of-range / non-numeric threshold is 422", (s1, s2, s3) == (422, 422, 422), (s1, s2, s3))
s, b = threshold(15, ADMIN["token"])
R.check("DAS-004", "Admin sets the threshold to 15%", s == 200 and b.get("discount_approval_threshold") == 15, (s, b))
s, b = req("PUT", "/sales/settings", {"discount_approval_threshold": 150}, OWNER)
R.check("DAS-005", "General sales settings refuse a threshold over 100 (400)", s == 400, (s, b))

# ── Within the limit ─────────────────────────────────────────
p_ok = new_quote(REP["token"], "Within", 15)
s, a = req("GET", f"/proposals/{p_ok}/approval", None, REP["token"])
R.check("DAS-006", "A 15% discount at a 15% limit needs no approval", s == 200 and a.get("approval_required") is False
        and a.get("can_send") is True, (s, a))
R.check("DAS-007", "That quote can be sent", status(REP["token"], p_ok, "SENT") == 200)

# ── Above the limit: blocked, submit, approve ───────────────
pid = new_quote(REP["token"], "Flow", 25)
s, a = req("GET", f"/proposals/{pid}/approval", None, REP["token"])
R.check("DAS-008", "25% discount: approval required, cannot send, owner can submit",
        s == 200 and a.get("approval_required") and not a.get("can_send") and a.get("can_submit")
        and a.get("effective_discount_pct") == 25, (s, a))
codes = [status(REP["token"], pid, v) for v in ("SENT", "UNDER_REVIEW", "ACCEPTED")]
R.check("DAS-009", "Sent / Under review / Accepted are refused with 409 before approval", codes == [409, 409, 409], codes)
s, b = req("POST", f"/proposals/{pid}/approval/submit", {"note": "x" * 1001}, REP["token"])
R.check("DAS-010", "Submit note over 1000 characters is 422", s == 422, (s, str(b)[:100]))
before = count_notes(MGR["user_id"], "QUOTE_APPROVAL")
s, b = req("POST", f"/proposals/{pid}/approval/submit", {"note": "Strategic logo"}, REP["token"])
R.check("DAS-011", "Owner submits for approval -> PENDING", s == 200 and b.get("approval_status") == "PENDING", (s, b))
R.check("DAS-012", "The owner's manager is notified", count_notes(MGR["user_id"], "QUOTE_APPROVAL") == before + 1)
s, b = req("POST", f"/proposals/{pid}/approval/submit", {}, REP["token"])
R.check("DAS-013", "Submitting twice is 409", s == 409, (s, b))
s, lst = req("GET", "/quotes/approvals", None, MGR["token"])
ids = [i["proposal_id"] for i in (lst or {}).get("items", [])]
R.check("DAS-014", "Manager's 'Awaiting my approval' list has the quote", s == 200 and pid in ids, (s, ids))
s, lst = req("GET", "/quotes/approvals", None, OTHER["token"])
R.check("DAS-015", "An unrelated manager does not see it", s == 200 and pid not in [i["proposal_id"] for i in lst.get("items", [])], (s, lst))
s, _ = req("POST", f"/proposals/{pid}/approval/approve", {}, REP["token"])
s2, _ = req("POST", f"/proposals/{pid}/approval/approve", {}, OTHER["token"])
R.check("DAS-016", "Owner and unrelated manager cannot approve (403/404)", s == 403 and s2 in (403, 404), (s, s2))
s, b = req("POST", f"/proposals/{pid}/approval/approve", {"reason": "OK for this logo"}, MGR["token"])
R.check("DAS-017", "Manager approves with a reason; approver and time recorded",
        s == 200 and b.get("approval_status") == "APPROVED" and b.get("approval_decided_by") == MGR["user_id"]
        and b.get("approval_decided_at") and b.get("approval_reason") == "OK for this logo", (s, b))
R.check("DAS-018", "Owner is notified of the decision", count_notes(REP["user_id"], "QUOTE_DECISION") >= 1)
R.check("DAS-019", "Approved quote can be sent", status(REP["token"], pid, "SENT") == 200)
opp = sql(f"SELECT opportunity_id FROM proposals WHERE proposal_id='{pid}'")
hist = int(sql(f"SELECT COUNT(*) FROM property_changes WHERE object_id='{opp}' AND field='quote_approval'") or 0)
R.check("DAS-020", "Approval steps are in the deal history", hist >= 2, hist)

# ── Edit after approval ─────────────────────────────────────
s, q = set_lines(REP["token"], pid, 18)
R.check("DAS-021", "Lowering the discount keeps the approval", s == 200 and q["approval"]["approval_status"] == "APPROVED", (s, q.get("approval") if isinstance(q, dict) else q))
s, q = set_lines(REP["token"], pid, 30)
R.check("DAS-022", "Raising it above the approved discount resets to PENDING", s == 200
        and q["approval"]["approval_status"] == "PENDING" and not q["approval"]["can_send"], (s, q.get("approval") if isinstance(q, dict) else q))
R.check("DAS-023", "...and the quote cannot be accepted", status(REP["token"], pid, "ACCEPTED") == 409)

# ── Reject ───────────────────────────────────────────────────
s, _ = req("POST", f"/proposals/{pid}/approval/reject", {}, MGR["token"])
s2, _ = req("POST", f"/proposals/{pid}/approval/reject", {"reason": "y" * 1001}, MGR["token"])
R.check("DAS-024", "Reject needs a reason (400) of at most 1000 characters (422)", (s, s2) == (400, 422), (s, s2))
s, b = req("POST", f"/proposals/{pid}/approval/reject", {"reason": "Too deep"}, MGR["token"])
R.check("DAS-025", "Manager rejects with a reason", s == 200 and b.get("approval_status") == "REJECTED", (s, b))
s, b = req("POST", f"/proposals/{pid}/approval/submit", {"note": "Second try"}, REP["token"])
s2, b2 = req("POST", f"/proposals/{pid}/approval/approve", {}, ADMIN["token"])
R.check("DAS-026", "Owner resubmits; an admin can approve", s == 200 and s2 == 200 and b2.get("approval_status") == "APPROVED", (s, s2, b2))

# ── Isolation and bounds ────────────────────────────────────
s1, _ = req("GET", f"/proposals/{pid}/approval", None, W2.token)
s2, _ = req("POST", f"/proposals/{pid}/approval/approve", {}, W2.token)
R.check("DAS-027", "Another workspace gets 404", (s1, s2) == (404, 404), (s1, s2))
s1, _ = req("GET", "/quotes/approvals?status=BOGUS", None, OWNER)
s2, _ = req("GET", "/quotes/approvals?limit=501", None, OWNER)
R.check("DAS-028", "List filter / limit are bounded (422)", (s1, s2) == (422, 422), (s1, s2))

# ── Switch off ───────────────────────────────────────────────
p_off = new_quote(REP["token"], "Off", 60)
blocked = status(REP["token"], p_off, "SENT")
threshold(None)
R.check("DAS-029", "Turning approval off lifts the gate", blocked == 409 and status(REP["token"], p_off, "SENT") == 200, blocked)
s, spec = req("GET", "/openapi.json")
R.check("DAS-030", "Approval endpoints are published in the API spec", s == 200
        and "/proposals/{proposal_id}/approval/approve" in spec.get("paths", {}), s)

R.write()
c = {k: sum(1 for r in R.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")}
print("COUNTS", c)
