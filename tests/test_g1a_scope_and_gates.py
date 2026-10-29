"""
g1a against MySQL:
- campaign wizard drafts are owner-scoped: a colleague (AGENT) gets 404 for someone else's draft id, an
  admin may read and delete it, lists show only the caller's own drafts;
- launch needs a template on every sequence step (not merely as many templates as steps), and the
  offline / built-in starter-template path still launches;
- DEC-20: IMAP sync and webhook delivery skip suspended workspaces (deliveries stay PENDING).
"""
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql
from app.core.clock import utcnow
from app.core.config import settings


@pytest.fixture(scope="module")
def ws(client):
    from app.models.company_profile import CompanyProfile

    tenant = register_tenant(client, "gonea")
    with SessionLocal() as db:
        db.add(CompanyProfile(tenant_id=tenant["tenant_id"], profile_name="Main", is_default=True,
                              company_name="G1a Co", postal_address="1 Main St, Springfield"))
        db.commit()
    return tenant


@pytest.fixture(autouse=True)
def _no_daily_limit(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)


def _member(client, tenant_id, label, role):
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@gonea{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


def _inbox(client, h):
    domain = f"g1a-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass",
        "warmup_enabled": False, "delay_between_emails": 60})
    assert r.status_code == 201, r.text
    return r.json()["inbox_id"]


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@g1aclient{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _campaign(client, h):
    r = client.post("/campaigns", headers=h, json={
        "campaign_name": f"G1a {uuid.uuid4().hex[:6]} {RUN}", "inbox_ids": [_inbox(client, h)], "sender_name": "Ann"})
    assert r.status_code == 201, r.text
    return r.json()["campaign_id"]


def _steps(cid, n):
    from app.models.email_sequence import EmailSequence

    ids = []
    with SessionLocal() as db:
        for number in range(1, n + 1):
            step = EmailSequence(campaign_id=cid, step_number=number, wait_days=0 if number == 1 else 2)
            db.add(step)
            db.flush()
            ids.append(step.sequence_id)
        db.commit()
    return ids


# ── 1. drafts are owner-scoped ───────────────────────────────────────────────────────────────────

def test_g1a_drafts_are_owner_scoped(client, ws):
    owner = ws["headers"]
    agent = _member(client, ws["tenant_id"], "agent", "AGENT")["headers"]
    colleague = _member(client, ws["tenant_id"], "colleague", "AGENT")["headers"]

    r = client.post("/campaign-drafts/", headers=agent, json={
        "draft_name": "Agent draft", "current_step": 2, "draft_data": {"x": 1}})
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]

    # The creator reads it; a colleague gets 404 (not 403) on read and delete, and never lists it
    assert client.get(f"/campaign-drafts/{draft_id}", headers=agent).status_code == 200
    assert client.get(f"/campaign-drafts/{draft_id}", headers=colleague).status_code == 404
    assert client.delete(f"/campaign-drafts/{draft_id}", headers=colleague).status_code == 404
    assert draft_id not in {d["draft_id"] for d in client.get("/campaign-drafts/", headers=colleague).json()}
    assert sql("SELECT COUNT(*) FROM campaign_drafts WHERE draft_id = :d", d=draft_id) == 1

    # Another workspace's admin: 404 too
    stranger = register_tenant(client, "goneb")
    assert client.get(f"/campaign-drafts/{draft_id}", headers=stranger["headers"]).status_code == 404

    # The workspace owner (SUPER_ADMIN) may read and delete a member's draft
    assert client.get(f"/campaign-drafts/{draft_id}", headers=owner).json()["draft_id"] == draft_id
    assert client.delete(f"/campaign-drafts/{draft_id}", headers=owner).status_code == 200
    assert sql("SELECT COUNT(*) FROM campaign_drafts WHERE draft_id = :d", d=draft_id) == 0


# ── 2. launch gate: one template per step ────────────────────────────────────────────────────────

def test_g1a_launch_refuses_a_step_without_template_even_when_counts_match(client, ws):
    from app.models.email_template import EmailTemplate

    h = ws["headers"]
    cid = _campaign(client, h)
    step1, _step2 = _steps(cid, 2)
    with SessionLocal() as db:  # two templates on step 1 (generic + CFO), none on step 2
        db.add(EmailTemplate(campaign_id=cid, sequence_id=step1, subject="Hi", body="Hello {{first_name}}"))
        db.add(EmailTemplate(campaign_id=cid, sequence_id=step1, designation="CFO", subject="CFO", body="Numbers"))
        db.commit()
    pid = _contact(client, h, "gate")
    assert client.post(f"/campaigns/{cid}/prospects", headers=h, json={"prospect_ids": [pid]}).status_code == 200

    r = client.post(f"/campaigns/{cid}/launch", headers=h)
    assert r.status_code == 400, r.text
    assert "step 2" in r.json()["detail"]
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=cid) == "DRAFT"
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c", c=cid) == 0


