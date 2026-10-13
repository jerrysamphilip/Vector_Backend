"""Quote discount approval (BRD v2.0 BR-SF-17 / RP-103), in-process API tests.

Own workspace: owner (SUPER_ADMIN), an admin, a manager (level 2) with a rep (level 3)
reporting to them, and an unrelated manager. The rep's quotes with a discount above the
workspace threshold cannot be sent / accepted until the manager (or an admin) approves.
"""
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


def _member(client, tenant_id: str, label: str, role: str = "AGENT", manager_id=None, sales_level=None) -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@disc{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True, manager_id=manager_id, sales_level=sales_level)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "email": email, "headers": auth(login(client, email, password))}


@pytest.fixture(scope="module")
def ws(client):
    t = register_tenant(client, "disc")
    tid = t["tenant_id"]
    admin = _member(client, tid, "dadmin", role="ADMIN")
    mgr = _member(client, tid, "dmgr", role="MANAGER", sales_level=2)
    rep = _member(client, tid, "drep", role="AGENT", manager_id=mgr["user_id"], sales_level=3)
    other = _member(client, tid, "dother", role="MANAGER", sales_level=2)
    return {"owner": t, "admin": admin, "mgr": mgr, "rep": rep, "other": other, "tenant_id": tid}


def _set_threshold(client, h, value):
    return client.put("/quotes/approval-settings", headers=h, json={"discount_approval_threshold": value})


def _quote(client, h, discount: float, title: str = "Quote") -> str:
    stage = next(s for s in client.get("/sales/stages", headers=h).json() if s["status"] == "OPEN")
    r = client.post("/opportunities", headers=h, json={"name": f"Deal {title} {RUN}", "stage_id": stage["stage_id"]})
    assert r.status_code == 201, r.text
    r = client.post("/proposals", headers=h, json={"opportunity_id": r.json()["opportunity_id"], "title": f"{title} {RUN}"})
    assert r.status_code == 201, r.text
    pid = r.json()["proposal_id"]
    _lines(client, h, pid, discount)
    return pid


def _lines(client, h, pid, discount):
    r = client.put(f"/proposals/{pid}/lines", headers=h, json={"lines": [
        {"description": "Seats", "quantity": 10, "unit_price": 100, "discount_pct": 5},
        {"description": "Onboarding", "quantity": 1, "unit_price": 1000, "discount_pct": discount}]})
    assert r.status_code == 200, r.text
    return r.json()


