"""
A8 repositories against MySQL: the due batch (lanes, step order, workspace status, retry backoff), the atomic claim
and the send_key guard, the stuck-SENDING sweep queries, the conditional SENT, per-step stops, the warm-up
repository's tenant scope, and one full scheduler pass end to end (fake SES) so the lane plumbing is exercised.
"""
import asyncio
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant, sql
from app.core.clock import utcnow
from app.core.config import settings
from app.domain.sending import SendKeyConflict
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.repositories.sending import SqlAlchemySendingQueueRepository
from app.repositories.sending_context import SqlAlchemySendingContextRepository
from app.repositories.sending_warmup import SqlAlchemyWarmupRepository
from app.services import send_breaker
from app.services.email_scheduler_service import email_scheduler
from app.services.email_sender_service import email_sender
from app.services.sequence_stop import stop_pending_steps


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "aeight")


@pytest.fixture(scope="module")
def other_ws(client):
    return register_tenant(client, "aeightb")


def _inbox(client, h, label, delay=0):
    domain = f"{label}-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass",
        "warmup_enabled": False, "delay_between_emails": delay})
    assert r.status_code == 201, r.text
    return r.json()["inbox_id"]


def _campaign(client, h, label, inbox_ids=None, status="ACTIVE"):
    r = client.post("/campaigns", headers=h, json={
        "campaign_name": f"{label} {uuid.uuid4().hex[:6]} {RUN}", "inbox_ids": inbox_ids or []})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    sql("UPDATE campaigns SET status = :s WHERE campaign_id = :c", s=status, c=cid)
    return cid


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@aeclient{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _step(cid, number=1, **flags):
    with SessionLocal() as db:
        step = EmailSequence(campaign_id=cid, step_number=number, wait_days=0, **flags)
        db.add(step)
        db.commit()
        return step.sequence_id


def _message(cid, pid, seq=None, status="QUEUED", inbox_id=None, minutes_ago=1, **kw):
    with SessionLocal() as db:
        msg = EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq, inbox_id=inbox_id,
                           direction=kw.pop("direction", "OUTBOUND"), status=status, subject="Hi",
                           body_text="Hello there", to_email=kw.pop("to_email", f"{pid}@aeclient{RUN}.example.com"),
                           scheduled_at=utcnow() - timedelta(minutes=minutes_ago), **kw)
        db.add(msg)
        db.commit()
        return msg.message_id


def _status(mid):
    return sql("SELECT status FROM email_messages WHERE message_id = :m", m=mid)


def _due(lane_cap=120):
    with SessionLocal() as db:
        return SqlAlchemySendingQueueRepository(db).select_due(utcnow(), lane_cap, 5000)


# ── Due batch ──

def test_a8_due_batch_is_round_robin_and_capped_per_lane(client, ws):
    h = ws["headers"]
    inbox_a, inbox_b = _inbox(client, h, "lanea"), _inbox(client, h, "laneb")
    cid = _campaign(client, h, "Lanes", [inbox_a, inbox_b])
    a = [_message(cid, _contact(client, h, f"la{i}"), inbox_id=inbox_a, minutes_ago=10 - i) for i in range(3)]
    b = [_message(cid, _contact(client, h, f"lb{i}"), inbox_id=inbox_b, minutes_ago=10 - i) for i in range(2)]
    rows = [r for r in _due(lane_cap=2) if r.message_id in set(a + b)]
    assert [r.message_id for r in rows if r.lane == inbox_a] == a[:2]  # capped at 2, oldest first
    assert [r.message_id for r in rows if r.lane == inbox_b] == b
    # Round-robin: every lane's 1st row comes before any lane's 2nd
    order = [r.message_id for r in rows]
    assert max(order.index(a[0]), order.index(b[0])) < min(order.index(a[1]), order.index(b[1]))
    assert all(r.inbox_id == r.lane for r in rows)


def test_a8_due_batch_holds_paused_campaigns_backoff_and_later_steps(client, ws):
    h = ws["headers"]
    paused = _campaign(client, h, "Paused due", status="PAUSED")
    pid = _contact(client, h, "held")
    held_paused = _message(paused, pid)
    cid = _campaign(client, h, "Held due")
    backoff = _message(cid, pid, next_retry_at=utcnow() + timedelta(minutes=5))
    future = _message(cid, pid, minutes_ago=-30)
    first, second = _step(cid, 1), _step(cid, 2)
    step1 = _message(cid, pid, first, minutes_ago=5)
    step2 = _message(cid, pid, second, minutes_ago=10)  # due earlier, but its previous step is unsent (B01)
    due = {r.message_id for r in _due()}
    assert step1 in due and not {held_paused, backoff, future, step2} & due
    with SessionLocal() as db:
        queue = SqlAlchemySendingQueueRepository(db)
        assert queue.earlier_step_unsent(queue.load_message(step2)) is True
        assert queue.earlier_step_unsent(queue.load_message(step1)) is False
    sql("UPDATE email_messages SET status = 'SENT', sent_at = UTC_TIMESTAMP() WHERE message_id = :m", m=step1)
    assert step2 in {r.message_id for r in _due()}


