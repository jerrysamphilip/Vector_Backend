"""Review fixes, in-process against MySQL:

- T-2 send-now claims stamp claimed_at; the stuck-SENDING sweep only reaps stale claims
- T-2 import: list memberships commit with each chunk (a failure mid-import keeps them)
- T-5 fixed query budgets: profile, pipeline board, contacts / campaigns / opportunities lists
- T-1 no DB transaction during the (stubbed) LLM / token-refresh calls
- T-3 optimistic concurrency on PATCH /prospects/{id} and the wizard activation
"""
import io
import json
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, engine, register_tenant, sql
from tests.query_budget import queries_for
from app.core.clock import utcnow


@pytest.fixture(scope="module")
def tenant_rf(client):
    return register_tenant(client, "rf")


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@rfclient{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _campaign(client, h, label):
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"{label} {uuid.uuid4().hex[:6]} {RUN}"})
    assert r.status_code == 201, r.text
    return r.json()


def _active_inbox(client, h, label):
    """An ACTIVE sending inbox: campaign mail is never sent from the platform address (item 12)."""
    domain = f"{label}-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass",
        "imap_host": "127.0.0.1", "imap_port": 1, "imap_username": "sender",
        "imap_password": "fixture-imap-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    return r.json()["inbox_id"]


def assert_no_open_transaction(where: str) -> None:
    """No pooled connection is checked out, i.e. no session holds a transaction (T-1)."""
    assert engine.pool.checkedout() == 0, f"DB connection checked out during {where}"


# ── T-2 send-now vs the stuck-SENDING sweep ──────────────────

def _queued_message(cid, pid, status="QUEUED", claimed_at=None):
    from app.models.email_message import EmailMessage
    with SessionLocal() as db:
        msg = EmailMessage(campaign_id=cid, prospect_id=pid, direction="OUTBOUND", status=status,
                           subject="Hi", body_text="Hello", to_email=f"{pid}@x.example.com",
                           scheduled_at=utcnow(), claimed_at=claimed_at)
        db.add(msg)
        db.commit()
        return msg.message_id


def test_send_now_claim_is_dated_and_sweep_leaves_it_alone(client, tenant_rf, monkeypatch):
    import asyncio
    from app.core.config import settings
    from app.models.campaign import Campaign
    from app.models.email_message import EmailMessage
    from app.models.email_template import EmailTemplate
    from app.services.campaign_send_now_service import CampaignSendNowService, _Tally
    from app.services.email_scheduler_service import email_scheduler
    from app.services.email_sender_service import email_sender

    h = tenant_rf["headers"]
    cid = _campaign(client, h, "Send now race")["campaign_id"]
    sql("INSERT INTO campaign_inboxes (campaign_id, inbox_id) VALUES (:c, :i)", c=cid, i=_active_inbox(client, h, "race"))
    pid = _contact(client, h, "race")
    with SessionLocal() as db:
        t = EmailTemplate(campaign_id=cid, subject="Hi", body="Hello")
        db.add(t)
        db.commit()
        template_id = t.template_id
    mid = _queued_message(cid, pid)
    sql("UPDATE email_messages SET template_id = :t WHERE message_id = :m", t=template_id, m=mid)
    seen = {}

    async def fake_send(email_message, email_template, prospect, **kw):
        # While SES "runs", the background sweep fires: the fresh claim must survive it
        seen["claimed_at"] = sql("SELECT claimed_at FROM email_messages WHERE message_id = :m", m=mid)
        with SessionLocal() as other:
            email_scheduler._recover_stuck_sends(other)
        seen["status"] = sql("SELECT status FROM email_messages WHERE message_id = :m", m=mid)
        return {"success": True, "ses_message_id": f"ses-{uuid.uuid4().hex}"}

    monkeypatch.setattr(email_sender, "send_with_retry", fake_send)
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)
    with SessionLocal() as db:
        campaign = db.query(Campaign).filter(Campaign.campaign_id == cid).one()
        msg = db.query(EmailMessage).filter(EmailMessage.message_id == mid).one()
        asyncio.run(CampaignSendNowService(db)._send_one(campaign, msg, None, _Tally()))
    assert seen["claimed_at"] is not None
    assert seen["status"] == "SENDING"
    assert sql("SELECT status FROM email_messages WHERE message_id = :m", m=mid) == "SENT"


