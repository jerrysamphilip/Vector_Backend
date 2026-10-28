"""
A8: the worker's named send steps (EmailSchedulerService._prepare / _deliver / outcomes), the claim, the pass
statistics (next_run_in), stop_pending_steps and the "send now" steps, against fake repositories and a fake
session (no database).
"""
import asyncio
from datetime import datetime, timedelta, time
from types import SimpleNamespace

import pytest

from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError
from app.domain import sending as rules
from app.services import campaign_send_now_service as send_now_mod
from app.services import email_scheduler_service as sched_mod
from app.services import send_breaker, sequence_stop
from app.services.campaign_send_now_service import CampaignSendNowService, _Tally
from app.services.email_scheduler_service import EmailSchedulerService, _SendJob
from app.services.email_sender_service import PermanentEmailFailure, PlatformEmailFailure, TransientEmailFailure


class FakeDb:
    def __init__(self):
        self.commits = self.rollbacks = 0
        self.refreshed = []
        self.added = []

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def refresh(self, obj):
        self.refreshed.append(obj)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


class FakeQueue:
    def __init__(self, claim_rows=1, duplicate=None, conflict=False, earlier_unsent=False):
        self.claim_rows, self.duplicate, self.conflict, self.earlier_unsent = claim_rows, duplicate, conflict, earlier_unsent
        self.cancelled_duplicates = []
        self.claims = []

    def earlier_step_unsent(self, msg):
        return self.earlier_unsent

    def duplicate_of(self, msg):
        return self.duplicate

    def claim(self, message_id, key, now):
        if self.conflict:
            raise rules.SendKeyConflict("duplicate send_key")
        self.claims.append((message_id, key))
        return self.claim_rows

    def cancel_duplicate(self, message_id, reason):
        self.cancelled_duplicates.append((message_id, reason))
        return 1


class FakeCtx:
    def __init__(self, **kw):
        self.campaign = kw.get("campaign")
        self.creator = kw.get("creator", SimpleNamespace(first_name="Ann", last_name="Lee"))
        self.prospect_row = kw.get("prospect")
        self.template_row = kw.get("template")
        self.inbox_row = kw.get("inbox")
        self.pace = kw.get("pace", (0, None))
        self.status = kw.get("status", "ACTIVE")
        self.conv = kw.get("conv")
        self.added_convs = []

    def fresh_campaign(self, campaign_id):
        return self.campaign

    def campaign_with_creator(self, campaign_id):
        return (self.campaign, self.creator) if self.campaign and self.creator else None

    def prospect(self, prospect_id):
        return self.prospect_row

    def template(self, template_id):
        return self.template_row

    def inbox(self, inbox_id):
        return self.inbox_row

    def inbox_pace(self, inbox_id):
        return self.pace

    def previous_sends(self, campaign_id, prospect_id):
        return []

    def enrollment_status(self, campaign_id, prospect_id):
        return self.status

    def step(self, sequence_id):
        return SimpleNamespace(stop_on_reply=True, stop_on_bounce=True, step_number=1)

    def conversation(self, prospect_id, inbox_id):
        return self.conv

    def add_conversation(self, **fields):
        conv = SimpleNamespace(id="conv-new", **fields)
        self.added_convs.append(conv)
        return conv

    def inbox_delays(self, inbox_ids):
        return {}