def test_a8_due_batch_skips_suspended_workspaces(client, other_ws):
    h = other_ws["headers"]
    mid = _message(_campaign(client, h, "Suspended"), _contact(client, h, "susp"))
    assert mid in {r.message_id for r in _due()}
    try:
        sql("UPDATE tenants SET status = 'SUSPENDED' WHERE tenant_id = :t", t=other_ws["tenant_id"])
        assert mid not in {r.message_id for r in _due()}
    finally:
        sql("UPDATE tenants SET status = 'ACTIVE' WHERE tenant_id = :t", t=other_ws["tenant_id"])


# ── Claim ──

def test_a8_claim_is_atomic_and_the_send_key_is_unique(client, ws):
    h = ws["headers"]
    cid = _campaign(client, h, "Claim")
    pid = _contact(client, h, "claim")
    seq = _step(cid)
    first = _message(cid, pid, seq)
    key = f"{cid}:{seq}:{pid}@aeclient{RUN}.example.com".lower()
    with SessionLocal() as db:
        queue = SqlAlchemySendingQueueRepository(db)
        assert queue.claim(first, key, utcnow()) == 1
        db.commit()
        assert queue.claim(first, key, utcnow()) == 0  # already SENDING
        db.commit()
    assert _status(first) == "SENDING"
    assert sql("SELECT send_key FROM email_messages WHERE message_id = :m", m=first) == key
    assert sql("SELECT claimed_at FROM email_messages WHERE message_id = :m", m=first) is not None
    # A second row for the same step and address (a different contact row, same address): flagged as a duplicate
    other_pid = _contact(client, h, "claim2")
    dup = _message(cid, other_pid, seq, to_email=f"{pid}@aeclient{RUN}.example.com".upper())
    with SessionLocal() as db:
        queue = SqlAlchemySendingQueueRepository(db)
        assert queue.duplicate_of(queue.load_message(dup)) == first
        with pytest.raises(SendKeyConflict):
            queue.claim(dup, key, utcnow())
        db.rollback()
        assert queue.cancel_duplicate(dup, "Duplicate") == 1
        db.commit()
    assert _status(dup) == "CANCELLED"


def test_a8_send_now_claim_dates_the_claim(client, ws):
    h = ws["headers"]
    mid = _message(_campaign(client, h, "Send now claim"), _contact(client, h, "snclaim"), status="SCHEDULED")
    with SessionLocal() as db:
        assert SqlAlchemySendingQueueRepository(db).claim_for_send_now(mid, utcnow()) == 1
        db.commit()
    assert _status(mid) == "SENDING"
    assert sql("SELECT send_key FROM email_messages WHERE message_id = :m", m=mid) is None


# ── Stuck sweep and the conditional SENT ──

def test_a8_stuck_sweep_queries(client, ws):
    h = ws["headers"]
    cid = _campaign(client, h, "Stuck")
    pid = _contact(client, h, "stuck")
    old = utcnow() - timedelta(minutes=40)
    stale = _message(cid, pid, status="SENDING", claimed_at=old)
    fresh = _message(cid, pid, status="SENDING", claimed_at=utcnow())
    legacy = _message(cid, pid, status="SENDING")
    with SessionLocal() as db:
        db.add(EmailEvent(message_id=stale, event_type=EmailEvent.EVENT_DELIVERED))
        db.add(EmailEvent(message_id=fresh, event_type=EmailEvent.EVENT_SENT))
        db.commit()
        queue = SqlAlchemySendingQueueRepository(db)
        assert queue.date_undated_claims(utcnow()) >= 1
        db.commit()
        stuck = {m.message_id for m in queue.stuck_sending(utcnow() - timedelta(minutes=15))}
        assert stale in stuck and fresh not in stuck and legacy not in stuck
        assert queue.provider_event_ids([stale, fresh]) == {stale}  # our own SENT event is no evidence
    assert sql("SELECT claimed_at FROM email_messages WHERE message_id = :m", m=legacy) is not None


def test_a8_mark_sent_keeps_a_bounce_recorded_mid_send(client, ws):
    h = ws["headers"]
    cid = _campaign(client, h, "Mark sent")
    pid = _contact(client, h, "marksent")
    sending, bounced = _message(cid, pid, status="SENDING"), _message(cid, pid, status="BOUNCED")
    with SessionLocal() as db:
        queue = SqlAlchemySendingQueueRepository(db)
        values = {"sent_at": utcnow(), "from_email": "s@x.example.com", "provider_message_id": "<p>",
                  "ses_message_id": "ses-a8"}
        assert queue.mark_sent(queue.load_message(sending), values) is True
        msg = queue.load_message(bounced)
        assert queue.mark_sent(msg, values) is False and msg.status == "BOUNCED" and msg.sent_at is not None
        db.commit()
    assert _status(sending) == "SENT" and _status(bounced) == "BOUNCED"