def test_stuck_sweep_reaps_only_stale_claims_and_dates_legacy_rows(client, tenant_rf):
    from app.services.email_scheduler_service import email_scheduler

    h = tenant_rf["headers"]
    cid = _campaign(client, h, "Sweep")["campaign_id"]
    pid = _contact(client, h, "sweep")
    stale_at = utcnow() - timedelta(minutes=email_scheduler.STUCK_SENDING_MINUTES + 5)
    fresh = _queued_message(cid, pid, "SENDING", utcnow())
    stale = _queued_message(cid, pid, "SENDING", stale_at)
    legacy = _queued_message(cid, pid, "SENDING", None)

    with SessionLocal() as db:
        email_scheduler._recover_stuck_sends(db)
    status = lambda m: sql("SELECT status FROM email_messages WHERE message_id = :m", m=m)  # noqa: E731
    assert status(fresh) == "SENDING"
    assert status(stale) == "FAILED"
    # No claim time: dated now, not reaped on sight (it may be in flight)
    assert status(legacy) == "SENDING"
    assert sql("SELECT claimed_at FROM email_messages WHERE message_id = :m", m=legacy) is not None

    sql("UPDATE email_messages SET claimed_at = :t WHERE message_id = :m", t=stale_at, m=legacy)
    with SessionLocal() as db:
        email_scheduler._recover_stuck_sends(db)
    assert status(legacy) == "FAILED"  # still SENDING a full threshold later: reaped
    assert status(fresh) == "SENDING"


def test_stuck_sweep_with_nothing_to_date_leaves_no_locks_for_the_lane_claims(client, tenant_rf):
    """The batch session runs the sweep and stays open while the lanes claim messages in their own sessions.
    A zero-row 'date undated claims' UPDATE must not keep its next-key locks on the SENDING range (it did:
    every claim waited until the read timeout)."""
    from sqlalchemy import text

    from app.services.email_scheduler_service import email_scheduler

    h = tenant_rf["headers"]
    cid = _campaign(client, h, "Sweep locks")["campaign_id"]
    pid = _contact(client, h, "sweeplock")
    mid = _queued_message(cid, pid)
    sql("UPDATE email_messages SET claimed_at = UTC_TIMESTAMP() WHERE status = 'SENDING' AND claimed_at IS NULL")
    with SessionLocal() as batch:
        email_scheduler._recover_stuck_sends(batch)  # batch session left open, as in process_due_emails
        with SessionLocal() as lane:
            lane.execute(text("SET SESSION innodb_lock_wait_timeout = 2"))
            claimed = lane.execute(text("UPDATE email_messages SET status = 'SENDING', claimed_at = UTC_TIMESTAMP() "
                                        "WHERE message_id = :m AND status = 'QUEUED'"), {"m": mid}).rowcount
            lane.commit()
    assert claimed == 1


# ── T-2 import: memberships per chunk ────────────────────────