def _inbox(**kw):
    base = dict(inbox_id="i1", email_address="sender@acme.example.com", status="ACTIVE", emails_sent_today=0,
                last_daily_reset=utcnow(), current_daily_limit=100, cooling_period_hours=24,
                delay_between_emails=0, warmup_enabled=False, warmup_start_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _campaign(**kw):
    base = dict(campaign_id="c1", status="ACTIVE", sender_name=None, created_by="u1", respect_timezone=False,
                campaign_timezone="UTC", send_window_start=time(0, 0), send_window_end=time(23, 59),
                inboxes=[_inbox()])
    base.update(kw)
    return SimpleNamespace(**base)


def _msg(**kw):
    base = dict(message_id="m1", campaign_id="c1", prospect_id="p1", sequence_id="s1", inbox_id=None,
                template_id=None, to_email="bob@client.example.com", from_email=None, subject="Hi",
                body_text="Hello", direction="OUTBOUND", status="QUEUED", send_key=None, scheduled_at=None,
                next_retry_at=None, retry_count=0, max_retries=3, deferred_count=0, failure_reason=None,
                last_error_code=None, final_status=None, final_status_at=None, claimed_at=None, sent_at=None,
                conversation_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


PROSPECT = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="bob@client.example.com", owner_id="u1",
                           poc_state=None)


@pytest.fixture
def world(monkeypatch):
    """A scheduler wired to fake repositories; tweak ``world.ctx`` / ``world.queue`` per test."""
    w = SimpleNamespace(ctx=FakeCtx(campaign=_campaign(), prospect=PROSPECT, inbox=_inbox()), queue=FakeQueue(),
                        db=FakeDb(), reserved=[], released=[])
    monkeypatch.setattr(sched_mod, "_context", lambda db: w.ctx)
    monkeypatch.setattr(sched_mod, "_queue", lambda db: w.queue)
    monkeypatch.setattr(sched_mod.suppression, "blocked_reason", lambda db, prospect: None)
    monkeypatch.setattr(sched_mod.daily_limit, "is_first_step", lambda db, msg: False)
    monkeypatch.setattr(sched_mod.business_calendar, "is_within_send_window", lambda **kw: True)
    w.s = EmailSchedulerService()
    w.s._daily = SimpleNamespace(reserve=lambda db, user: w.reserved.append(user) or True,
                                 release=lambda user: w.released.append(user))
    return w


def _prepare(w, msg):
    job = _SendJob(msg=msg, db=w.db)
    return w.s._prepare(job), job


# ── The steps ──

def test_a8_prepare_happy_path_claims_binds_and_restarts_the_clock(world):
    conv = SimpleNamespace(id="conv-1", last_message_at=datetime(2020, 1, 1))
    world.ctx.conv = conv
    msg = _msg()
    outcome, job = _prepare(world, msg)
    assert outcome is None
    assert world.queue.claims == [("m1", "c1:s1:bob@client.example.com")] and world.db.refreshed == [msg]
    assert msg.inbox_id == "i1" and msg.from_email == "sender@acme.example.com" == job.from_email
    assert job.sender_name == "Ann Lee" and job.template.subject == "Hi" and job.template.body == "Hello"
    # Item 5: an existing conversation is linked and brought to the top
    assert msg.conversation_id == "conv-1" and conv.last_message_at > datetime(2026, 1, 1)
    assert msg.claimed_at is not None and world.db.commits >= 2  # the claim, then the restarted clock


def test_a8_new_conversation_is_created_once_the_inbox_is_known(world):
    msg = _msg()
    _prepare(world, msg)
    [conv] = world.ctx.added_convs
    assert msg.conversation_id == "conv-new" and conv.tenant_id == "t1" and conv.inbox_id == "i1"
    assert conv.status == "OPEN" and conv.is_unread is False


def test_a8_paused_campaign_is_requeued_before_the_claim(world):
    world.ctx.campaign = _campaign(status="PAUSED")
    msg = _msg()
    outcome, _ = _prepare(world, msg)
    assert outcome == rules.SKIPPED and world.queue.claims == []
    assert msg.status == "QUEUED" and msg.scheduled_at > utcnow() + timedelta(minutes=55) and world.db.commits == 1


def test_a8_earlier_step_unsent_holds_the_follow_up(world):
    world.queue.earlier_unsent = True
    outcome, _ = _prepare(world, _msg())
    assert outcome == rules.SKIPPED and world.queue.claims == []


def test_a8_lost_claim_skips(world):
    world.queue.claim_rows = 0
    outcome, _ = _prepare(world, _msg())
    assert outcome == rules.SKIPPED and world.db.refreshed == []


def test_a8_missing_contact_fails_without_a_final_status(world):
    world.ctx.prospect_row = None
    msg = _msg()
    outcome, _ = _prepare(world, msg)
    assert outcome == rules.FAILED and msg.status == "FAILED" and msg.failure_reason == "Prospect not found"
    assert msg.final_status is None


def test_a8_suppressed_and_stopped_contacts_are_cancelled(world, monkeypatch):
    monkeypatch.setattr(sched_mod.suppression, "blocked_reason", lambda db, prospect: "Unsubscribed")
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SUPPRESSED and (msg.status, msg.failure_reason) == ("CANCELLED", "Unsubscribed")
    monkeypatch.setattr(sched_mod.suppression, "blocked_reason", lambda db, prospect: None)
    world.ctx.status = "REPLIED"
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SUPPRESSED and msg.failure_reason == "Stopped: contact replied"


def test_a8_missing_template_fails(world):
    world.ctx.template_row = None
    msg = _msg(template_id="tpl-gone")
    assert _prepare(world, msg)[0] == rules.FAILED and msg.failure_reason == "Template not found"


def test_a8_campaign_paused_mid_batch_and_outside_window_requeue(world, monkeypatch):
    calls = []
    world.ctx.campaign = _campaign()
    real = world.ctx.fresh_campaign
    world.ctx.fresh_campaign = lambda cid: real(cid)
    world.ctx.campaign_with_creator = lambda cid: (_campaign(status="PAUSED"), world.ctx.creator)
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SKIPPED and msg.send_key is None
    assert msg.scheduled_at > utcnow() + timedelta(minutes=55)

    world.ctx.campaign_with_creator = lambda cid: (_campaign(), world.ctx.creator)
    monkeypatch.setattr(sched_mod.business_calendar, "is_within_send_window",
                        lambda **kw: calls.append(kw) or False)
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SKIPPED
    assert utcnow() + timedelta(minutes=25) < msg.scheduled_at < utcnow() + timedelta(minutes=35)
    assert calls[-1]["timezone_str"] == "UTC" and calls[-1]["send_window_start"] == time(0, 0)


def test_a8_contact_timezone_follows_the_state_when_respected(monkeypatch):
    monkeypatch.setattr(sched_mod.business_calendar, "get_timezone_for_state", lambda state, tz: f"tz-{state}")
    campaign = _campaign(respect_timezone=True, campaign_timezone="America/New_York")
    assert EmailSchedulerService._contact_timezone(campaign, SimpleNamespace(poc_state="CA")) == "tz-CA"
    assert EmailSchedulerService._contact_timezone(campaign, SimpleNamespace(poc_state=None)) == "America/New_York"
    assert EmailSchedulerService._contact_timezone(_campaign(campaign_timezone=None),
                                                   SimpleNamespace(poc_state="CA")) == "UTC"


def test_a8_missing_creator_is_an_unexpected_failure(world):
    world.ctx.creator = None
    msg = _msg()
    assert asyncio.run(world.s._process_single_email(msg, world.db)) == rules.FAILED
    assert msg.status == "FAILED" and msg.failure_reason == "Unexpected: campaign or its creator not found"


def test_a8_no_active_inbox_keeps_the_email_queued(world):
    world.ctx.campaign = _campaign(inboxes=[_inbox(status="PAUSED")])
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SKIPPED and msg.last_error_code == "NO_ACTIVE_INBOX"


def test_a8_inbox_at_its_daily_cap_waits_out_the_cooling_period(world):
    world.ctx.inbox_row = _inbox(emails_sent_today=100, cooling_period_hours=6)
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SKIPPED and msg.status == "QUEUED" and msg.send_key is None
    assert utcnow() + timedelta(hours=5) < msg.scheduled_at < utcnow() + timedelta(hours=7)


def test_a8_new_day_resets_the_inbox_counter_and_warns_on_a_pace_mismatch(world, caplog):
    inbox = _inbox(emails_sent_today=100, last_daily_reset=utcnow() - timedelta(days=1), delay_between_emails=3600)
    with caplog.at_level("WARNING", logger="app.services.email_scheduler_service"):
        EmailSchedulerService._roll_inbox_day(inbox)
    assert inbox.emails_sent_today == 0 and inbox.last_daily_reset.date() == utcnow().date()
    assert "allows at most ~24/day" in caplog.text


def test_a8_paced_inbox_gives_the_claim_back(world):
    world.ctx.pace = (60, utcnow())  # sent just now: not ready
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.PACED
    assert msg.status == "QUEUED" and msg.send_key is None and msg.scheduled_at is None


def test_a8_daily_new_contact_limit_holds_first_steps(world, monkeypatch):
    monkeypatch.setattr(sched_mod.daily_limit, "is_first_step", lambda db, msg: True)
    world.s._daily = SimpleNamespace(reserve=lambda db, user: False, release=lambda user: None)
    msg = _msg()
    outcome, job = _prepare(world, msg)
    assert outcome == rules.SKIPPED and msg.last_error_code == sched_mod.daily_limit.HELD_CODE
    assert job.limit_user is None and "Held for tomorrow" in msg.failure_reason


def test_a8_daily_limit_is_reserved_for_the_owner_and_inbound_is_exempt(world, monkeypatch):
    monkeypatch.setattr(sched_mod.daily_limit, "is_first_step", lambda db, msg: True)
    outcome, job = _prepare(world, _msg())
    assert outcome is None and job.limit_user == "u1" and world.reserved == ["u1"]
    outcome, job = _prepare(world, _msg(direction="INBOUND"))
    assert outcome is None and job.limit_user is None


# ── Delivery and outcomes ──

def _job(world, **kw):
    job = _SendJob(msg=_msg(status="SENDING", send_key="k"), db=world.db, prospect=PROSPECT,
                   template=SimpleNamespace(subject="Hi", body="Hello"), campaign=_campaign(),
                   from_email="sender@acme.example.com", limit_user="u1")
    for key, value in kw.items():
        setattr(job, key, value)
    return job


def test_a8_deliver_releases_the_daily_slot_when_nothing_went_out(world, monkeypatch):
    async def transient(**kw):
        raise TransientEmailFailure("Throttling")

    monkeypatch.setattr(sched_mod.email_sender, "send_with_retry", transient)
    with pytest.raises(TransientEmailFailure):
        asyncio.run(world.s._deliver(_job(world)))
    assert world.released == ["u1"]

    async def refused(**kw):
        return {"success": False, "status": "BLOCKED_SPAM", "error": "lint"}

    monkeypatch.setattr(sched_mod.email_sender, "send_with_retry", refused)
    job = _job(world)
    assert asyncio.run(world.s._deliver(job)) == rules.FAILED
    assert world.released == ["u1", "u1"] and job.msg.final_status == "FAILED" and job.msg.last_error_code == "BLOCKED_SPAM"


def test_a8_deliver_success_clears_the_hold_and_records(world, monkeypatch):
    seen = {}

    async def ok(**kw):
        seen.update(kw)
        return {"success": True, "ses_message_id": "ses-1"}

    async def record(msg, result, from_email, prospect, db):
        seen["recorded"] = (result["ses_message_id"], from_email)
        return rules.SENT

    monkeypatch.setattr(sched_mod.email_sender, "send_with_retry", ok)
    monkeypatch.setattr(world.s, "_record_sent", record)
    job = _job(world)
    job.msg.last_error_code, job.msg.failure_reason = sched_mod.daily_limit.HELD_CODE, "Held for tomorrow"
    assert asyncio.run(world.s._deliver(job)) == rules.SENT
    assert job.msg.last_error_code is None and job.msg.failure_reason is None and world.released == []
    assert seen["recorded"] == ("ses-1", "sender@acme.example.com") and seen["sender_name"] is None
    assert seen["max_retries"] == 3 and seen["from_email_address"] == "sender@acme.example.com"


def test_a8_refused_send_retries_with_backoff_then_fails():
    msg = _msg(retry_count=0, max_retries=3)
    assert EmailSchedulerService._on_refused(msg, {"success": False, "error": "SES 500"}, PROSPECT) == rules.FAILED
    assert msg.status == "QUEUED" and msg.retry_count == 1 and msg.last_error_code == "SES 500"
    assert msg.next_retry_at > utcnow() + timedelta(seconds=100)
    msg = _msg(retry_count=2, max_retries=3)
    EmailSchedulerService._on_refused(msg, {"success": False}, PROSPECT)
    assert msg.status == "FAILED" and msg.failure_reason == "Unknown error" and msg.final_status == "FAILED"


def test_a8_transient_and_ambiguous_outcomes():
    msg = _msg(retry_count=0)
    assert EmailSchedulerService._on_transient(msg, TransientEmailFailure("Throttling")) == rules.FAILED
    assert msg.status == "QUEUED" and msg.next_retry_at is not None and msg.last_error_code == "Throttling"
    msg = _msg(retry_count=2)
    EmailSchedulerService._on_transient(msg, TransientEmailFailure("Throttling"))
    assert msg.status == "FAILED" and "persisted after 3 attempts" in msg.failure_reason
    msg = _msg()
    EmailSchedulerService._on_ambiguous(msg, Exception("read timeout"))
    assert msg.status == "FAILED" and "not retried to avoid a duplicate" in msg.failure_reason


def test_a8_permanent_recipient_failure_auto_suppresses(world, monkeypatch):
    suppressed = []
    monkeypatch.setattr(settings, "AUTO_SUPPRESS_HARD_BOUNCES", True)
    monkeypatch.setattr(world.s, "_auto_suppress_hard_bounce", lambda msg, prospect, db, err: suppressed.append(err))
    job = _job(world)
    exc = PermanentEmailFailure("MessageRejected: Illegal address", code="MessageRejected", recipient_fault=True)
    assert world.s._on_permanent(job, exc) == rules.FAILED
    assert job.msg.last_error_code == "MessageRejected" and suppressed == [str(exc)]
    job = _job(world)
    world.s._on_permanent(job, PermanentEmailFailure("ConfigurationSetDoesNotExist"))
    assert job.msg.last_error_code == "PERMANENT" and len(suppressed) == 1


def test_a8_deferred_failure_is_routed_to_defer(world, monkeypatch):
    monkeypatch.setattr(send_breaker, "platform_breaker", send_breaker.CircuitBreaker())
    monkeypatch.setattr(world.s, "_prepare", lambda job: None)

    async def deferred(job):
        raise PlatformEmailFailure("paused", code="AccountSendingPausedException")

    monkeypatch.setattr(world.s, "_deliver", deferred)
    msg = _msg(inbox_id="i1")
    assert asyncio.run(world.s._process_single_email(msg, world.db)) == rules.DEFERRED
    assert msg.deferred_count == 1 and msg.retry_count == 0


# ── Claim ──

def test_a8_claim_cancels_a_duplicate_step(world):
    world.queue.duplicate = "m0"
    msg = _msg()
    result = world.s._claim_message(msg, world.db)
    assert result == rules.ClaimResult(claimed=False, duplicate=True, duplicate_of="m0")
    assert world.queue.cancelled_duplicates == [("m1", rules.duplicate_reason("m0"))] and world.db.commits == 1


def test_a8_claim_conflict_rolls_back_and_cancels(world):
    world.queue.conflict = True
    result = world.s._claim_message(_msg(), world.db)
    assert result.duplicate and not result.claimed and world.db.rollbacks == 1
    assert world.queue.cancelled_duplicates == [("m1", rules.duplicate_reason(None))]


def test_a8_claim_of_adhoc_mail_has_no_send_key(world):
    msg = _msg(sequence_id=None)
    assert world.s._claim(msg, world.db) is True and world.queue.claims == [("m1", None)]


# ── Stuck sweep ──

def test_a8_settle_stuck_records_accepted_sends_as_sent():
    claimed = utcnow() - timedelta(minutes=30)
    accepted = SimpleNamespace(message_id="a", ses_message_id="ses-1", provider_message_id=None, delivered_at=None,
                               final_status=None, claimed_at=claimed, status="SENDING", sent_at=None,
                               failure_reason=None, final_status_at=None)
    unknown = SimpleNamespace(message_id="b", ses_message_id=None, provider_message_id=None, delivered_at=None,
                              final_status=None, claimed_at=claimed, status="SENDING", sent_at=None,
                              failure_reason=None, final_status_at=None)
    assert EmailSchedulerService._settle_stuck([accepted, unknown], set()) == (1, 1)
    assert accepted.status == "SENT" and accepted.sent_at == claimed and accepted.final_status is None
    assert unknown.status == "FAILED" and unknown.final_status == "FAILED"


# ── One pass: next_run_in instead of a fake "processed" count ──

def test_a8_paced_pass_reports_when_to_wake(world, monkeypatch):
    monkeypatch.setattr(sched_mod._time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(send_breaker, "platform_breaker", send_breaker.CircuitBreaker())
    monkeypatch.setattr(world.s, "_recover_stuck_sends", lambda db: None)
    monkeypatch.setattr(world.s, "_check_completed_campaigns", lambda db: None)
    monkeypatch.setattr(world.s, "_select_due", lambda db, now: [rules.DueRow("m1", "i1", "i1")])

    async def lane(ids):
        return {"paced": 1}

    monkeypatch.setattr(world.s, "_run_inbox_queue", lane)
    world.db.expire_all = lambda: None
    world.s._ready_at = {"i1": 1012.0}
    stats = asyncio.run(world.s.process_scheduled_emails(world.db))
    assert stats["processed"] == 0 and stats["paced"] == 1 and stats["recheck_in"] == 12
    assert stats["next_run_in"] == 12

    async def busy(ids):
        return {"processed": 2, "sent": 2}

    monkeypatch.setattr(world.s, "_run_inbox_queue", busy)
    stats = asyncio.run(world.s.process_scheduled_emails(world.db))
    assert stats["sent"] == 2 and stats["next_run_in"] == 2.0


def test_a8_idle_pass_uses_the_normal_interval(world, monkeypatch):
    monkeypatch.setattr(send_breaker, "platform_breaker", send_breaker.CircuitBreaker())
    monkeypatch.setattr(world.s, "_recover_stuck_sends", lambda db: None)
    monkeypatch.setattr(world.s, "_check_completed_campaigns", lambda db: None)
    monkeypatch.setattr(world.s, "_select_due", lambda db, now: [])
    stats = asyncio.run(world.s.process_scheduled_emails(world.db))
    assert stats["processed"] == 0 and stats["next_run_in"] is None


# ── stop_pending_steps: the one entry point ──

class FakeStopQueue:
    def __init__(self, rows):
        self.rows, self.cancelled = rows, []

    def stoppable_steps(self, campaign_id, prospect_id, statuses):
        assert statuses == sequence_stop.PENDING
        return self.rows

    def cancel(self, ids, reason, statuses):
        self.cancelled.append((list(ids), reason))
        return len(ids)


def test_a8_stop_pending_steps_follows_each_steps_flags(monkeypatch):
    rows = [("m2", SimpleNamespace(stop_on_reply=True, stop_on_bounce=True)),
            ("m3", SimpleNamespace(stop_on_reply=False, stop_on_bounce=True))]
    fake = FakeStopQueue(rows)
    monkeypatch.setattr(sequence_stop, "SqlAlchemySendingQueueRepository", lambda db: fake)
    assert sequence_stop.stop_pending_steps(None, "c1", "p1", "REPLIED") == 1
    assert fake.cancelled == [(["m2"], "Stopped: contact replied")]
    assert sequence_stop.stop_pending_steps(None, "c1", "p1", "BOUNCED", failure_reason="Hard bounce") == 2
    assert fake.cancelled[-1] == (["m2", "m3"], "Hard bounce")
    assert sequence_stop.stop_pending_steps(None, "c1", "p1", "ACTIVE") == 0
    assert sched_mod.stop_pending_steps is sequence_stop.stop_pending_steps  # re-exported, not a second copy


# ── "Send now" steps ──

def _send_now(monkeypatch, ctx=None, claim_rows=1):
    db = FakeDb()
    svc = CampaignSendNowService(db)
    svc.ctx = ctx or FakeCtx(campaign=_campaign(), prospect=PROSPECT, inbox=_inbox(),
                             template=SimpleNamespace(subject="Hi", body="Hello", attachments=[]))
    svc.queue = SimpleNamespace(claim_for_send_now=lambda mid, now: claim_rows,
                                record_sent_event=lambda *a, **k: None)
    monkeypatch.setattr(send_now_mod.suppression, "blocked_reason", lambda db, prospect: None)
    monkeypatch.setattr(send_now_mod.daily_limit, "is_first_step", lambda db, msg: False)
    monkeypatch.setattr(send_now_mod.email_scheduler, "stopped_by_enrollment", lambda msg, db: None)
    monkeypatch.setattr(send_now_mod.email_scheduler, "_resolve_sender_for_message",
                        lambda email_msg, campaign, db: "sender@acme.example.com")
    return svc, db


def test_a8_send_now_requires_an_active_sendable_campaign(monkeypatch):
    with pytest.raises(BadRequestError):
        CampaignSendNowService._require_sendable(_campaign(status="PAUSED"))
    with pytest.raises(BadRequestError):
        CampaignSendNowService._require_sendable(_campaign(inboxes=[]))
    CampaignSendNowService._require_sendable(_campaign())


def test_a8_send_now_skips_a_message_the_scheduler_holds(monkeypatch):
    svc, db = _send_now(monkeypatch, claim_rows=0)
    tally = _Tally()
    asyncio.run(svc._send_one(_campaign(), _msg(template_id="t1"), None, tally))
    assert db.commits == 1 and tally.sent == tally.failed == 0 and tally.skipped == []


def test_a8_send_now_guard_and_cap_skips(monkeypatch):
    svc, db = _send_now(monkeypatch)
    monkeypatch.setattr(send_now_mod.suppression, "blocked_reason", lambda db, prospect: "Unsubscribed")
    tally, msg = _Tally(), _msg(template_id="t1")
    assert svc._gate(_campaign(), msg, PROSPECT, tally) == (False, None, None)
    assert msg.status == "CANCELLED" and tally.skipped == [{"email": PROSPECT.email, "reason": "Unsubscribed"}]

    monkeypatch.setattr(send_now_mod.suppression, "blocked_reason", lambda db, prospect: None)
    svc.ctx.inbox_row = _inbox(emails_sent_today=100)
    tally, msg = _Tally(), _msg(template_id="t1", inbox_id="i1")
    assert svc._gate(_campaign(), msg, PROSPECT, tally)[0] is False
    assert msg.status == "QUEUED" and "reached its daily limit" in tally.skipped[0]["reason"]


def test_a8_send_now_error_outcomes(monkeypatch):
    svc, db = _send_now(monkeypatch)
    deferred = []
    monkeypatch.setattr(send_now_mod, "record_deferred_failure", lambda db, kind, inbox, err: deferred.append(kind))
    tally, msg = _Tally(), _msg(status="SENDING")
    svc._on_send_error(msg, PROSPECT, PlatformEmailFailure("Throttling", code="Throttling"), tally)
    assert deferred == ["PLATFORM"] and msg.status == "QUEUED" and tally.skipped and tally.failed == 0
    tally, msg = _Tally(), _msg(status="SENDING")
    svc._on_send_error(msg, PROSPECT, PermanentEmailFailure("MessageRejected"), tally)
    assert msg.status == "FAILED" and msg.final_status == "FAILED" and tally.failed == 1


def test_a8_send_now_refused_result_releases_the_daily_slot(monkeypatch):
    svc, db = _send_now(monkeypatch)
    released = []
    monkeypatch.setattr(send_now_mod.email_scheduler, "_daily",
                        SimpleNamespace(release=lambda user: released.append(user)))
    tally, msg = _Tally(), _msg(status="SENDING")
    result = {"success": False, "error": "BLOCKED"}
    svc._on_result(msg, PROSPECT, rules.SendAttempt.from_result(result), result, "s@x.example.com", "u1", tally)
    assert released == ["u1"] and msg.status == "FAILED" and tally.errors == [f"{PROSPECT.email}: BLOCKED"]
