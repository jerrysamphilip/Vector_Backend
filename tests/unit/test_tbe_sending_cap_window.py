"""S-17: a mailbox at its daily cap and a closed send window.

Domain (app/domain/sending.py): window starts across days and DST, the first open moment, the roomiest
mailbox. Worker (EmailSchedulerService steps) against the A8 fakes: a contact this campaign never emailed
moves to the campaign mailbox with the most room (with its later steps); others are parked until the window
is open after the cooling period; outside the window the message is parked at the next window start."""
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app.core.clock import utcnow
from app.domain import sending as rules
from app.services import email_scheduler_service as sched_mod
from app.services.email_scheduler_service import EmailSchedulerService, _SendJob
from tests.unit.test_a8_sending_services import PROSPECT, FakeCtx, FakeDb, FakeQueue, _campaign, _inbox, _msg


# ── Domain ──

def test_tbe_window_time_accepts_time_and_timedelta():
    assert rules.window_time(time(8, 30), time(9)) == time(8, 30)
    assert rules.window_time(timedelta(hours=8, minutes=15, seconds=5), time(9)) == time(8, 15, 5)
    assert rules.window_time(None, time(9)) == time(9)


def test_tbe_window_starts_follow_the_local_clock_across_dst():
    # Saturday 2026-03-07 15:00 UTC; New York moves to EDT on Sunday 2026-03-08
    starts = rules.window_starts_after(datetime(2026, 3, 7, 15, 0), "America/New_York", time(9), days=3)
    assert starts == [datetime(2026, 3, 8, 13, 0), datetime(2026, 3, 9, 13, 0), datetime(2026, 3, 10, 13, 0)]
    # today's start still ahead is included; an unknown zone is UTC
    assert rules.window_starts_after(datetime(2026, 3, 9, 7, 0), "Nope/Zone", time(9), days=1) == [
        datetime(2026, 3, 9, 9, 0)]
    assert rules.window_starts_after(datetime(2026, 3, 9, 7, 0), None, time(9), days=1)[0].hour == 9


def test_tbe_first_open_moment_and_roomiest_inbox():
    weekday = lambda at: at.weekday() < 5  # noqa: E731
    assert rules.first_open_moment([datetime(2026, 3, 7), datetime(2026, 3, 9)], weekday) == datetime(2026, 3, 9)
    assert rules.first_open_moment([datetime(2026, 3, 7)], weekday) is None
    a, b, c = "a", "b", "c"
    assert rules.roomiest_inbox([rules.InboxRoom(a, 5, 100), rules.InboxRoom(b, 9, 10), rules.InboxRoom(c, 9, 50)]) == c
    assert rules.roomiest_inbox([rules.InboxRoom(a, 3), rules.InboxRoom(b, 3)]) == a
    assert rules.roomiest_inbox([rules.InboxRoom(a, 0, 500), rules.InboxRoom(b, -2)]) is None
    assert rules.roomiest_inbox([]) is None


# ── Worker ──

class Queue(FakeQueue):
    def __init__(self):
        super().__init__()
        self.rebinds = []

    def rebind_unsent(self, campaign_id, prospect_id, exclude_message_id, inbox_id, from_email):
        self.rebinds.append((campaign_id, prospect_id, exclude_message_id, inbox_id, from_email))
        return 2


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(ctx=FakeCtx(campaign=_campaign(), prospect=PROSPECT, inbox=_inbox()), queue=Queue(),
                        db=FakeDb(), sends=[])
    w.ctx.previous_sends = lambda campaign_id, prospect_id: w.sends
    monkeypatch.setattr(sched_mod, "_context", lambda db: w.ctx)
    monkeypatch.setattr(sched_mod, "_queue", lambda db: w.queue)
    monkeypatch.setattr(sched_mod.suppression, "blocked_reason", lambda db, prospect: None)
    monkeypatch.setattr(sched_mod.daily_limit, "is_first_step", lambda db, msg: False)
    monkeypatch.setattr(sched_mod.business_calendar, "is_within_send_window", lambda **kw: True)
    w.s = EmailSchedulerService()
    return w


def _prepare(w, msg):
    job = _SendJob(msg=msg, db=w.db)
    return w.s._prepare(job), job


def _capped_world(w, *others):
    full = _inbox(emails_sent_today=100, current_daily_limit=100, cooling_period_hours=6)
    w.ctx.inbox_row = full
    w.ctx.campaign = _campaign(inboxes=[_inbox(), *others])  # i1 here is the same mailbox as the full row


