"""
Regression tests for the defect-fix round (metrics consistency, invitations, repeated
DELETEs, oversize values, sign-offs, planned follow-up dates, automation sends, purge).
"""

from datetime import datetime, timedelta

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant
from app.core.clock import utcnow


@pytest.fixture(scope="module")
def tenant_m(client):
    return register_tenant(client, "m")


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{RUN}@metrics{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


# ── 1. Sent / opened counts agree on every screen ──

def test_sent_and_opened_counts_are_consistent(client, tenant_m):
    from app.models.email_message import EmailEvent, EmailMessage
    from app.models.email_sequence import EmailSequence

    h = tenant_m["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Metrics {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    p1, p2, p3 = (_contact(client, h, n) for n in ("one", "two", "three"))

    now = utcnow()
    with SessionLocal() as db:
        s1 = EmailSequence(campaign_id=cid, step_number=1, wait_days=0)
        s2 = EmailSequence(campaign_id=cid, step_number=2, wait_days=2)
        db.add_all([s1, s2])
        db.flush()

        def msg(pid, seq, status, direction="OUTBOUND", sent=True):
            m = EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq.sequence_id,
                             direction=direction, status=status, to_email="x@example.com",
                             sent_at=now if sent else None)
            db.add(m)
            db.flush()
            return m

        m11 = msg(p1, s1, "SENT")
        m12 = msg(p1, s2, "SENT")
        m21 = msg(p2, s1, "BOUNCED")             # bounced after the send: still sent
        msg(p3, s1, "SENT", direction="INBOUND")  # a reply, not an outbound send
        msg(p3, s1, "QUEUED", sent=False)
        # p1 opens both steps (three open events), p2 opens once
        for m in (m11, m11, m12, m21):
            db.add(EmailEvent(message_id=m.message_id, event_type=EmailEvent.EVENT_OPEN, event_time=now))
        db.commit()

    expected_sent, expected_opened = 3, 2

    m = client.get(f"/campaigns/{cid}/metrics", headers=h).json()
    assert (m["sent_count"], m["opened_count"]) == (expected_sent, expected_opened), m

    detail = client.get(f"/campaigns/{cid}", headers=h).json()["metrics"]
    assert (detail["sent_count"], detail["opened_count"]) == (expected_sent, expected_opened)

    a = client.get(f"/campaigns/{cid}/analytics", headers=h).json()
    assert (a["metrics"]["sent_count"], a["metrics"]["opened_count"]) == (expected_sent, expected_opened)
    steps = {s["step"]: s for s in a["sequences"]}
    assert steps[1]["sent"] == 2 and steps[2]["sent"] == 1
    assert steps[1]["opened_count"] == 2 and steps[2]["opened_count"] == 1

    g = client.get("/reports/global-analytics", headers=h).json()["kpis"]
    assert (g["total_emails_sent"], g["total_opened"]) == (expected_sent, expected_opened)

    comp = client.get("/reports/campaign-comparison", headers=h).json()["items"]
    row = next(i for i in comp if i["campaign_id"] == cid)
    assert (row["sent_count"], row["opened_count"]) == (expected_sent, expected_opened)

    dash = client.get("/reports/dashboard-summary", headers=h).json()
    assert (dash["kpis"]["total_emails_sent"], dash["kpis"]["total_opened"]) == (expected_sent, expected_opened)


def _campaign_with_steps(client, h, label, waits=(0, 2, 2), body="Hi {{first_name}},\n\nBody.\n\nRegards,\n{{signature_block}}"):
    from app.models.email_sequence import EmailSequence
    from app.models.email_template import EmailTemplate

    r = client.post("/campaigns", headers=h, json={"campaign_name": f"{label} {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    with SessionLocal() as db:
        for i, w in enumerate(waits, start=1):
            seq = EmailSequence(campaign_id=cid, step_number=i, wait_days=w)
            db.add(seq)
            db.flush()
            db.add(EmailTemplate(campaign_id=cid, sequence_id=seq.sequence_id, subject=f"Step {i}", body=body))
        db.commit()
    return cid


def _enroll(cid, pid):
    from app.models.campaign import CampaignProspect
    with SessionLocal() as db:
        db.add(CampaignProspect(campaign_id=cid, prospect_id=pid, status="ACTIVE"))
        db.commit()


# ── 3. One sign-off, signed by the campaign sender ──

def test_campaign_email_has_one_sign_off_from_campaign_sender(client, tenant_m):
    from app.models.email_message import EmailMessage
    from app.services.campaign_email_service import CampaignEmailService
    from app.services.email_sender_service import email_sender

    h = tenant_m["headers"]
    cid = _campaign_with_steps(client, h, "Signoff", waits=(0,))
    pid = _contact(client, h, "signoff")
    _enroll(cid, pid)
    with SessionLocal() as db:
        assert CampaignEmailService(db)._preschedule_all_emails(cid) == 1
        db.commit()
        body = db.query(EmailMessage.body_text).filter(EmailMessage.campaign_id == cid).scalar()
    # No campaign sender name set: the campaign creator signs, never the platform name
    assert "Outreach AI" not in body
    assert "M Owner" in body
    # The finalized (HTML) body is recognised as signed, so no second sign-off is appended
    assert email_sender.has_sign_off(body)
    assert not email_sender.has_sign_off("<p>Hi Ann,</p><p>Just checking in.</p>")


# ── 4. Planned follow-ups count from the planned step 1 ──

def test_planned_follow_ups_follow_shifted_first_step(client, tenant_m, monkeypatch):
    from datetime import time

    import app.services.campaign_email_service as ces
    from app.models.campaign import Campaign
    from app.models.email_message import EmailMessage
    from app.models.email_sequence import EmailSequence

    saturday = datetime(2026, 10, 10, 15, 0)  # launch on a Saturday afternoon (UTC)

    monkeypatch.setattr(ces, "utcnow", lambda: saturday)
    h = tenant_m["headers"]
    cid = _campaign_with_steps(client, h, "Weekend")
    pid = _contact(client, h, "weekend")
    _enroll(cid, pid)
    with SessionLocal() as db:
        c = db.get(Campaign, cid)
        c.send_window_start, c.send_window_end, c.campaign_timezone = time(9, 0), time(17, 0), "UTC"
        db.commit()
        assert ces.CampaignEmailService(db)._preschedule_all_emails(cid) == 3
        db.commit()
        planned = [r[0] for r in db.query(EmailMessage.scheduled_at).join(
            EmailSequence, EmailSequence.sequence_id == EmailMessage.sequence_id).filter(
            EmailMessage.campaign_id == cid).order_by(EmailSequence.step_number)]
    step1, step2, step3 = planned
    assert step1 >= datetime(2026, 10, 12)  # moved past the weekend (and the Columbus Day holiday)
    assert step2 - step1 >= timedelta(days=2)
    assert step3 - step2 >= timedelta(days=2)


# ── 5 + 7. Invitations stay pending until joined; repeated DELETEs are 404 ──

def test_invitation_lifecycle_and_repeat_deletes(client, tenant_m):
    from tests.conftest import sql

    h = tenant_m["headers"]
    email = f"invitee.{RUN}@m{RUN}.example.com"
    r = client.post("/api/users/invite", headers=h, json={"email": email, "role": "AGENT"})
    assert r.status_code == 201, r.text
    inv_id, user_id = r.json()["invitation_id"], r.json()["user_id"]
    invs = {i["invitation_id"]: i for i in client.get("/api/users/invitations", headers=h).json()}
    assert invs[inv_id]["status"] == "PENDING"

    # Revoke while pending: the invited account can no longer be used
    assert client.delete(f"/api/users/invitations/{inv_id}", headers=h).status_code == 200
    assert sql("SELECT status FROM users WHERE user_id = :u", u=user_id) == "INACTIVE"
    assert client.delete(f"/api/users/invitations/{inv_id}", headers=h).status_code == 404
    assert client.delete(f"/api/users/{user_id}", headers=h).status_code == 404

    # A second invitee signs in for the first time: the invitation becomes ACCEPTED
    email2 = f"invitee2.{RUN}@m{RUN}.example.com"
    r = client.post("/api/users/invite", headers=h, json={"email": email2, "role": "AGENT"})
    assert r.status_code == 201, r.text
    inv2, user2 = r.json()["invitation_id"], r.json()["user_id"]
    # I-13: the invitee joins with the emailed sign-in link (they have no password)
    raw = f"join-{RUN}-{user2}"
    sql("INSERT INTO magic_login_tokens (token_id, user_id, token_hash, expires_at) "
        "VALUES (UUID(), :u, :t, NOW() + INTERVAL 1 HOUR)",
        u=user2, t=__import__("app.core.security", fromlist=["hash_token"]).hash_token(raw))
    assert client.post("/api/auth/magic-login", json={"token": raw}).status_code == 200
    assert sql("SELECT status FROM invitations WHERE invitation_id = :i", i=inv2) == "ACCEPTED"
    assert client.delete(f"/api/users/invitations/{inv2}", headers=h).status_code == 400

    # Deactivating an active user twice: the second DELETE is a 404
    assert client.delete(f"/api/users/{user2}", headers=h).status_code == 200
    assert client.delete(f"/api/users/{user2}", headers=h).status_code == 404


def test_accept_invite_completes_invited_account(client, tenant_m):
    import secrets

    from app.core.security import hash_token
    from tests.conftest import sql

    h = tenant_m["headers"]
    email = f"accept.{RUN}@m{RUN}.example.com"
    r = client.post("/api/users/invite", headers=h, json={"email": email, "role": "AGENT"})
    assert r.status_code == 201, r.text
    inv_id = r.json()["invitation_id"]
    raw = secrets.token_urlsafe(32)  # stand-in for the emailed link's token
    sql("UPDATE invitations SET token = :t WHERE invitation_id = :i", t=hash_token(raw), i=inv_id)
    r = client.post("/api/users/accept-invite", json={
        "token": raw, "first_name": "Acc", "last_name": "Epted", "password": "Accepted-pass-123"})
    assert r.status_code == 201, r.text
    assert sql("SELECT status FROM invitations WHERE invitation_id = :i", i=inv_id) == "ACCEPTED"
    assert client.post("/api/auth/login", json={"email": email, "password": "Accepted-pass-123"}).status_code == 200


def test_repeat_remove_from_campaign_is_404(client, tenant_m):
    h = tenant_m["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Remove {RUN}"})
    cid = r.json()["campaign_id"]
    pid = _contact(client, h, "removeme")
    _enroll(cid, pid)
    assert client.delete(f"/campaigns/{cid}/prospects/{pid}", headers=h).status_code == 200
    assert client.delete(f"/campaigns/{cid}/prospects/{pid}", headers=h).status_code == 404


# ── 6. Automation mail is not cancelled by the step duplicate guard ──

def test_automation_send_is_not_a_duplicate_of_step_one(client, tenant_m):
    from app.models.automation_rule import AutomationRule
    from app.models.email_message import EmailMessage
    from app.models.email_sequence import EmailSequence
    from app.models.email_template import EmailTemplate
    from app.services.automation_rule_service import automation_service
    from app.services.email_scheduler_service import email_scheduler

    h = tenant_m["headers"]
    cid = _campaign_with_steps(client, h, "Automation", waits=(0,))
    pid = _contact(client, h, "auto")
    with SessionLocal() as db:
        seq = db.query(EmailSequence).filter(EmailSequence.campaign_id == cid).first()
        tpl = db.query(EmailTemplate).filter(EmailTemplate.campaign_id == cid).first()
        to = db.execute(__import__("sqlalchemy").text(
            "SELECT email FROM prospects WHERE prospect_id = :p"), {"p": pid}).scalar()
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq.sequence_id, status="SENT",
                            to_email=to, sent_at=utcnow()))
        rule = AutomationRule(campaign_id=cid, name="Offer", trigger_type="EMAIL_OPENED",
                              action_type="SEND_EMAIL", action_config={"template_id": tpl.template_id})
        db.add(rule)
        db.flush()
        automation_service._action_send_email(rule, pid, db)
        automation_service._action_send_email(rule, pid, db)  # fired twice: still one mail
        db.commit()
        msgs = db.query(EmailMessage).filter(EmailMessage.campaign_id == cid, EmailMessage.status == "QUEUED").all()
        assert len(msgs) == 1
        assert email_scheduler._claim(msgs[0], db) is True
        db.refresh(msgs[0])
        assert msgs[0].status == "SENDING"