def _approval(client, h, pid):
    r = client.get(f"/proposals/{pid}/approval", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_da_001_threshold_setting_admin_only_and_bounded(client, ws):
    """DA-001 threshold: off by default, admin-editable, 0-100 only."""
    oh, rh = ws["owner"]["headers"], ws["rep"]["headers"]
    assert client.get("/quotes/approval-settings", headers=rh).json()["discount_approval_threshold"] is None
    assert _set_threshold(client, rh, 10).status_code == 403
    assert client.put("/quotes/approval-settings", headers=ws["mgr"]["headers"],
                      json={"discount_approval_threshold": 10}).status_code == 403
    for bad in (-1, 100.5, "abc"):
        assert _set_threshold(client, oh, bad).status_code == 422, bad
    r = _set_threshold(client, ws["admin"]["headers"], 15)
    assert r.status_code == 200 and r.json()["discount_approval_threshold"] == 15
    assert client.get("/sales/settings", headers=oh).json()["discount_approval_threshold"] == 15
    # the general settings endpoint validates the key too
    assert client.put("/sales/settings", headers=oh, json={"discount_approval_threshold": 101}).status_code == 400


def test_da_002_discount_within_limit_needs_no_approval(client, ws):
    """DA-002 a quote at or below the threshold can be sent straight away."""
    _set_threshold(client, ws["owner"]["headers"], 15)
    h = ws["rep"]["headers"]
    pid = _quote(client, h, 15, "Within")
    a = _approval(client, h, pid)
    assert a["approval_required"] is False and a["can_send"] is True and a["approval_status"] == "NOT_REQUIRED"
    assert client.post(f"/proposals/{pid}/approval/submit", headers=h, json={}).status_code == 400
    assert client.patch(f"/proposals/{pid}", headers=h, json={"status": "SENT"}).status_code == 200


def test_da_003_full_flow_submit_approve_send(client, ws):
    """DA-003 above the limit: blocked, owner submits, manager notified, approves, owner notified, send works."""
    _set_threshold(client, ws["owner"]["headers"], 15)
    rh, mh = ws["rep"]["headers"], ws["mgr"]["headers"]
    pid = _quote(client, rh, 25, "Flow")
    a = _approval(client, rh, pid)
    assert a["approval_required"] and not a["can_send"] and a["can_submit"] and a["effective_discount_pct"] == 25
    for status in ("SENT", "UNDER_REVIEW", "ACCEPTED"):
        r = client.patch(f"/proposals/{pid}", headers=rh, json={"status": status})
        assert r.status_code == 409, (status, r.text)
    assert sql("SELECT status FROM proposals WHERE proposal_id = :p", p=pid) == "DRAFT"

    # Unbounded note refused; then submit
    assert client.post(f"/proposals/{pid}/approval/submit", headers=rh, json={"note": "x" * 1001}).status_code == 422
    r = client.post(f"/proposals/{pid}/approval/submit", headers=rh, json={"note": "Strategic logo"})
    assert r.status_code == 200, r.text
    assert r.json()["approval_status"] == "PENDING" and r.json()["can_decide"] is False
    assert client.post(f"/proposals/{pid}/approval/submit", headers=rh, json={}).status_code == 409
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "SENT"}).status_code == 409
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'QUOTE_APPROVAL'",
               u=ws["mgr"]["user_id"]) >= 1

    # Awaiting my approval: the manager sees it, the unrelated manager does not
    mine = client.get("/quotes/approvals", headers=mh).json()
    assert pid in [i["proposal_id"] for i in mine["items"]]
    assert next(i for i in mine["items"] if i["proposal_id"] == pid)["can_decide"] is True
    assert pid not in [i["proposal_id"] for i in client.get("/quotes/approvals", headers=ws["other"]["headers"]).json()["items"]]
    assert pid in [i["proposal_id"] for i in client.get("/quotes/approvals", headers=rh, params={"scope": "requested"}).json()["items"]]
    # The rep cannot approve their own quote; the unrelated manager cannot either
    assert client.post(f"/proposals/{pid}/approval/approve", headers=rh, json={}).status_code == 403
    assert client.post(f"/proposals/{pid}/approval/approve", headers=ws["other"]["headers"], json={}).status_code in (403, 404)

    r = client.post(f"/proposals/{pid}/approval/approve", headers=mh, json={"reason": "OK for this logo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval_status"] == "APPROVED" and body["approval_decided_by"] == ws["mgr"]["user_id"]
    assert body["approval_decided_at"] and body["approval_reason"] == "OK for this logo" and body["can_send"]
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'QUOTE_DECISION'",
               u=ws["rep"]["user_id"]) >= 1
    assert client.post(f"/proposals/{pid}/approval/approve", headers=mh, json={}).status_code == 409
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "SENT"}).status_code == 200

    # History rows on the deal
    opp = sql("SELECT opportunity_id FROM proposals WHERE proposal_id = :p", p=pid)
    assert sql("SELECT COUNT(*) FROM property_changes WHERE object_id = :o AND field = 'quote_approval'", o=opp) >= 2


def test_da_004_reject_requires_reason_and_resubmit(client, ws):
    """DA-004 reject needs a bounded reason; the owner may resubmit; an admin can decide."""
    _set_threshold(client, ws["owner"]["headers"], 15)
    rh, mh = ws["rep"]["headers"], ws["mgr"]["headers"]
    pid = _quote(client, rh, 40, "Reject")
    assert client.post(f"/proposals/{pid}/approval/submit", headers=rh).status_code == 200
    assert client.post(f"/proposals/{pid}/approval/reject", headers=mh, json={}).status_code == 400
    assert client.post(f"/proposals/{pid}/approval/reject", headers=mh, json={"reason": "y" * 1001}).status_code == 422
    r = client.post(f"/proposals/{pid}/approval/reject", headers=mh, json={"reason": "Too deep"})
    assert r.status_code == 200 and r.json()["approval_status"] == "REJECTED" and r.json()["approval_reason"] == "Too deep"
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "ACCEPTED"}).status_code == 409
    assert _approval(client, rh, pid)["can_submit"] is True
    assert client.post(f"/proposals/{pid}/approval/submit", headers=rh, json={"note": "Second try"}).status_code == 200
    r = client.post(f"/proposals/{pid}/approval/approve", headers=ws["admin"]["headers"], json={})
    assert r.status_code == 200 and r.json()["approval_status"] == "APPROVED"


