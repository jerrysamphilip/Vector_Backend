"""
Functional tests (in-process app + database) for the behaviour-fix round:

  * deal won / lost / stage-change notifications to the owner and their manager (E2E-J1-87b)
  * campaign auto-pause notifications to the campaign owner and workspace admins (E2E-J2-09)
  * D1: built-in starter templates for every sequence step when AI generation is unavailable
  * re-inviting an email whose invitation was revoked (409 only for people who joined)
  * Send Now works in capped batches with a cursor and never jumps to a follow-up mid-run
  * S10: the listed response schemas are nullable where the column is
  * @mention picker limited to people who can see the contact / deal

No OpenAI calls: every AI entry point used here is monkeypatched.
"""
import uuid
from datetime import datetime

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def tenant_bf(client):
    return register_tenant(client, "bf")


def _member(client, tenant_id, label, role="AGENT", manager_id=None):
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@bf{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True, manager_id=manager_id)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "email": email, "headers": auth(login(client, email, password))}


def _owner_id(tenant):
    return sql("SELECT user_id FROM users WHERE email = :e", e=tenant["email"])


def _contact(client, h, label, **kw):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@bfclient{RUN}.example.com", "first_name": label.title(), **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _notes(user_id, kind):
    from app.models.sales_extra import Notification
    with SessionLocal() as db:
        return db.query(Notification).filter(Notification.user_id == user_id, Notification.kind == kind).all()


def _campaign(client, h, label, **kw):
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"{label} {RUN}", **kw})
    assert r.status_code == 201, r.text
    return r.json()["campaign_id"]


def _steps(cid, n):
    from app.models.email_sequence import EmailSequence
    ids = []
    with SessionLocal() as db:
        for i in range(1, n + 1):
            seq = EmailSequence(campaign_id=cid, step_number=i, wait_days=0 if i == 1 else 2)
            db.add(seq)
            db.flush()
            ids.append(seq.sequence_id)
        db.commit()
    return ids


# ── BF-F01 deal stage notifications ──────────────────────────

def test_bf_f01_deal_stage_won_lost_notify_owner_and_manager(client, tenant_bf):
    h = tenant_bf["headers"]
    actor = _owner_id(tenant_bf)
    mgr = _member(client, tenant_bf["tenant_id"], "mgr", role="MANAGER")
    rep = _member(client, tenant_bf["tenant_id"], "rep", manager_id=mgr["user_id"])
    stages = client.get("/sales/stages", headers=h).json()
    open_stages = [s for s in stages if s["status"] == "OPEN"]
    won = next(s for s in stages if s["status"] == "WON")
    lost = next(s for s in stages if s["status"] == "LOST")

    r = client.post("/opportunities", headers=h, json={
        "name": f"Notify deal {RUN}", "owner_id": rep["user_id"], "stage_id": open_stages[0]["stage_id"]})
    assert r.status_code == 201, r.text
    deal_id = r.json()["opportunity_id"]
    # Creating a deal is not a stage change
    assert _notes(rep["user_id"], "DEAL_STAGE") == []

    # The admin moves it: owner + manager hear about it, the admin (actor) does not
    r = client.patch(f"/opportunities/{deal_id}", headers=h, json={"stage_id": open_stages[1]["stage_id"]})
    assert r.status_code == 200, r.text
    for uid in (rep["user_id"], mgr["user_id"]):
        notes = _notes(uid, "DEAL_STAGE")
        assert len(notes) == 1
        assert notes[0].link == f"/app/deals/{deal_id}"
        assert open_stages[1]["name"] in notes[0].title and open_stages[0]["name"] in notes[0].body
    assert _notes(actor, "DEAL_STAGE") == []

    # A PATCH that doesn't change the stage notifies nobody
    assert client.patch(f"/opportunities/{deal_id}", headers=h, json={"next_step": "Call"}).status_code == 200
    assert len(_notes(rep["user_id"], "DEAL_STAGE")) == 1

    # Won (with reason): both notified with the reason
    r = client.patch(f"/opportunities/{deal_id}", headers=h,
                     json={"stage_id": won["stage_id"], "closed_reason": "Great fit"})
    assert r.status_code == 200, r.text
    for uid in (rep["user_id"], mgr["user_id"]):
        notes = _notes(uid, "DEAL_WON")
        assert len(notes) == 1 and "Great fit" in notes[0].body and notes[0].link == f"/app/deals/{deal_id}"

    # The owner marks it lost themselves: only the manager is told
    r = client.patch(f"/opportunities/{deal_id}", headers=rep["headers"],
                     json={"stage_id": lost["stage_id"], "closed_reason": "No budget"})
    assert r.status_code == 200, r.text
    assert _notes(rep["user_id"], "DEAL_LOST") == []
    assert len(_notes(mgr["user_id"], "DEAL_LOST")) == 1

    # Notifications are visible to the owner through the API
    items = client.get("/notifications", headers=rep["headers"]).json()["items"]
    assert any(n["kind"] == "DEAL_WON" and n["link"] == f"/app/deals/{deal_id}" for n in items)