# ── Per-step stops ──

def test_a8_stop_pending_steps_against_the_schema(client, ws):
    h = ws["headers"]
    cid = _campaign(client, h, "Stops")
    pid = _contact(client, h, "stops")
    keep_going = _message(cid, pid, _step(cid, 1, stop_on_bounce=False))
    stopped = _message(cid, pid, _step(cid, 2))
    manual = _message(cid, pid)  # no step: never stopped by these rules
    already_sent = _message(cid, pid, _step(cid, 3), status="SENT", sent_at=utcnow())
    with SessionLocal() as db:
        assert stop_pending_steps(db, cid, pid, "BOUNCED") == 1
        db.commit()
    assert _status(stopped) == "CANCELLED" and _status(keep_going) == "QUEUED"
    assert _status(manual) == "QUEUED" and _status(already_sent) == "SENT"
    assert sql("SELECT failure_reason FROM email_messages WHERE message_id = :m", m=stopped) == "Stopped: contact bounced"
    with SessionLocal() as db:
        assert stop_pending_steps(db, cid, pid, "UNSUBSCRIBED", failure_reason="Opted out") == 1
        db.commit()
    assert sql("SELECT failure_reason FROM email_messages WHERE message_id = :m", m=keep_going) == "Opted out"


# ── Lookups ──

def test_a8_context_lookups(client, ws):
    h = ws["headers"]
    inbox = _inbox(client, h, "ctx", delay=90)
    cid = _campaign(client, h, "Context", [inbox])
    pid = _contact(client, h, "ctx")
    _message(cid, pid, inbox_id=inbox, status="SENT", sent_at=utcnow() - timedelta(hours=1))
    with SessionLocal() as db:
        ctx = SqlAlchemySendingContextRepository(db)
        assert ctx.inbox_delays([inbox, None]) == {inbox: 90} and ctx.inbox_delay(inbox) == 90
        assert ctx.inbox_pace(inbox)[0] == 90 and ctx.inbox_pace("nope") is None
        assert [m.inbox_id for m in ctx.previous_sends(cid, pid)] == [inbox]
        campaign, creator = ctx.campaign_with_creator(cid)
        assert campaign.campaign_id == cid and creator.user_id
        assert ctx.enrollment_counts(cid) == (0, 0)
        assert ctx.tenant_admin_ids(ws["tenant_id"]) and ctx.tenant_admin_ids("no-such-tenant") == []


def test_a8_warmup_repository_is_tenant_scoped(client, ws, other_ws):
    inbox = _inbox(client, ws["headers"], "warm")
    with SessionLocal() as db:
        repo = SqlAlchemyWarmupRepository(db)
        assert repo.tenant_inbox(ws["tenant_id"], inbox).inbox_id == inbox
        assert repo.tenant_inbox(other_ws["tenant_id"], inbox) is None
        assert inbox in {i.inbox_id for i in repo.inboxes_to_warm(ws["tenant_id"])}
        assert inbox not in {i.inbox_id for i in repo.inboxes_to_warm(other_ws["tenant_id"])}
        assert repo.tenant_events(other_ws["tenant_id"], inbox, 10) == []


# ── One full pass, end to end ──

def test_a8_full_pass_sends_through_the_lanes(client, ws, monkeypatch):
    send_breaker.platform_breaker.record_success()
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)
    monkeypatch.setattr("app.utils.business_calendar.is_within_send_window", lambda **kw: True)
    sent = []

    async def fake_send(email_message, email_template, prospect, **kw):
        sent.append(email_message.message_id)
        return {"success": True, "ses_message_id": f"ses-{uuid.uuid4().hex}",
                "internet_message_id": f"<{uuid.uuid4().hex}@a8.example.com>"}

    monkeypatch.setattr(email_sender, "send_with_retry", fake_send)
    h = ws["headers"]
    inbox = _inbox(client, h, "pass")
    cid = _campaign(client, h, "Full pass", [inbox])
    pid = _contact(client, h, "pass")
    mid = _message(cid, pid, _step(cid), inbox_id=inbox)
    stats = asyncio.run(email_scheduler.process_scheduled_emails())
    assert mid in sent and _status(mid) == "SENT"
    assert stats["sent"] >= 1 and stats["next_run_in"] == 2.0
    conv = sql("SELECT conversation_id FROM email_messages WHERE message_id = :m", m=mid)
    assert conv and sql("SELECT last_message_at FROM conversations WHERE id = :c", c=conv) is not None
    assert sql("SELECT emails_sent_today FROM sending_inboxes WHERE inbox_id = :i", i=inbox) == 1
