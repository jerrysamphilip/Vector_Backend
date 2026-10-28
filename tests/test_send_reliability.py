"""
Sending reliability (items 12, 14, 15a, 19, 20 of the sending review), against the real schema:
no platform-address fallback, platform failures re-queue without failing, a bounce recorded
mid-send survives, one email per campaign step per contact, per-step stop flags.
"""
import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import RUN, SessionLocal, register_tenant, sql
from app.core.clock import utcnow
from app.core.config import settings
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.services import send_breaker
from app.services.campaign_email_service import CampaignEmailService
from app.services.email_scheduler_service import email_scheduler, record_deferred_failure, stop_pending_steps
from app.services.email_sender_service import PlatformEmailFailure, email_sender


@pytest.fixture(scope="module")
def tenant_sd(client):
    return register_tenant(client, "sd")


@pytest.fixture(autouse=True)
def _clean_breaker(monkeypatch):
    send_breaker.platform_breaker.record_success()
    send_breaker.sender_rejections.reset()
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)
    # Business-hours windows depend on the day the suite runs; these tests are about other rules
    monkeypatch.setattr("app.utils.business_calendar.is_within_send_window", lambda **kw: True)
    yield
    send_breaker.platform_breaker.record_success()
    send_breaker.sender_rejections.reset()


def _inbox(client, h, label="s"):
    domain = f"{label}-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass",
        "warmup_enabled": False, "delay_between_emails": 0})
    assert r.status_code == 201, r.text
    return r.json()["inbox_id"]


def _campaign(client, h, label, inbox_ids=None, status="ACTIVE"):
    r = client.post("/campaigns", headers=h, json={
        "campaign_name": f"{label} {uuid.uuid4().hex[:6]} {RUN}", "inbox_ids": inbox_ids or []})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    if status != "DRAFT":
        sql("UPDATE campaigns SET status = :s WHERE campaign_id = :c", s=status, c=cid)
    return cid


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@sdclient{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _step(cid, number=1, **flags):
    with SessionLocal() as db:
        step = EmailSequence(campaign_id=cid, step_number=number, wait_days=0, **flags)
        db.add(step)
        db.commit()
        return step.sequence_id


def _message(cid, pid, seq=None, status="QUEUED", inbox_id=None, **kw):
    with SessionLocal() as db:
        msg = EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq, inbox_id=inbox_id,
                           direction=kw.pop("direction", "OUTBOUND"), status=status, subject="Hi",
                           body_text="Hello there", to_email=f"{pid}@sdclient{RUN}.example.com",
                           scheduled_at=utcnow() - timedelta(minutes=1), **kw)
        db.add(msg)
        db.commit()
        return msg.message_id


def _row(mid):
    with SessionLocal() as db:
        return db.query(EmailMessage).filter(EmailMessage.message_id == mid).one()


def _fake_sender(monkeypatch, on_send=None):
    calls = []

    async def fake_send(email_message, email_template, prospect, **kw):
        calls.append(email_message.message_id)
        if on_send:
            on_send(email_message)
        return {"success": True, "ses_message_id": f"ses-{uuid.uuid4().hex}",
                "internet_message_id": f"<{uuid.uuid4().hex}@sd.example.com>"}

    monkeypatch.setattr(email_sender, "send_with_retry", fake_send)
    return calls


def _run(mid):
    return asyncio.run(email_scheduler._run_inbox_queue([mid]))


# ── Item 12: no platform-address fallback ─────────────────────

def test_send_no_inbox_campaign_mail_stays_queued(client, tenant_sd, monkeypatch):
    calls = _fake_sender(monkeypatch)
    h = tenant_sd["headers"]
    cid = _campaign(client, h, "No inbox")
    pid = _contact(client, h, "noinbox")
    mid = _message(cid, pid, _step(cid))
    stats = _run(mid)
    msg = _row(mid)
    assert calls == [] and stats["sent"] == 0
    assert msg.status == "QUEUED" and msg.sent_at is None and msg.send_key is None
    assert msg.last_error_code == "NO_ACTIVE_INBOX"
    assert msg.scheduled_at > utcnow() + timedelta(minutes=50)
    assert msg.from_email != settings.SENDER_EMAIL or not settings.SENDER_EMAIL