# ── BF-F02 auto-pause notifications ──────────────────────────

def test_bf_f02_auto_pause_notifies_owner_and_admins(client, tenant_bf):
    from app.models.campaign import Campaign
    from app.services import send_safety

    h = tenant_bf["headers"]
    owner = _owner_id(tenant_bf)
    admin = _member(client, tenant_bf["tenant_id"], "adm", role="ADMIN")
    agent = _member(client, tenant_bf["tenant_id"], "agt")
    cid = _campaign(client, h, "Autopause")
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)

    with SessionLocal() as db:
        assert send_safety.auto_pause(db, db.get(Campaign, cid), "hard-bounce rate 9.0% is too high")
        db.commit()
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=cid) == "PAUSED"
    for uid in (owner, admin["user_id"]):
        notes = [n for n in _notes(uid, "CAMPAIGN_AUTO_PAUSED") if n.link == f"/app/campaigns/{cid}"]
        assert len(notes) == 1
        assert "hard-bounce rate 9.0%" in notes[0].body
        assert "Autopause" in notes[0].title
    assert _notes(agent["user_id"], "CAMPAIGN_AUTO_PAUSED") == []

    # Already paused: no second pause, no second notification
    with SessionLocal() as db:
        assert not send_safety.auto_pause(db, db.get(Campaign, cid), "again")
        db.commit()
    assert len([n for n in _notes(owner, "CAMPAIGN_AUTO_PAUSED") if n.link == f"/app/campaigns/{cid}"]) == 1


def test_bf_f03_reputation_safety_switch_pauses_and_notifies(client, tenant_bf):
    from app.services.deliverability_service import deliverability_service
    from app.services.domain_tenancy import get_or_create_tenant_domain

    h = tenant_bf["headers"]
    owner = _owner_id(tenant_bf)
    domain = f"rep-bf{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass",
        "imap_host": "127.0.0.1", "imap_port": 1, "imap_username": "sender",
        "imap_password": "fixture-imap-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    cid = _campaign(client, h, "Reputation", inbox_ids=[r.json()["inbox_id"]])
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)

    with SessionLocal() as db:
        row = get_or_create_tenant_domain(db, tenant_bf["tenant_id"], domain)
        row.current_reputation_score, row.safety_threshold = 40, 70
        db.flush()
        deliverability_service._trigger_safety_switch(row, db)
        db.commit()
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=cid) == "PAUSED"
    # Same pause as the other safety paths: flagged automatic with the reason
    assert sql("SELECT auto_paused FROM campaigns WHERE campaign_id = :c", c=cid) == 1
    assert "reputation score dropped to 40" in sql("SELECT paused_reason FROM campaigns WHERE campaign_id = :c", c=cid)
    notes = [n for n in _notes(owner, "CAMPAIGN_AUTO_PAUSED") if n.link == f"/app/campaigns/{cid}"]
    assert len(notes) == 1 and "reputation score dropped to 40" in notes[0].body


# ── BF-F04..F06 D1 starter templates without AI ──────────────

def _no_ai(monkeypatch, persona):
    import app.services.batch_processor_service as bps

    calls = []

    def fake_llm(**kw):
        calls.append(kw)
        return None  # what generate_sequence_with_llm returns with no key / spend cap hit

    monkeypatch.setattr(bps, "generate_sequence_with_llm", fake_llm)
    monkeypatch.setattr(bps, "classify_prospect", lambda designation, company_name: (persona, 0.9))
    return calls