def test_import_failure_mid_way_keeps_list_members_of_committed_chunks(client, tenant_rf, monkeypatch):
    import app.routers.import_router as import_router

    h = tenant_rf["headers"]
    monkeypatch.setattr(import_router, "CHUNK", 2)
    real = import_router.crm.email_domain
    emails = [f"imp{i}.{uuid.uuid4().hex[:6]}.{RUN}@rfimport{RUN}.example.com" for i in range(5)]

    def failing_on_third_contact(email):  # row 3 = first row of the second chunk
        if email == emails[2]:
            raise RuntimeError("injected failure")
        return real(email)

    monkeypatch.setattr(import_router.crm, "email_domain", failing_on_third_contact)
    csv = "email,first_name\n" + "".join(f"{e},Imp{i}\n" for i, e in enumerate(emails))
    list_name = f"Import list {uuid.uuid4().hex[:6]} {RUN}"
    r = client.post("/imports", headers=h, files={"file": ("c.csv", io.BytesIO(csv.encode()), "text/csv")},
                    data={"mapping": json.dumps({"email": "contact.email", "first_name": "contact.first_name"}),
                          "options": json.dumps({"new_list_name": list_name})})
    assert r.status_code == 500, r.text

    list_id = sql("SELECT list_id FROM prospect_lists WHERE list_name = :n", n=list_name)
    assert list_id, "the first chunk (with the new list) was committed"
    members = sql("SELECT COUNT(*) FROM prospect_list_members WHERE list_id = :l", l=list_id)
    created = sql("SELECT COUNT(*) FROM prospects WHERE tenant_id = :t AND email LIKE :p",
                  t=tenant_rf["tenant_id"], p=f"imp%.{RUN}@rfimport{RUN}.example.com")
    assert created == 2 and members == 2  # chunk 1 committed with its memberships; chunk 2 rolled back
    assert sql("SELECT status FROM import_jobs WHERE tenant_id = :t ORDER BY started_at DESC LIMIT 1",
               t=tenant_rf["tenant_id"]) == "FAILED"


# ── T-5 fixed query budgets ──────────────────────────────────

def _flat(client, path, h, add_rows, n=3):
    """Statement count of GET path with n rows and with 3n rows: must not grow with the rows."""
    add_rows(n)
    queries_for(lambda: client.get(path, headers=h))  # warm caches (settings, stages, ...)
    small = queries_for(lambda: client.get(path, headers=h))
    add_rows(2 * n)
    large = queries_for(lambda: client.get(path, headers=h))
    assert large.count <= small.count, (
        f"{path}: {small.count} statements with {n} rows, {large.count} with {3 * n}\n"
        + "\n".join(large.statements))
    return small.count


def _users(tenant, n):
    from app.models.user import User
    owner_id = sql("SELECT user_id FROM users WHERE email = :e", e=tenant["email"])
    with SessionLocal() as db:
        for i in range(n):  # team members reporting to the signed-in user
            db.add(User(tenant_id=tenant["tenant_id"], email=f"qb{i}.{uuid.uuid4().hex[:6]}.{RUN}@rf{RUN}.example.com",
                        first_name="Q", last_name="B", role="AGENT", status="ACTIVE", manager_id=owner_id))
        db.commit()


def _opps(client, h, n):
    for i in range(n):
        r = client.post("/opportunities", headers=h, json={"name": f"Deal {i} {uuid.uuid4().hex[:4]}",
                                                         "amount": 100 + i})
        assert r.status_code == 201, r.text


def test_profile_query_budget(client, tenant_rf):
    _flat(client, "/api/auth/me", tenant_rf["headers"], lambda n: _users(tenant_rf, n))


def test_contacts_list_query_budget(client, tenant_rf):
    h = tenant_rf["headers"]
    _flat(client, "/contacts?page_size=100", h, lambda n: [_contact(client, h, f"qb{i}") for i in range(n)])


def test_campaigns_list_query_budget(client, tenant_rf):
    h = tenant_rf["headers"]
    _flat(client, "/campaigns?page_size=100", h, lambda n: [_campaign(client, h, f"QB {i}") for i in range(n)])


def test_opportunities_list_and_board_query_budget(client, tenant_rf):
    h = tenant_rf["headers"]
    _flat(client, "/opportunities?page_size=100", h, lambda n: _opps(client, h, n))
    _flat(client, "/opportunities/board", h, lambda n: _opps(client, h, n))
    board = client.get("/opportunities/board", headers=h).json()["columns"]
    assert sum(c["count"] for c in board) == len([i for c in board for i in c["items"]])
    assert all(c["amount"] == sum(i["amount"] or 0 for i in c["items"]) for c in board)