def test_send_launch_resume_send_now_refuse_without_active_inbox(client, tenant_sd):
    h = tenant_sd["headers"]
    draft = _campaign(client, h, "Launch no inbox", status="DRAFT")
    r = client.post(f"/campaigns/{draft}/launch", headers=h)
    assert r.status_code == 400 and "no active sending inbox" in r.json()["detail"], r.text
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=draft) == "DRAFT"

    paused = _campaign(client, h, "Resume no inbox", status="PAUSED")
    r = client.post(f"/campaigns/{paused}/resume", headers=h)
    assert r.status_code == 400 and "no active sending inbox" in r.json()["detail"], r.text
    assert sql("SELECT status FROM campaigns WHERE campaign_id = :c", c=paused) == "PAUSED"

    active = _campaign(client, h, "Send now no inbox")
    r = client.post(f"/campaigns/{active}/send-now", headers=h)
    assert r.status_code == 400 and "no active sending inbox" in r.json()["detail"], r.text

    # A paused inbox does not count
    inbox = _inbox(client, h, "paused")
    sql("UPDATE sending_inboxes SET status = 'PAUSED' WHERE inbox_id = :i", i=inbox)
    sql("INSERT INTO campaign_inboxes (campaign_id, inbox_id) VALUES (:c, :i)", c=paused, i=inbox)
    assert client.post(f"/campaigns/{paused}/resume", headers=h).status_code == 400
    sql("UPDATE sending_inboxes SET status = 'ACTIVE' WHERE inbox_id = :i", i=inbox)
    r = client.post(f"/campaigns/{paused}/resume", headers=h)
    assert r.status_code == 200 and r.json()["new_status"] == "ACTIVE", r.text


def test_send_preschedule_without_inbox_never_uses_platform_address(client, tenant_sd):
    h = tenant_sd["headers"]
    cid = _campaign(client, h, "Preschedule no inbox")
    pid = _contact(client, h, "presched")
    seq = _step(cid)
    with SessionLocal() as db:
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=None, direction="OUTBOUND",
                            status="CANCELLED", to_email="x@y.example.com"))  # unrelated row
        db.commit()
    sql("INSERT INTO email_templates (template_id, campaign_id, tenant_id, sequence_id, subject, body) "
        "SELECT :t, campaign_id, tenant_id, :s, 'Hi', 'Hello' FROM campaigns WHERE campaign_id = :c",
        t=str(uuid.uuid4()), c=cid, s=seq)
    sql("INSERT INTO campaign_prospects (id, campaign_id, prospect_id, tenant_id, current_step, status) "
        "SELECT :i, campaign_id, :p, tenant_id, 1, 'ACTIVE' FROM campaigns WHERE campaign_id = :c",
        i=str(uuid.uuid4()), c=cid, p=pid)
    with SessionLocal() as db:
        assert CampaignEmailService(db)._preschedule_all_emails(cid) == 1
        db.commit()
    assert sql("SELECT from_email FROM email_messages WHERE campaign_id = :c AND sequence_id = :s",
               c=cid, s=seq) is None
    assert sql("SELECT inbox_id FROM email_messages WHERE campaign_id = :c AND sequence_id = :s",
               c=cid, s=seq) is None


# ── Item 14: platform failures re-queue; sender rejections pause the inbox ──

def test_send_platform_error_requeues_without_failing(client, tenant_sd, monkeypatch):
    async def paused_account(email_message, email_template, prospect, **kw):
        raise PlatformEmailFailure("AccountSendingPausedException: Sending paused for this account",
                                   code="AccountSendingPausedException")

    monkeypatch.setattr(email_sender, "send_with_retry", paused_account)
    h = tenant_sd["headers"]
    inbox = _inbox(client, h, "platform")
    cid = _campaign(client, h, "Platform error", [inbox])
    pid = _contact(client, h, "platform")
    mid = _message(cid, pid, _step(cid), inbox_id=inbox)
    _run(mid)
    msg = _row(mid)
    assert msg.status == "QUEUED" and msg.final_status is None
    assert msg.retry_count == 0 and msg.deferred_count == 1
    assert msg.last_error_code == "AccountSendingPausedException"
    assert msg.next_retry_at > utcnow() + timedelta(minutes=3)
    assert msg.send_key is None and msg.sent_at is None

    # At the cap it is failed with the reason, never silently dropped
    sql("UPDATE email_messages SET deferred_count = :n, next_retry_at = NULL WHERE message_id = :m",
        n=send_breaker.MAX_DEFERRED_ATTEMPTS - 1, m=mid)
    send_breaker.platform_breaker.record_success()
    _run(mid)
    msg = _row(mid)
    assert msg.status == "FAILED" and "platform" in msg.failure_reason