def test_g1a_builtin_starter_templates_still_launch(client, ws, monkeypatch):
    import app.services.batch_processor_service as bps
    from app.models import ProspectList, ProspectListMember

    persona = f"G1A_{RUN}".upper()
    monkeypatch.setattr(bps, "generate_sequence_with_llm", lambda **kw: None)  # no key / over budget
    monkeypatch.setattr(bps, "classify_prospect", lambda designation, company_name: (persona, 0.9))
    h = ws["headers"]
    cid = _campaign(client, h)
    _steps(cid, 3)
    pids = [_contact(client, h, "wiz1"), _contact(client, h, "wiz2")]
    owner_id = sql("SELECT user_id FROM users WHERE email = :e", e=ws["email"])
    with SessionLocal() as db:
        lst = ProspectList(tenant_id=ws["tenant_id"], list_name=f"G1a list {uuid.uuid4().hex[:6]}",
                           source_type="MANUAL", uploaded_by=owner_id)
        db.add(lst)
        db.flush()
        for pid in pids:
            db.add(ProspectListMember(list_id=lst.list_id, prospect_id=pid))
        db.commit()
        list_id = lst.list_id
    r = client.post(f"/campaigns/{cid}/generations", headers=h, json={
        "campaign_id": cid, "list_id": list_id, "product_name": "Widget",
        "sequence_steps": [{"step_number": 1, "wait_days": 0}, {"step_number": 2, "wait_days": 2},
                           {"step_number": 3, "wait_days": 2}]})
    assert r.status_code == 200, r.text
    assert r.json()["ai_fallback_used"] is True
    assert client.post(f"/campaigns/{cid}/prospects", headers=h, json={"prospect_ids": pids}).status_code == 200

    r = client.post(f"/campaigns/{cid}/launch", headers=h)
    assert r.status_code == 200, r.text
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=cid) == "ACTIVE"
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c", c=cid) == 6


# ── 3. DEC-20: suspended workspaces are not synced and get no webhooks ───────────────────────────

def _suspended(tenant_id):
    sql("UPDATE tenants SET status = 'SUSPENDED' WHERE tenant_id = :t", t=tenant_id)


def _reactivated(tenant_id):
    sql("UPDATE tenants SET status = 'ACTIVE' WHERE tenant_id = :t", t=tenant_id)


def test_g1a_imap_sync_skips_suspended_workspaces(client, ws):
    from app.repositories.inbox_sync import ImapSyncRepository

    tenant = register_tenant(client, "gonec")
    iid = _inbox(client, tenant["headers"])

    def listed():
        with SessionLocal() as db:
            return iid in {i.inbox_id for i in ImapSyncRepository(db).inboxes_to_sync()}

    assert listed()
    try:
        _suspended(tenant["tenant_id"])
        assert not listed()
    finally:
        _reactivated(tenant["tenant_id"])
    assert listed()


def test_g1a_webhooks_are_held_for_suspended_workspaces(client, ws):
    from app.models.webhook import WebhookDelivery, WebhookEndpoint
    from app.repositories.webhooks import borrow

    tenant = register_tenant(client, "goned")
    with SessionLocal() as db:
        ep = WebhookEndpoint(tenant_id=tenant["tenant_id"], url="https://hooks.example.com/g1a",
                             secret=f"whsec_{uuid.uuid4().hex}", event_types=["contact.created"], active=True)
        db.add(ep)
        db.flush()
        d = WebhookDelivery(tenant_id=tenant["tenant_id"], endpoint_id=ep.endpoint_id, event_id=str(uuid.uuid4()),
                            event_type="contact.created", payload="{}", status="PENDING",
                            next_attempt_at=utcnow() - timedelta(minutes=1))
        db.add(d)
        db.commit()
        delivery_id = d.delivery_id

    def due():
        with SessionLocal() as db:
            return delivery_id in {row.delivery_id for row, _ in borrow(db).webhooks.due(utcnow(), 10_000)}

    assert due()
    try:
        _suspended(tenant["tenant_id"])
        assert not due()
    finally:
        _reactivated(tenant["tenant_id"])
    # Nothing was cancelled while suspended: the delivery goes out after reactivation
    assert sql("SELECT status FROM webhook_deliveries WHERE delivery_id = :d", d=delivery_id) == "PENDING"
    assert due()