def _list_with(tenant, pids):
    from app.models import ProspectList, ProspectListMember
    with SessionLocal() as db:
        lst = ProspectList(tenant_id=tenant["tenant_id"], list_name=f"BF list {uuid.uuid4().hex[:6]}",
                           source_type="MANUAL", uploaded_by=_owner_id(tenant))
        db.add(lst)
        db.flush()
        for pid in pids:
            db.add(ProspectListMember(list_id=lst.list_id, prospect_id=pid))
        db.commit()
        return lst.list_id


def test_bf_f04_wizard_builds_template_for_every_step_without_ai(client, tenant_bf, monkeypatch):
    from app.models.email_template import EmailTemplate

    h = tenant_bf["headers"]
    persona = f"NOBP_{RUN}".upper()
    _no_ai(monkeypatch, persona)
    cid = _campaign(client, h, "Wizard fallback")
    seq_ids = _steps(cid, 3)
    list_id = _list_with(tenant_bf, [_contact(client, h, "wiz1"), _contact(client, h, "wiz2")])

    r = client.post(f"/campaigns/{cid}/generations", headers=h, json={
        "campaign_id": cid, "list_id": list_id, "product_name": "Widget",
        "sequence_steps": [{"step_number": 1, "wait_days": 0}, {"step_number": 2, "wait_days": 2},
                           {"step_number": 3, "wait_days": 2}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai_fallback_used"] is True
    assert body["notice"] == "AI unavailable - starter templates were used, please review"
    [result] = body["templates_generated"]
    assert result["fallback_used"] is True and result["fallback_steps"] == [1, 2, 3]
    assert [e["step_number"] for e in result["all_emails"]] == [1, 2, 3]
    assert len({e["subject"] for e in result["all_emails"]}) == 3

    with SessionLocal() as db:
        rows = db.query(EmailTemplate).filter(EmailTemplate.campaign_id == cid).all()
    assert len(rows) == 3  # launch gate: templates >= steps
    assert {t.sequence_id for t in rows} == set(seq_ids)
    assert all(t.is_ai_generated is False and t.designation == persona for t in rows)
    assert all((t.model_settings or {}).get("method") == "BUILTIN_FALLBACK" for t in rows)
    # No CTA link on the campaign: no CTA token in the copy
    assert all("calendar_link" not in (t.body or "") for t in rows)


def test_bf_f05_persona_endpoint_reports_fallback_and_partial_ai_is_completed(client, tenant_bf, monkeypatch):
    import app.services.batch_processor_service as bps
    from app.models import PersonaBlueprint
    from app.models.email_template import EmailTemplate

    h = tenant_bf["headers"]
    persona = f"BFBP_{RUN}".upper()
    with SessionLocal() as db:
        if not db.query(PersonaBlueprint).filter(PersonaBlueprint.persona_type == persona).first():
            db.add(PersonaBlueprint(persona_type=persona, is_active=True))
            db.commit()
    # The AI only wrote step 1 (e.g. the spend cap hit mid-way): steps 2 and 3 get starter copy
    monkeypatch.setattr(bps, "generate_sequence_with_llm",
                        lambda **kw: {"email_1": {"subject": "AI subject", "body": "Hi {{first_name}}, AI body", "cta": ""}})
    cid = _campaign(client, h, "Persona fallback")
    _steps(cid, 3)
    list_id = _list_with(tenant_bf, [])
    r = client.post(f"/campaigns/{cid}/personas/{persona}/generations", headers=h, json={
        "list_id": list_id, "sequence_steps": [{"wait_days": 0}, {"wait_days": 2}, {"wait_days": 3}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fallback_used"] is True and body["fallback_steps"] == [2, 3]
    assert body["notice"] == "AI unavailable - starter templates were used, please review"
    assert [e["is_ai_generated"] for e in body["all_emails"]] == [True, False, False]
    assert body["all_emails"][0]["subject"] == "AI subject"
    with SessionLocal() as db:
        flags = sorted(t.is_ai_generated for t in db.query(EmailTemplate).filter(EmailTemplate.campaign_id == cid))
    assert flags == [False, False, True]


def test_bf_f06_cta_link_puts_cta_token_in_every_starter_template(client, tenant_bf, monkeypatch):
    from app.models.email_template import EmailTemplate
    from app.services.batch_processor_service import generate_template_for_persona

    h = tenant_bf["headers"]
    _no_ai(monkeypatch, "X")
    cid = _campaign(client, h, "CTA fallback")
    step_ids = _steps(cid, 2)
    with SessionLocal() as db:
        result = generate_template_for_persona(
            db, cid, f"NOBP2_{RUN}", [], "Widget", "", [{"wait_days": 0}, {"wait_days": 4}],
            False, False, "COLD_OUTREACH", "https://cal.example.com/meet", True,
            {1: step_ids[0], 2: step_ids[1]})
        db.commit()
    assert result["fallback_used"] is True and result["sequence_count"] == 2
    assert [e["schedule"] for e in result["all_emails"]] == ["Day 1", "Day 5"]
    with SessionLocal() as db:
        rows = db.query(EmailTemplate).filter(EmailTemplate.campaign_id == cid).all()
    assert len(rows) == 2 and all("{{calendar_link}}" in t.body for t in rows)


# ── BF-F07 re-invite after a revoked invitation ──────────────

def test_bf_f07_reinvite_after_revoke_or_deactivation(client, tenant_bf):
    h = tenant_bf["headers"]
    email = f"reinvite.{RUN}@bf{RUN}.example.com"
    r = client.post("/api/users/invite", headers=h, json={"email": email, "role": "AGENT"})
    assert r.status_code == 201, r.text
    inv1, user_id = r.json()["invitation_id"], r.json()["user_id"]
    assert client.delete(f"/api/users/invitations/{inv1}", headers=h).status_code == 200
    assert sql("SELECT status FROM users WHERE user_id = :u", u=user_id) == "INACTIVE"

    # Revoked: inviting again reissues on the same account
    r = client.post("/api/users/invite", headers=h, json={"email": email, "role": "MANAGER"})
    assert r.status_code == 201, r.text
    inv2 = r.json()["invitation_id"]
    assert r.json()["user_id"] == user_id and inv2 != inv1
    assert sql("SELECT status FROM users WHERE user_id = :u", u=user_id) == "INVITED"
    assert sql("SELECT role FROM users WHERE user_id = :u", u=user_id) == "MANAGER"
    assert sql("SELECT status FROM invitations WHERE invitation_id = :i", i=inv1) == "REVOKED"
    assert sql("SELECT status FROM invitations WHERE invitation_id = :i", i=inv2) == "PENDING"
    assert sql("SELECT COUNT(*) FROM users WHERE email = :e", e=email) == 1

    # Invited account deactivated through the team list: re-invite works too
    assert client.delete(f"/api/users/{user_id}", headers=h).status_code == 200
    r = client.post("/api/users/invite", headers=h, json={"email": email, "role": "AGENT"})
    assert r.status_code == 201, r.text
    assert sql("SELECT status FROM invitations WHERE invitation_id = :i", i=inv2) == "REVOKED"

    # Someone who actually joined still gets 409 (active or deactivated)
    member = _member(client, tenant_bf["tenant_id"], "joined")
    assert client.post("/api/users/invite", headers=h, json={"email": member["email"]}).status_code == 409
    joined = f"joined2.{RUN}@bf{RUN}.example.com"
    r = client.post("/api/users/invite", headers=h, json={"email": joined, "role": "AGENT"})
    uid2 = r.json()["user_id"]
    from app.core.security import hash_password
    sql("UPDATE users SET password_hash = :p WHERE user_id = :u", p=hash_password("Joined-pass-123"), u=uid2)
    assert client.post("/api/auth/login", json={"email": joined, "password": "Joined-pass-123"}).status_code == 200
    assert client.delete(f"/api/users/{uid2}", headers=h).status_code == 200
    assert client.post("/api/users/invite", headers=h, json={"email": joined}).status_code == 409


# ── BF-F08 Send Now in capped batches ────────────────────────

def test_bf_f08_send_now_batches_with_cursor(client, tenant_bf, monkeypatch):
    from app.core.config import settings
    from app.models.email_message import EmailMessage
    from app.models.email_template import EmailTemplate
    from app.services.email_sender_service import email_sender

    sent_to = []

    async def fake_send(email_message, email_template, prospect, **kw):
        sent_to.append(email_message.message_id)
        return {"success": True, "ses_message_id": f"ses-{uuid.uuid4().hex}"}

    monkeypatch.setattr(email_sender, "send_with_retry", fake_send)
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)
    h = tenant_bf["headers"]
    cid = _campaign(client, h, "Send now batches")
    s1, s2 = _steps(cid, 2)
    pids = [_contact(client, h, f"sn{i}") for i in range(6)]
    with SessionLocal() as db:
        t1 = EmailTemplate(campaign_id=cid, sequence_id=s1, subject="One", body="Hi")
        t2 = EmailTemplate(campaign_id=cid, sequence_id=s2, subject="Two", body="Again")
        db.add_all([t1, t2])
        db.flush()
        for pid in pids:
            for seq, t, status in ((s1, t1, "QUEUED"), (s2, t2, "SCHEDULED")):
                db.add(EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq, template_id=t.template_id,
                                    direction="OUTBOUND", status=status, subject=t.subject, body_text=t.body,
                                    to_email=f"{pid}@x.example.com", scheduled_at=datetime.utcnow()))
        db.commit()
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)

    r = client.post(f"/campaigns/{cid}/send-now", headers=h, params={"limit": 4})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["sent"] == 4 and first["failed"] == 0 and first["total"] == 4
    assert first["has_more"] is True and first["remaining"] == 2 and first["cursor"] and first["started_at"]

    r = client.post(f"/campaigns/{cid}/send-now", headers=h,
                    params={"limit": 4, "cursor": first["cursor"], "since": first["started_at"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["sent"] == 2 and second["has_more"] is False and second["remaining"] == 0
    assert len(set(sent_to)) == 6
    # Only first steps went: follow-ups that became due during the run are left for the scheduler
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c AND status = 'SENT' "
               "AND sequence_id = :s", c=cid, s=s1) == 6
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c AND sent_at IS NOT NULL "
               "AND sequence_id = :s", c=cid, s=s2) == 0

    assert client.post(f"/campaigns/{cid}/send-now", headers=h, params={"cursor": "garbage"}).status_code == 400
    assert client.post(f"/campaigns/{cid}/send-now", headers=h, params={"limit": 0}).status_code == 422
    assert client.post(f"/campaigns/{cid}/send-now", headers=h, params={"limit": 101}).status_code == 422


# ── BF-F09 S10 nullable schemas in the OpenAPI ───────────────

def test_bf_f09_openapi_marks_nullable_fields(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    def nullable(model, field):
        prop = schemas[model]["properties"][field]
        return prop.get("nullable") or any(o.get("type") == "null" for o in prop.get("anyOf", []))

    assert nullable("CompanyProfileResponse", "is_default")
    assert nullable("SendingInboxResponse", "status") and nullable("SendingInboxResponse", "daily_limit")
    assert nullable("ProspectListResponse", "uploaded_at")
    assert nullable("ProspectInListResponse", "consent_status")
    assert nullable("UserOut", "status")


# ── BF-F10 mention picker scoped to the record ───────────────

def test_bf_f10_mentionable_users_scoped_to_record(client, tenant_bf):
    h = tenant_bf["headers"]
    owner = _owner_id(tenant_bf)
    a1 = _member(client, tenant_bf["tenant_id"], "mena")
    a2 = _member(client, tenant_bf["tenant_id"], "menb")
    pid = _contact(client, h, "scoped", owner_id=a1["user_id"])

    def ids(headers, **params):
        r = client.get("/contacts/mentionable-users", headers=headers, params={"limit": 50, **params})
        assert r.status_code == 200, r.text
        return {u["user_id"] for u in r.json()}

    everyone = ids(h)
    assert {owner, a1["user_id"], a2["user_id"]} <= everyone
    scoped = ids(h, prospect_id=pid)
    assert {owner, a1["user_id"]} <= scoped and a2["user_id"] not in scoped
    assert scoped <= everyone
    # Someone who can't see the contact can't use it to probe
    assert client.get("/contacts/mentionable-users", headers=a2["headers"],
                      params={"prospect_id": pid}).status_code == 404
    assert client.get("/contacts/mentionable-users", headers=h,
                      params={"prospect_id": "x" * 37}).status_code == 422

    stage = next(s for s in client.get("/sales/stages", headers=h).json() if s["status"] == "OPEN")
    r = client.post("/opportunities", headers=h, json={
        "name": f"Mention deal {RUN}", "owner_id": a1["user_id"], "stage_id": stage["stage_id"]})
    assert r.status_code == 201, r.text
    deal_scoped = ids(h, opportunity_id=r.json()["opportunity_id"])
    assert a1["user_id"] in deal_scoped and a2["user_id"] not in deal_scoped