def test_send_breaker_open_stops_the_whole_pass(client, tenant_sd, monkeypatch):
    calls = _fake_sender(monkeypatch)
    h = tenant_sd["headers"]
    inbox = _inbox(client, h, "breaker")
    cid = _campaign(client, h, "Breaker", [inbox])
    mid = _message(cid, _contact(client, h, "breaker"), _step(cid), inbox_id=inbox)
    for _ in range(send_breaker.PLATFORM_FAILURE_THRESHOLD):
        record_deferred_failure(None, "PLATFORM", inbox, "Throttling: Maximum sending rate exceeded")
    assert send_breaker.platform_breaker.is_open()
    stats = asyncio.run(email_scheduler.process_scheduled_emails())
    assert stats["sent"] == 0 and calls == []
    assert _row(mid).status == "QUEUED"
    r = client.post(f"/campaigns/{cid}/send-now", headers=h)
    assert r.status_code == 503, r.text


def test_send_sender_rejections_pause_the_inbox(client, tenant_sd):
    h = tenant_sd["headers"]
    inbox = _inbox(client, h, "identity")
    with SessionLocal() as db:
        for _ in range(send_breaker.SENDER_REJECTION_THRESHOLD):
            record_deferred_failure(db, "SENDER", inbox, "MessageRejected: Email address is not verified.")
        db.commit()
    assert sql("SELECT status FROM sending_inboxes WHERE inbox_id = :i", i=inbox) == "PAUSED"
    assert sql("SELECT COUNT(*) FROM inbox_warmup_events WHERE inbox_id = :i AND event_type = 'ISSUE'",
               i=inbox) == 1
    assert sql("SELECT COUNT(*) FROM notifications WHERE tenant_id = :t AND kind = 'INBOX_PAUSED'",
               t=tenant_sd["tenant_id"]) >= 1


# ── Item 15a: SENT never overwrites a bounce recorded mid-send ──

def test_send_mid_send_bounce_survives(client, tenant_sd, monkeypatch):
    def webhook_bounces(email_message):
        sql("UPDATE email_messages SET status = 'BOUNCED', final_status = 'BOUNCED' WHERE message_id = :m",
            m=email_message.message_id)

    _fake_sender(monkeypatch, webhook_bounces)
    h = tenant_sd["headers"]
    inbox = _inbox(client, h, "bounce")
    cid = _campaign(client, h, "Mid-send bounce", [inbox])
    mid = _message(cid, _contact(client, h, "bounce"), _step(cid), inbox_id=inbox)
    stats = _run(mid)
    msg = _row(mid)
    assert stats["sent"] == 1
    assert msg.status == "BOUNCED" and msg.final_status == "BOUNCED"
    assert msg.sent_at is not None and msg.ses_message_id and msg.provider_message_id


def test_send_success_marks_sent_and_paces_the_inbox(client, tenant_sd, monkeypatch):
    _fake_sender(monkeypatch)
    h = tenant_sd["headers"]
    inbox = _inbox(client, h, "pace")
    sql("UPDATE sending_inboxes SET delay_between_emails = 600 WHERE inbox_id = :i", i=inbox)
    cid = _campaign(client, h, "Pacing", [inbox])
    seq = _step(cid)
    first = _message(cid, _contact(client, h, "pace1"), seq, inbox_id=inbox)
    second = _message(cid, _contact(client, h, "pace2"), seq, inbox_id=inbox)
    stats = asyncio.run(email_scheduler._run_inbox_queue([first, second]))
    assert _row(first).status == "SENT"
    # The second is not slept on: it stays due, untouched, for a later pass
    assert stats["paced"] == 1 and _row(second).status == "QUEUED" and _row(second).send_key is None
    with SessionLocal() as db:
        assert email_scheduler.pace_wait(db, inbox) > 400


