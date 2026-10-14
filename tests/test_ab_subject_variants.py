"""A/B testing of email subject lines (BRD v2.0 BR-SF-18 / RP-104), in-process API tests.

Own workspace: the owner (SUPER_ADMIN, campaign creator), a MANAGER who did not create the
campaign and an AGENT without manage_campaigns, plus an unrelated workspace. A two-step campaign gets subject
variants A/B on step 1; emails are pre-scheduled with the real planner
(CampaignEmailService._preschedule_all_emails), "sent" and opened/clicked/replied through
direct DB writes (the scheduler and SES are not started in tests).

The tests in this module build on each other and run in file order.
"""
import uuid
from collections import Counter
from datetime import datetime

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql

STATE: dict = {}


def _member(client, tenant_id: str, label: str, role: str, custom_permissions=None) -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@abt{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True, custom_permissions=custom_permissions)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


def _enroll(client, h, campaign_id: str, names) -> list:
    """Create contacts through the API and enroll them directly (ACTIVE)."""
    from app.models.campaign import CampaignProspect

    ids = []
    for name in names:
        r = client.post("/contacts", headers=h, json={
            "email": f"{name.lower()}.{RUN}@abclient{RUN}.example.com", "first_name": name})
        assert r.status_code == 201, r.text
        ids.append(r.json()["prospect_id"])
    with SessionLocal() as db:
        for pid in ids:
            db.add(CampaignProspect(campaign_id=campaign_id, prospect_id=pid, current_step=1, status="ACTIVE"))
        db.commit()
    return ids


def _preschedule(campaign_id: str) -> int:
    from app.services.campaign_email_service import CampaignEmailService

    with SessionLocal() as db:
        n = CampaignEmailService(db)._preschedule_all_emails(campaign_id)
        db.commit()
    return n


def _messages(sequence_id: str) -> list:
    from app.models.email_message import EmailMessage

    with SessionLocal() as db:
        rows = db.query(EmailMessage).filter(EmailMessage.sequence_id == sequence_id).order_by(
            EmailMessage.prospect_id).all()
        return [{"id": m.message_id, "prospect_id": m.prospect_id, "label": m.subject_variant,
                 "subject": m.subject, "status": m.status} for m in rows]


def _mark_sent(message_ids) -> None:
    for mid in message_ids:
        sql("UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE message_id=:m", m=mid)


def _event(message_id: str, event_type: int) -> None:
    from app.models.email_message import EmailEvent

    with SessionLocal() as db:
        db.add(EmailEvent(message_id=message_id, event_type=event_type, event_time=datetime.utcnow()))
        db.commit()