def test_board_query_count_does_not_grow_with_stages(client):
    tenant = register_tenant(client, "rfst")
    h = tenant["headers"]
    _opps(client, h, 2)
    queries_for(lambda: client.get("/opportunities/board", headers=h))
    before = queries_for(lambda: client.get("/opportunities/board", headers=h)).count
    stages = client.get("/opportunities/board", headers=h).json()["columns"]
    for i in range(3):
        sql("INSERT INTO sales_stages (stage_id, tenant_id, name, probability, sort_order, is_won, is_lost, active) "
            "VALUES (UUID(), :t, :n, 50, :o, 0, 0, 1)", t=tenant["tenant_id"], n=f"Extra {i}", o=100 + i)
    after_resp = client.get("/opportunities/board", headers=h)
    assert len(after_resp.json()["columns"]) >= len(stages)
    after = queries_for(lambda: client.get("/opportunities/board", headers=h)).count
    assert after <= before


# ── T-1 no transaction during external calls ─────────────────

def test_persona_generation_holds_no_transaction_during_llm(client, tenant_rf, monkeypatch):
    import app.services.batch_processor_service as bps
    from app.models import PersonaBlueprint, ProspectList
    from app.models.email_sequence import EmailSequence
    from app.models.email_template import EmailTemplate

    h = tenant_rf["headers"]
    persona = f"RFBP_{RUN}".upper()
    with SessionLocal() as db:
        if not db.query(PersonaBlueprint).filter(PersonaBlueprint.persona_type == persona).first():
            db.add(PersonaBlueprint(persona_type=persona, is_active=True))
        lst = ProspectList(tenant_id=tenant_rf["tenant_id"], list_name=f"RF list {uuid.uuid4().hex[:6]}",
                           source_type="MANUAL",
                           uploaded_by=sql("SELECT user_id FROM users WHERE email = :e", e=tenant_rf["email"]))
        db.add(lst)
        db.commit()
        list_id = lst.list_id
    cid = _campaign(client, h, "Persona T-1")["campaign_id"]
    with SessionLocal() as db:
        db.add(EmailSequence(campaign_id=cid, step_number=1, wait_days=0))
        db.add(EmailTemplate(campaign_id=cid, designation=persona, subject="Old", body="Old"))
        db.commit()
    calls = []

    def fake_llm(**kw):
        assert_no_open_transaction("persona LLM call")
        calls.append(kw)
        return {"email_1": {"subject": "New subject", "body": "New body", "cta": ""}}

    monkeypatch.setattr(bps, "generate_sequence_with_llm", fake_llm)
    r = client.post(f"/campaigns/{cid}/personas/{persona}/generations", headers=h,
                    json={"list_id": list_id, "sequence_steps": [{"wait_days": 0}]})
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    with SessionLocal() as db:
        subjects = [s for (s,) in db.query(EmailTemplate.subject).filter(
            EmailTemplate.campaign_id == cid, EmailTemplate.designation == persona)]
    assert subjects == ["New subject"]  # old template replaced after the call


def test_regenerate_field_holds_no_transaction_during_llm(client, tenant_rf, monkeypatch):
    import types
    import app.services.sequence_generator as sg

    h = tenant_rf["headers"]
    cid = _campaign(client, h, "Regenerate T-1")["campaign_id"]
    r = client.post("/templates", headers=h, json={"campaign_id": cid, "subject": "Before", "body": "Body"})
    assert r.status_code == 201, r.text
    template_id = r.json()["template_id"]

    def create(**kw):
        assert_no_open_transaction("regenerate LLM call")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="  Regenerated subject  "))])

    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(sg, "openai_client", fake)
    r = client.post(f"/templates/{template_id}/regenerations", headers=h,
                    json={"template_id": template_id, "field": "subject"})
    assert r.status_code == 200, r.text
    assert r.json()["subject"] == "Regenerated subject"
    assert sql("SELECT subject FROM email_templates WHERE template_id = :t", t=template_id) == "Regenerated subject"