# ── 2. Oversize values are a 422, not a 500 ──

def test_oversize_values_are_422(client, tenant_m):
    h = tenant_m["headers"]
    pid = _contact(client, h, "oversize")
    r = client.patch(f"/prospects/{pid}", headers=h, json={"first_name": "A" * 5000})
    assert r.status_code == 422, r.text
    rec = {"email": f"big.{RUN}@oversize{RUN}.example.com", "first_name": "B" * 5000, "status": "ACCEPTED"}
    r = client.post("/uploads/x/confirmations", headers=h,
                    json={"upload_id": "00000000-0000-0000-0000-000000000000", "title": "T", "records": [rec]})
    # The upload re-validates rows: an over-long value is that row's reason, not a failed request
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 0 and "First Name is too long" in r.json()["not_added"][0]["reason"]


# ── 8. Company-only files suggest company fields ──

def test_company_only_import_suggests_company_fields(client, tenant_m):
    csv = "Company,Domain,Industry,City\nAcme,acme.example.com,Software,Austin\n"
    r = client.post("/imports/preview", headers=tenant_m["headers"],
                    files={"file": ("companies.csv", csv.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    m = r.json()["suggested_mapping"]
    assert m == {"Company": "company.name", "Domain": "company.domain",
                 "Industry": "company.industry", "City": "company.city"}
    csv = "Email,Industry\na@x.example.com,Software\n"
    r = client.post("/imports/preview", headers=tenant_m["headers"],
                    files={"file": ("contacts.csv", csv.encode(), "text/csv")})
    assert r.json()["suggested_mapping"]["Industry"] == "contact.industry"


# ── c. Delete forever from Recently deleted ──

def test_purge_deleted_contacts(client, tenant_m, tenant_b):
    from tests.conftest import sql

    h = tenant_m["headers"]
    live = _contact(client, h, "keepme")
    gone = _contact(client, h, "purgeme")
    assert client.delete(f"/contacts/{gone}", headers=h).status_code in (200, 204)
    # Another tenant cannot purge it; a live contact is never purged
    r = client.post("/contacts/purge", headers=tenant_b["headers"], json={"prospect_ids": [gone]})
    assert r.status_code == 200 and r.json()["purged"] == 0
    r = client.post("/contacts/purge", headers=h, json={"prospect_ids": [gone, live]})
    assert r.status_code == 200 and r.json()["purged"] == 1
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=gone) == 0
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=live) == 1
    assert sql("SELECT action FROM audit_logs WHERE entity_id = :p", p=gone) == "PURGE_CONTACT"