@pytest.fixture(scope="module")
def ab(client):
    owner = register_tenant(client, "abt")
    other = register_tenant(client, "abx")
    h = owner["headers"]
    manager = _member(client, owner["tenant_id"], "abmgr", "MANAGER")
    # AGENT holds manage_campaigns by default; this one has it revoked to exercise the permission check
    agent = _member(client, owner["tenant_id"], "abagent", "AGENT", custom_permissions=[])

    r = client.post("/campaigns", headers=h, json={"campaign_name": f"AB Campaign {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    seqs = {}
    for step in (1, 2):
        r = client.post(f"/campaigns/{cid}/sequences", headers=h, json={"step_number": step, "wait_days": step - 1})
        assert r.status_code == 201, r.text
        seqs[step] = r.json()["sequence_id"]
        r = client.post("/templates", headers=h, json={
            "campaign_id": cid, "sequence_id": seqs[step],
            "subject": f"Template {step} for {{{{first_name}}}}", "body": f"Body of step {step}"})
        assert r.status_code == 201, r.text
        STATE[f"template_{step}"] = r.json()["template_id"]
    base = f"/campaigns/{cid}/sequences"
    return {"owner": owner, "other": other, "h": h, "manager": manager, "agent": agent, "cid": cid,
            "seq": seqs, "url1": f"{base}/1/subject-variants", "url2": f"{base}/2/subject-variants"}


# ── AB-F01 variant CRUD and validation ───────────────────────

def test_ab_f01_create_variants_and_validation(client, ab):
    h, url = ab["h"], ab["url1"]
    r = client.get(url, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["variants"] == [] and r.json()["ab_active"] is False

    r = client.post(url, headers=h, json={"subject": "Alpha for {{first_name}}"})
    assert r.status_code == 201, r.text
    assert r.json()["label"] == "A" and r.json()["weight"] == 1
    r = client.post(url, headers=h, json={"subject": "Bravo {{first_name}}", "weight": 1})
    assert r.status_code == 201, r.text
    assert r.json()["label"] == "B"
    listing = client.get(url, headers=h).json()
    assert [v["label"] for v in listing["variants"]] == ["A", "B"] and listing["ab_active"] is True

    # bounds and subject checks
    for body in ({"subject": ""}, {"subject": "   "}, {"subject": "x" * 501},
                 {"subject": "ok", "weight": 0}, {"subject": "ok", "weight": 101}, {"weight": 2}):
        assert client.post(url, headers=h, json=body).status_code == 422, body
    assert client.post(url, headers=h, json={"subject": "Hello\nBcc: x@y.z"}).status_code == 422
    r = client.post(url, headers=h, json={"subject": "Claim your prize today"})
    assert r.status_code == 400 and "disallowed" in r.json()["detail"].lower()
    assert client.post(url, headers=h, json={"subject": "Alpha for {{first_name}}"}).status_code == 409

    # at most four variants
    assert client.post(url, headers=h, json={"subject": "Charlie"}).status_code == 201
    r = client.post(url, headers=h, json={"subject": "DELTA OFFER!!"})
    assert r.status_code == 201 and r.json()["label"] == "D" and r.json()["warnings"]  # lint warnings returned
    assert client.post(url, headers=h, json={"subject": "Echo"}).status_code == 409
    # unsent variants can be deleted
    assert client.delete(f"{url}/C", headers=h).status_code == 204
    assert client.delete(f"{url}/d", headers=h).status_code == 204
    assert client.delete(f"{url}/C", headers=h).status_code == 404
    assert client.patch(f"{url}/E", headers=h, json={"weight": 2}).status_code == 422
    assert client.patch(f"{url}/A", headers=h, json={}).status_code == 422
    assert [v["label"] for v in client.get(url, headers=h).json()["variants"]] == ["A", "B"]


# ── AB-F02 tenancy and permissions ───────────────────────────

def test_ab_f02_tenant_and_permission_checks(client, ab):
    url = ab["url1"]
    assert client.get(url, headers=ab["agent"]["headers"]).status_code == 200
    assert client.get(url + "/stats", headers=ab["agent"]["headers"]).status_code == 200
    assert client.post(url, headers=ab["agent"]["headers"], json={"subject": "Agent try"}).status_code == 403
    assert client.patch(f"{url}/A", headers=ab["agent"]["headers"], json={"weight": 3}).status_code == 403
    assert client.delete(f"{url}/A", headers=ab["agent"]["headers"]).status_code == 403
    other = ab["other"]["headers"]
    assert client.get(url, headers=other).status_code == 404
    assert client.get(url + "/stats", headers=other).status_code == 404
    assert client.post(url, headers=other, json={"subject": "Foreign"}).status_code == 404
    assert client.get(f"/campaigns/{ab['cid']}/sequences/5/subject-variants", headers=ab["h"]).status_code == 404
    assert client.get(f"/campaigns/{ab['cid']}/sequences/8/subject-variants", headers=ab["h"]).status_code == 422
    assert client.get(url, headers=auth("not-a-jwt")).status_code == 401


# ── AB-F03 pre-scheduling assigns variants evenly and keeps them ─

def test_ab_f03_preschedule_assigns_variants_evenly(client, ab):
    STATE["prospects"] = _enroll(client, ab["h"], ab["cid"], [f"Ann{i}" for i in range(8)])
    assert _preschedule(ab["cid"]) == 16
    step1 = _messages(ab["seq"][1])
    assert Counter(m["label"] for m in step1) == {"A": 4, "B": 4}
    for m in step1:
        expected_prefix = "Alpha for Ann" if m["label"] == "A" else "Bravo Ann"
        assert m["subject"].startswith(expected_prefix), m
    # a step without variants keeps the template subject and no label
    step2 = _messages(ab["seq"][2])
    assert all(m["label"] is None and m["subject"].startswith("Template 2 for Ann") for m in step2)
    # re-planning creates nothing and keeps every assignment
    before = {m["id"]: m["label"] for m in step1}
    assert _preschedule(ab["cid"]) == 0
    assert {m["id"]: m["label"] for m in _messages(ab["seq"][1])} == before


def test_ab_f04_later_enrollments_keep_the_split_even(client, ab):
    _enroll(client, ab["h"], ab["cid"], ["Bob1", "Bob2"])
    assert _preschedule(ab["cid"]) == 4
    assert Counter(m["label"] for m in _messages(ab["seq"][1])) == {"A": 5, "B": 5}


def test_ab_f05_template_edit_keeps_variant_subjects(client, ab):
    r = client.put(f"/templates/{STATE['template_1']}", headers=ab["h"], json={"subject": "New template 1 subject"})
    assert r.status_code == 200, r.text
    r = client.put(f"/templates/{STATE['template_2']}", headers=ab["h"], json={"subject": "New template 2 subject"})
    assert r.status_code == 200, r.text
    assert all(m["subject"].startswith(("Alpha for", "Bravo")) for m in _messages(ab["seq"][1]))
    assert all(m["subject"] == "New template 2 subject" for m in _messages(ab["seq"][2]))


# ── AB-F06 sent emails never change ──────────────────────────

def test_ab_f06_variants_cannot_change_sent_emails(client, ab):
    h, url = ab["h"], ab["url1"]
    step1 = _messages(ab["seq"][1])
    a_ids = [m["id"] for m in step1 if m["label"] == "A"]
    b_ids = [m["id"] for m in step1 if m["label"] == "B"]
    STATE["a_ids"], STATE["b_ids"] = a_ids, b_ids
    _mark_sent(a_ids[:3])
    sent_subjects = {m["id"]: m["subject"] for m in _messages(ab["seq"][1]) if m["id"] in a_ids[:3]}

    r = client.patch(f"{url}/A", headers=h, json={"subject": "Alpha rewritten"})
    assert r.status_code == 409 and "already sent" in r.json()["detail"]
    assert client.delete(f"{url}/A", headers=h).status_code == 409
    # weight may change; only unsent emails are re-balanced, sent ones keep their subject
    r = client.patch(f"{url}/A", headers=h, json={"weight": 2})
    assert r.status_code == 200 and r.json()["weight"] == 2
    r = client.patch(f"{url}/A", headers=h, json={"weight": 1})
    assert r.status_code == 200
    after = {m["id"]: m for m in _messages(ab["seq"][1])}
    assert all(after[mid]["subject"] == s and after[mid]["label"] == "A" for mid, s in sent_subjects.items())

    # an unsent variant's subject can change: its unsent emails are re-rendered
    r = client.patch(f"{url}/B", headers=h, json={"subject": "Bravo again {{first_name}}"})
    assert r.status_code == 200, r.text
    b_now = [m for m in _messages(ab["seq"][1]) if m["label"] == "B"]
    assert b_now and all(m["subject"].startswith("Bravo again ") for m in b_now)
    STATE["b_ids"] = [m["id"] for m in b_now]
    STATE["a_ids"] = [m["id"] for m in _messages(ab["seq"][1]) if m["label"] == "A"]


# ── AB-F07 stats, winner and promotion ───────────────────────

def test_ab_f07_stats_per_variant(client, ab):
    from app.models.email_message import EmailEvent

    a_sent = [mid for mid in STATE["a_ids"] if sql("SELECT status FROM email_messages WHERE message_id=:m", m=mid) == "SENT"]
    assert len(a_sent) == 3
    b_sent = STATE["b_ids"][:3]
    _mark_sent(b_sent)
    STATE["b_sent"] = b_sent
    _event(a_sent[0], EmailEvent.EVENT_OPEN)
    _event(a_sent[0], EmailEvent.EVENT_OPEN)   # second open by the same prospect counts once
    _event(a_sent[1], EmailEvent.EVENT_OPEN)
    _event(a_sent[0], EmailEvent.EVENT_CLICK)
    _event(b_sent[0], EmailEvent.EVENT_OPEN)
    _event(b_sent[0], EmailEvent.EVENT_REPLY)

    r = client.get(ab["url1"] + "/stats", headers=ab["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    rows = {v["label"]: v for v in body["variants"]}
    assert (rows["A"]["sent"], rows["A"]["opened"], rows["A"]["clicked"], rows["A"]["replied"]) == (3, 2, 1, 0)
    assert (rows["B"]["sent"], rows["B"]["opened"], rows["B"]["clicked"], rows["B"]["replied"]) == (3, 1, 0, 1)
    assert rows["A"]["open_rate"] == 66.67 and rows["A"]["click_rate"] == 50.0 and rows["B"]["reply_rate"] == 33.33
    assert rows["A"]["scheduled"] + rows["B"]["scheduled"] == 4
    assert body["min_sends_per_variant"] == 30
    assert body["winner"]["status"] == "insufficient_data" and body["winner"]["label"] is None


def test_ab_f08_promotion_requires_a_winner_and_the_owner(client, ab, monkeypatch):
    from app.services import ab_testing

    url = ab["url1"]
    assert client.post(f"{url}/A/promote", headers=ab["h"]).status_code == 409  # 3 sends < 30
    monkeypatch.setattr(ab_testing, "MIN_SENDS_PER_VARIANT", 3)
    stats = client.get(url + "/stats", headers=ab["h"]).json()
    assert stats["winner"]["status"] == "winner" and stats["winner"]["label"] == "A"
    assert client.post(f"{url}/B/promote", headers=ab["h"]).status_code == 409   # not the winner
    assert client.post(f"{url}/A/promote", headers=ab["manager"]["headers"]).status_code == 403  # not the owner
    assert client.post(f"{url}/A/promote", headers=ab["agent"]["headers"]).status_code == 403
    assert client.post(f"{url}/A/promote", headers=ab["other"]["headers"]).status_code == 404

    sent_before = {m["id"]: m["subject"] for m in _messages(ab["seq"][1]) if m["status"] == "SENT"}
    r = client.post(f"{url}/A/promote", headers=ab["h"])
    assert r.status_code == 200, r.text
    assert r.json()["promoted_label"] == "A" and r.json()["updated_messages"] == 2  # the two unsent B emails
    step1 = _messages(ab["seq"][1])
    for m in step1:
        if m["id"] in sent_before:
            assert m["subject"] == sent_before[m["id"]]  # sent history unchanged
        else:
            assert m["label"] == "A" and m["subject"].startswith("Alpha for ")
    stats = client.get(url + "/stats", headers=ab["h"]).json()
    assert stats["winner"]["status"] == "promoted" and stats["winner"]["label"] == "A"
    assert {v["label"]: v["is_winner"] for v in stats["variants"]} == {"A": True, "B": False}
    # the test is closed
    assert client.post(url, headers=ab["h"], json={"subject": "Late variant"}).status_code == 409
    assert client.patch(f"{url}/B", headers=ab["h"], json={"weight": 3}).status_code == 409
    assert client.post(f"{url}/A/promote", headers=ab["h"]).status_code == 409
    # new enrollments get the winner
    _enroll(client, ab["h"], ab["cid"], ["Cat1"])
    _preschedule(ab["cid"])
    newest = [m for m in _messages(ab["seq"][1]) if m["subject"].startswith("Alpha for Cat1")]
    assert len(newest) == 1 and newest[0]["label"] == "A"


# ── AB-F09 sent history shows the variant ────────────────────

def test_ab_f09_messages_and_schedule_show_the_variant(client, ab):
    url = ab["url1"]
    r = client.get(url + "/messages", headers=ab["h"], params={"limit": 200})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert r.json()["total"] == len(items) == 11
    assert items[0]["sent_at"] is not None  # sent history first
    assert {i["subject_variant"] for i in items if i["message_id"] in STATE["b_sent"]} == {"B"}
    only_b = client.get(url + "/messages", headers=ab["h"], params={"label": "b"}).json()
    assert only_b["total"] == 3 and all(i["subject_variant"] == "B" for i in only_b["items"])
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"label": "Z"}):
        assert client.get(url + "/messages", headers=ab["h"], params=params).status_code == 422
    schedule = client.get(f"/campaigns/{ab['cid']}/schedule", headers=ab["h"]).json()
    labels = {p.get("subject_variant") for item in schedule["schedule"] for p in item["prospects"]
              if item["step_number"] == 1}
    assert labels & {"A", "B"}


# ── AB-F10 variants added after scheduling; removing the test ─

def test_ab_f10_variants_on_scheduled_step_and_back_to_template(client, ab):
    h, url = ab["h"], ab["url2"]
    assert client.post(url, headers=h, json={"subject": "Step two A"}).status_code == 201
    assert all(m["label"] is None for m in _messages(ab["seq"][2]))  # one variant: no test yet
    r = client.post(url, headers=h, json={"subject": "Step two B"})
    assert r.status_code == 201 and r.json()["updated_messages"] == 11
    step2 = _messages(ab["seq"][2])
    assert Counter(m["label"] for m in step2) in (Counter({"A": 6, "B": 5}), Counter({"A": 5, "B": 6}))
    assert all(m["subject"] == f"Step two {m['label']}" for m in step2)
    # weights 3:1 re-balance the unsent emails
    assert client.patch(f"{url}/A", headers=h, json={"weight": 3}).status_code == 200
    assert Counter(m["label"] for m in _messages(ab["seq"][2]))["A"] == 8
    # back to one variant: unsent emails return to the template subject
    assert client.delete(f"{url}/B", headers=h).status_code == 204
    assert all(m["label"] is None and m["subject"] == "New template 2 subject" for m in _messages(ab["seq"][2]))