def test_ms365_token_refresh_outside_transaction_without_committing_caller(client, tenant_rf, monkeypatch):
    from app.models.sending_inbox import SendingInbox
    from app.services import ms365_oauth

    h = tenant_rf["headers"]
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"ms{uuid.uuid4().hex[:6]}@rfms{RUN}.example.com", "smtp_host": "127.0.0.1",
        "smtp_port": 1, "smtp_username": "u", "smtp_password": "p", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "u", "imap_password": "p", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    with SessionLocal() as db:
        inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).one()
        inbox.auth_type, inbox.oauth_refresh_token = ms365_oauth.AUTH_TYPE, "refresh-1"
        inbox.oauth_access_token, inbox.oauth_expires_at = "old", utcnow() - timedelta(minutes=1)
        db.commit()

    def fake_token_request(data):
        assert_no_open_transaction("token refresh")
        assert data == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
        return {"access_token": "new-access", "refresh_token": "refresh-2", "expires_in": 3600}

    monkeypatch.setattr(ms365_oauth, "_token_request", fake_token_request)
    # A reading caller: its read transaction is released before the HTTP call
    with SessionLocal() as db:
        inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).one()
        assert ms365_oauth.access_token(db, inbox) == "new-access"
        assert inbox.oauth_access_token == "new-access" and inbox not in db.dirty
    with SessionLocal() as db:
        stored = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).one()
        assert (stored.oauth_access_token, stored.oauth_refresh_token) == ("new-access", "refresh-2")

    # A caller with staged work: the token is stored, the caller's change is not committed
    sql("UPDATE sending_inboxes SET oauth_expires_at = :t WHERE inbox_id = :i",
        t=utcnow() - timedelta(minutes=1), i=inbox_id)
    monkeypatch.setattr(ms365_oauth, "_token_request", lambda data: {"access_token": "third", "expires_in": 60})
    with SessionLocal() as db:
        inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).one()
        inbox.daily_limit = 7777
        assert ms365_oauth.access_token(db, inbox) == "third"
        assert inbox in db.dirty
        db.rollback()
    assert sql("SELECT oauth_access_token IS NOT NULL FROM sending_inboxes WHERE inbox_id = :i", i=inbox_id)
    assert sql("SELECT daily_limit FROM sending_inboxes WHERE inbox_id = :i", i=inbox_id) != 7777
    with SessionLocal() as db:
        stored = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).one()
        assert stored.oauth_access_token == "third" and stored.oauth_refresh_token == "refresh-2"


# ── T-3 optimistic concurrency ───────────────────────────────

def test_legacy_prospect_patch_is_version_checked(client, tenant_rf):
    h = tenant_rf["headers"]
    pid = _contact(client, h, "occ")
    v = client.get(f"/contacts/{pid}", headers=h).json()["row_version"]
    r = client.patch(f"/prospects/{pid}", headers=h, json={"first_name": "First", "expected_version": v})
    assert r.status_code == 200, r.text
    assert r.json()["row_version"] == v + 1
    r = client.patch(f"/prospects/{pid}", headers=h, json={"first_name": "Stale", "expected_version": v})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "STALE_VERSION"
    r = client.patch(f"/prospects/{pid}", headers={**h, "If-Match": f'"{v}"'}, json={"first_name": "Stale"})
    assert r.status_code == 409
    assert sql("SELECT first_name FROM prospects WHERE prospect_id = :p", p=pid) == "First"


def test_wizard_activation_is_version_checked(client, tenant_rf):
    h = tenant_rf["headers"]
    campaign = _campaign(client, h, "Wizard OCC")
    cid, v = campaign["campaign_id"], campaign["row_version"]
    r = client.put(f"/campaigns/{cid}", headers=h, json={"campaign_description": "edited", "expected_version": v})
    assert r.status_code == 200, r.text
    assert r.json()["row_version"] == v + 1
    r = client.post(f"/campaigns/{cid}/activations", headers=h, json={"campaign_id": cid, "expected_version": v})
    assert r.status_code == 409, r.text
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=cid) == "DRAFT"
    r = client.post(f"/campaigns/{cid}/activations", headers=h,
                    json={"campaign_id": cid, "expected_version": v + 1})
    assert r.status_code != 409, r.text  # current version passes the check (activation itself may refuse)