def test_send_stuck_sweep_keeps_a_webhook_outcome(client, tenant_sd):
    h = tenant_sd["headers"]
    cid = _campaign(client, h, "Sweep outcome")
    pid = _contact(client, h, "sweep")
    old = utcnow() - timedelta(minutes=email_scheduler.STUCK_SENDING_MINUTES + 5)
    bounced = _message(cid, pid, status="SENDING", claimed_at=old, final_status="BOUNCED")
    delivered = _message(cid, pid, status="SENDING", claimed_at=old, final_status="DELIVERED")
    unknown = _message(cid, pid, status="SENDING", claimed_at=old)
    with SessionLocal() as db:
        email_scheduler._recover_stuck_sends(db)
    assert (_row(bounced).status, _row(bounced).final_status) == ("SENT", "BOUNCED")
    assert (_row(delivered).status, _row(delivered).final_status) == ("SENT", "DELIVERED")
    assert _row(unknown).status == "FAILED"


# ── Item 19: one email per campaign step per contact ─────────

def test_send_step_key_allows_only_one_live_email_per_step(client, tenant_sd):
    h = tenant_sd["headers"]
    cid = _campaign(client, h, "Step key")
    pid = _contact(client, h, "stepkey")
    seq = _step(cid)
    _message(cid, pid, seq)
    with pytest.raises(IntegrityError):
        _message(cid, pid, seq, status="SCHEDULED")
    # Not constrained: cancelled duplicates, inbound rows, mail with no step
    _message(cid, pid, seq, status="CANCELLED")
    _message(cid, pid, seq, direction="INBOUND")
    _message(cid, pid, None)
    _message(cid, pid, None)
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c", c=cid) == 5


def test_send_duplicate_step_insert_is_skipped(client, tenant_sd):
    h = tenant_sd["headers"]
    cid = _campaign(client, h, "Duplicate insert")
    p1, p2 = _contact(client, h, "dup1"), _contact(client, h, "dup2")
    seq = _step(cid)
    existing = _message(cid, p1, seq)

    def new(pid):
        return EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq, direction="OUTBOUND",
                            status="SCHEDULED", to_email=f"{pid}@x.example.com", scheduled_at=utcnow())

    with SessionLocal() as db:
        # A concurrent pre-schedule already wrote p1's step: only p2's is added
        assert CampaignEmailService(db)._insert_new_messages([new(p1), new(p2)]) == 1
        db.commit()
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c AND prospect_id = :p",
               c=cid, p=p1) == 1
    assert sql("SELECT message_id FROM email_messages WHERE campaign_id = :c AND prospect_id = :p",
               c=cid, p=p1) == existing
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c AND prospect_id = :p",
               c=cid, p=p2) == 1


def test_send_inbox_sent_index_exists():
    assert sql("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
               "AND table_name = 'email_messages' AND index_name = 'ix_email_messages_inbox_sent'") == 2


# ── Item 20: per-step stop flags ──────────────────────────────

def test_send_step_flags_decide_stop_on_reply(client, tenant_sd, monkeypatch):
    calls = _fake_sender(monkeypatch)
    h = tenant_sd["headers"]
    inbox = _inbox(client, h, "flags")
    cid = _campaign(client, h, "Stop flags", [inbox])
    pid = _contact(client, h, "flags")
    keeps_going = _step(cid, 1, stop_on_reply=False)
    stops = _step(cid, 2, stop_on_reply=True)
    sql("INSERT INTO campaign_prospects (id, campaign_id, prospect_id, tenant_id, current_step, status) "
        "SELECT :i, campaign_id, :p, tenant_id, 1, 'REPLIED' FROM campaigns WHERE campaign_id = :c",
        i=str(uuid.uuid4()), c=cid, p=pid)
    m1 = _message(cid, pid, keeps_going, inbox_id=inbox)
    m2 = _message(cid, pid, stops, inbox_id=inbox)
    _run(m1)
    assert _row(m1).status == "SENT" and calls == [m1]
    with SessionLocal() as db:
        assert stop_pending_steps(db, cid, pid, "REPLIED") == 1
        db.commit()
    assert _row(m2).status == "CANCELLED" and _row(m2).failure_reason == "Stopped: contact replied"


def test_send_inbox_delay_is_bounded(client, tenant_sd):
    h = tenant_sd["headers"]
    r = client.post("/inboxes", headers=h, json={"email_address": f"slow@slow{RUN}.example.com",
                                                 "delay_between_emails": 3601})
    assert r.status_code == 422, r.text
