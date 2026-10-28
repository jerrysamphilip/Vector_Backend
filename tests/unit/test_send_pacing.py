"""Non-blocking per-inbox pacing (F021, item 13) and the inbox delay bound."""
import asyncio
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.inbox_schema import MAX_DELAY_BETWEEN_EMAILS, SendingInboxCreate, SendingInboxUpdate
from app.services import email_scheduler_service as sched_mod
from app.services.email_scheduler_service import EmailSchedulerService
from app.domain.sending import PassStats
from app.services.send_pacing import next_gap, seconds_until_ready

NOW = datetime(2026, 10, 27, 12, 0, 0)


def test_send_gap_is_delay_with_bounded_jitter():
    rng = random.Random(7)
    gaps = [next_gap(60, 3, 15, rng) for _ in range(200)]
    assert all(45 <= g <= 75 for g in gaps) and len(set(gaps)) > 1
    assert all(7.5 <= next_gap(10, 3, 15, rng) <= 12.5 for _ in range(50))  # spread = 25% of delay
    assert all(3 <= next_gap(0, 3, 15, rng) <= 15 for _ in range(50))  # no own pace: human jitter
    assert next_gap(0, 0, 0) == 0 and next_gap(None, 5, 5) == 0


def test_send_ready_after_the_gap_from_either_clock():
    kw = dict(now=NOW, now_mono=100.0)
    assert seconds_until_ready(60, 15, last_sent_at=None, ready_at_mono=None, **kw) == 0
    # Own record: the jittered gap drawn at the last send
    assert seconds_until_ready(60, 15, last_sent_at=None, ready_at_mono=130.0, **kw) == 30
    assert seconds_until_ready(60, 15, last_sent_at=None, ready_at_mono=90.0, **kw) == 0
    # Database record (another process sent): at least the shortest allowed gap (60 - 15)
    assert seconds_until_ready(60, 15, last_sent_at=NOW - timedelta(seconds=20), ready_at_mono=None, **kw) == 25
    assert seconds_until_ready(60, 15, last_sent_at=NOW - timedelta(seconds=50), ready_at_mono=None, **kw) == 0
    # No delay of its own: only the process's jitter record counts
    assert seconds_until_ready(0, 15, last_sent_at=NOW, ready_at_mono=None, **kw) == 0


class _FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.row


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def query(self, *a):
        return _FakeQuery(self.rows.pop(0) if self.rows else None)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_send_lane_skips_a_not_ready_inbox_without_sleeping(monkeypatch):
    s = EmailSchedulerService()
    msg = SimpleNamespace(message_id="m1", status="QUEUED", sent_at=None, inbox_id="i1")
    monkeypatch.setattr(sched_mod, "SessionLocal", lambda: _FakeSession([msg, msg]))
    monkeypatch.setattr(s, "pace_wait", lambda db, inbox_id: 42.0)

    async def must_not_run(*a, **k):
        raise AssertionError("a not-ready inbox's email must not be processed")

    async def no_sleep(_):
        raise AssertionError("pacing must never sleep")

    monkeypatch.setattr(s, "_process_single_email", must_not_run)
    monkeypatch.setattr(sched_mod.asyncio, "sleep", no_sleep)
    stats = asyncio.run(s._run_inbox_queue(["m1", "m2"]))
    assert stats["paced"] == 1 and stats.get("processed", 0) == 0  # stops the lane: order kept


def test_send_lane_stops_when_the_breaker_is_open(monkeypatch):
    s = EmailSchedulerService()
    monkeypatch.setattr(sched_mod, "SessionLocal", lambda: _FakeSession([]))
    monkeypatch.setattr(sched_mod.send_breaker.platform_breaker, "is_open", lambda: True)
    stats = asyncio.run(s._run_inbox_queue(["m1"]))
    assert not any(stats.values())


def test_send_pacing_pass_wakes_the_loop_when_the_soonest_inbox_is_ready(monkeypatch):
    """A8: a pass held back only by pacing is not counted as processed; it sets next_run_in instead."""
    s = EmailSchedulerService()
    monkeypatch.setattr(sched_mod._time, "monotonic", lambda: 1000.0)
    s._ready_at = {"a": 1010.0}
    stats = PassStats(paced=1)
    s._note_pacing(stats)
    out = stats.as_dict()
    assert out["recheck_in"] == 10 and out["processed"] == 0 and out["next_run_in"] == 10
    s._ready_at = {"a": 1600.0}
    stats = PassStats(paced=1)
    s._note_pacing(stats)
    out = stats.as_dict()
    assert out["recheck_in"] == 600 and out["processed"] == 0 and out["next_run_in"] is None


def test_send_inbox_delay_bounds():
    assert MAX_DELAY_BETWEEN_EMAILS == 3600
    assert SendingInboxCreate(email_address="a@b.example.com", delay_between_emails=3600).delay_between_emails == 3600
    assert SendingInboxUpdate(delay_between_emails=0).delay_between_emails == 0
    for model, kw in ((SendingInboxCreate, {"email_address": "a@b.example.com"}), (SendingInboxUpdate, {})):
        for bad in (-1, 3601):
            with pytest.raises(ValidationError):
                model(delay_between_emails=bad, **kw)