def test_tbe_new_contact_moves_to_the_mailbox_with_most_room(world):
    _capped_world(world,
                  _inbox(inbox_id="i2", email_address="b@acme.example.com", current_daily_limit=100,
                         emails_sent_today=50),
                  _inbox(inbox_id="i3", email_address="c@acme.example.com", current_daily_limit=200,
                         emails_sent_today=150),  # same room, larger rotation weight
                  _inbox(inbox_id="i4", email_address="d@acme.example.com", status="PAUSED", emails_sent_today=0))
    msg = _msg(inbox_id="i1", from_email="sender@acme.example.com")
    outcome, job = _prepare(world, msg)
    assert outcome is None and msg.inbox_id == "i3" and msg.from_email == "c@acme.example.com"
    assert job.from_email == "c@acme.example.com"
    assert world.queue.rebinds == [("c1", "p1", "m1", "i3", "c@acme.example.com")]


def test_tbe_warm_up_caps_count_as_no_room(world):
    _capped_world(world, _inbox(inbox_id="i2", email_address="b@acme.example.com", current_daily_limit=20,
                                emails_sent_today=20))
    msg = _msg(inbox_id="i1")
    assert _prepare(world, msg)[0] == rules.SKIPPED and msg.inbox_id == "i1" and world.queue.rebinds == []
    assert utcnow() + timedelta(hours=5) < msg.scheduled_at < utcnow() + timedelta(hours=7)


def test_tbe_a_contact_already_emailed_stays_with_its_mailbox(world):
    _capped_world(world, _inbox(inbox_id="i2", email_address="b@acme.example.com", emails_sent_today=0))
    world.sends = [SimpleNamespace(inbox_id="i1", from_email="sender@acme.example.com")]
    msg = _msg(inbox_id="i1")
    assert _prepare(world, msg)[0] == rules.SKIPPED and msg.inbox_id == "i1" and world.queue.rebinds == []


def test_tbe_capped_mail_is_parked_until_the_window_is_open_after_the_cooling_period(world, monkeypatch):
    now = utcnow()
    # open now (the window check passes), closed at now + 6 h, open again at the window start after it
    monkeypatch.setattr(sched_mod.business_calendar, "is_within_send_window",
                        lambda current_utc, **kw: current_utc < now + timedelta(hours=1)
                        or current_utc > now + timedelta(hours=7))
    world.ctx.campaign = _campaign(send_window_start=time(9), send_window_end=time(17))
    world.ctx.inbox_row = _inbox(emails_sent_today=100, cooling_period_hours=6)
    msg = _msg(inbox_id="i1")
    world.sends = [object()]
    assert _prepare(world, msg)[0] == rules.SKIPPED
    expected = next(at for at in rules.window_starts_after(now + timedelta(hours=6), "UTC", time(9))
                    if at > now + timedelta(hours=7))
    assert msg.scheduled_at == expected and msg.scheduled_at.time() == time(9)


def test_tbe_outside_the_window_parks_at_the_next_window_start(world, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(sched_mod.business_calendar, "is_within_send_window",
                        lambda current_utc, **kw: current_utc > now + timedelta(minutes=1)
                        and current_utc.weekday() < 5)
    world.ctx.campaign = _campaign(send_window_start=time(9), send_window_end=time(17))
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SKIPPED and msg.send_key is None
    expected = next(at for at in rules.window_starts_after(now, "UTC", time(9)) if at.weekday() < 5)
    assert msg.scheduled_at == expected


def test_tbe_window_never_open_in_the_look_ahead_falls_back_to_the_recheck(world, monkeypatch):
    monkeypatch.setattr(sched_mod.business_calendar, "is_within_send_window", lambda **kw: False)
    assert EmailSchedulerService._next_window_open(("UTC", time(9), time(17)), utcnow()) is None
    msg = _msg()
    assert _prepare(world, msg)[0] == rules.SKIPPED  # outside, never opens: re-checked in half an hour
    assert utcnow() + timedelta(minutes=25) < msg.scheduled_at < utcnow() + timedelta(minutes=35)


def test_tbe_capped_until_without_a_window_is_the_cooling_period(world):
    job = _SendJob(msg=_msg(), db=world.db)
    at = world.s._capped_until(job, _inbox(cooling_period_hours=None))
    assert utcnow() + timedelta(hours=23) < at <= utcnow() + timedelta(hours=24)