def test_da_005_raising_discount_after_approval_resets_to_pending(client, ws):
    """DA-005 editing lines above the approved discount resets to PENDING; lowering keeps the approval."""
    _set_threshold(client, ws["owner"]["headers"], 15)
    rh, mh = ws["rep"]["headers"], ws["mgr"]["headers"]
    pid = _quote(client, rh, 20, "Reset")
    client.post(f"/proposals/{pid}/approval/submit", headers=rh)
    assert client.post(f"/proposals/{pid}/approval/approve", headers=mh, json={}).status_code == 200
    q = _lines(client, rh, pid, 18)
    assert q["approval"]["approval_status"] == "APPROVED" and q["approval"]["can_send"]
    q = _lines(client, rh, pid, 30)
    assert q["approval"]["approval_status"] == "PENDING" and not q["approval"]["can_send"]
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "SENT"}).status_code == 409
    # Back within the limit: the request is withdrawn and the quote may go out
    q = _lines(client, rh, pid, 10)
    assert q["approval"]["approval_status"] == "NOT_REQUIRED" and q["approval"]["can_send"]
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "SENT"}).status_code == 200


def test_da_006_tenant_isolation_and_visibility(client, ws, tenant_b):
    """DA-006 another workspace cannot read or decide; unknown ids are 404."""
    _set_threshold(client, ws["owner"]["headers"], 15)
    rh = ws["rep"]["headers"]
    pid = _quote(client, rh, 50, "Iso")
    client.post(f"/proposals/{pid}/approval/submit", headers=rh)
    bh = tenant_b["headers"]
    assert client.get(f"/proposals/{pid}/approval", headers=bh).status_code == 404
    assert client.post(f"/proposals/{pid}/approval/approve", headers=bh, json={}).status_code == 404
    assert client.post(f"/proposals/{pid}/approval/reject", headers=bh, json={"reason": "no"}).status_code == 404
    assert pid not in [i["proposal_id"] for i in client.get("/quotes/approvals", headers=bh).json()["items"]]
    assert client.get(f"/proposals/{uuid.uuid4()}/approval", headers=rh).status_code == 404
    assert client.get("/quotes/approvals", headers=rh, params={"status": "BOGUS"}).status_code == 422
    assert client.get("/quotes/approvals", headers=rh, params={"limit": 501}).status_code == 422


def test_da_007_threshold_off_lifts_the_gate(client, ws):
    """DA-007 turning approval off lets a pending quote go out."""
    oh, rh = ws["owner"]["headers"], ws["rep"]["headers"]
    _set_threshold(client, oh, 15)
    pid = _quote(client, rh, 60, "Off")
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "SENT"}).status_code == 409
    assert _set_threshold(client, oh, None).json()["discount_approval_threshold"] is None
    assert client.patch(f"/proposals/{pid}", headers=rh, json={"status": "SENT"}).status_code == 200
    _set_threshold(client, oh, 15)


def test_da_008_admins_asked_when_owner_has_no_manager(client, ws):
    """DA-008 an owner without managers: the workspace admins are asked; no self-approval for an ADMIN."""
    oh = ws["owner"]["headers"]
    _set_threshold(client, oh, 15)
    pid = _quote(client, ws["admin"]["headers"], 35, "AdminOwned")
    assert client.post(f"/proposals/{pid}/approval/submit", headers=ws["admin"]["headers"]).status_code == 200
    owner_id = sql("SELECT user_id FROM users WHERE email = :e", e=ws["owner"]["email"])
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'QUOTE_APPROVAL'", u=owner_id) >= 1
    # the admin cannot approve their own quote; the SUPER_ADMIN can
    assert client.post(f"/proposals/{pid}/approval/approve", headers=ws["admin"]["headers"], json={}).status_code == 403
    assert client.post(f"/proposals/{pid}/approval/approve", headers=oh, json={}).status_code == 200
