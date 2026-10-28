"""A2 / F-04: pure rules of editing a campaign after launch (app/domain/campaigns_edit.py)."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app.domain.campaigns_edit import (ContactPlan, EditConflict, EditRefused, ScheduleSettings, StepEdit, StepMail,
                                       build_template_map, changed_settings, check_editable, check_settings,
                                       inbox_daily_capacity, launch_times, pacing_warning, passed_step, pick_template,
                                       plan_contact, plan_steps, renumber, starts_fresh, wait_until, window_minutes)


class FakeCalendar:
    """Business days = Mon-Fri; placing in the window = 09:00 of that day (UTC) or the next weekday."""

    def add_business_days(self, moment, days):
        while days:
            moment += timedelta(days=1)
            if moment.weekday() < 5:
                days -= 1
        return moment

    def batch_day_start(self, base, day_offset):
        return self.add_business_days(base, day_offset) if day_offset else base

    def timezone_for_state(self, state, default):
        return "America/New_York" if state == "NY" else default

    def place_in_window(self, base, timezone, settings, index, total):
        day = base if base.hour < 17 else base + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        slot = day.replace(hour=9, minute=index, second=0, microsecond=0)
        return max(slot, base.replace(second=0, microsecond=0))


def settings(**kw):
    return ScheduleSettings(calendar=FakeCalendar(), **kw)


def test_only_draft_active_paused_are_editable():
    for status in ("DRAFT", "ACTIVE", "PAUSED"):
        check_editable(status)
    with pytest.raises(EditConflict):
        check_editable("COMPLETED")


def test_start_date_locked_after_launch_and_window_order():
    changed = changed_settings({"start_date": date(2026, 11, 1)}, {"start_date": date(2026, 11, 2)})
    with pytest.raises(EditRefused, match="start date"):
        check_settings("ACTIVE", changed, {"start_date": date(2026, 11, 2)})
    check_settings("DRAFT", changed, {"start_date": date(2026, 11, 2)})
    with pytest.raises(EditRefused, match="send_window_end"):
        check_settings("DRAFT", {"send_window_end": time(8)}, {"send_window_start": time(9), "send_window_end": time(8)})
    with pytest.raises(EditRefused, match="timezone"):
        check_settings("DRAFT", {"campaign_timezone": "Mars/Base"}, {"campaign_timezone": "Mars/Base"})
    assert changed_settings({"a": 1}, {"a": 1}) == {}


def test_plan_steps_add_delete_and_refuse_reorder():
    existing = [("s1", 1), ("s2", 2), ("s3", 3)]
    plan = plan_steps(existing, [StepEdit("s1"), StepEdit(None, 2), StepEdit("s3")])
    assert [(sid, n) for sid, n, _ in plan.keep] == [("s1", 1), ("s3", 3)]
    assert [n for n, _ in plan.add] == [2] and plan.delete == ("s2",) and not plan.renumbered
    plan = plan_steps(existing, [StepEdit("s1"), StepEdit("s3")])
    assert plan.delete == ("s2",) and plan.renumbered
    with pytest.raises(EditRefused, match="reordered"):
        plan_steps(existing, [StepEdit("s2"), StepEdit("s1")])
    with pytest.raises(EditRefused):
        plan_steps(existing, [])
    with pytest.raises(EditRefused, match="Unknown"):
        plan_steps(existing, [StepEdit("nope")])
    with pytest.raises(EditRefused, match="Maximum"):
        plan_steps(existing, [StepEdit(None)] * 8)


def test_renumber_moves_to_the_next_surviving_step():
    old_to_new = {1: 1, 3: 2}  # step 2 deleted
    assert [renumber(n, old_to_new) for n in (1, 2, 3, 4)] == [1, 2, 2, 2]


def test_pick_template_prefers_persona_then_generic_then_any():
    t = lambda tid, seq, d: SimpleNamespace(template_id=tid, sequence_id=seq, designation=d)  # noqa: E731
    m = build_template_map([t("own", "s1", "CFO"), t("gen", "s1", None), t("other", "s2", "CTO")])
    assert pick_template(m, "s1", "CFO").template_id == "own"
    assert pick_template(m, "s1", "CEO").template_id == "gen"
    assert pick_template(m, "s2", "CEO").template_id == "other"
    assert pick_template(m, "s3", "CEO") is None


def test_wait_until_calendar_or_business_days():
    friday = datetime(2026, 10, 30, 9)
    assert wait_until(friday, 1, settings()) == datetime(2026, 10, 31, 9)
    assert wait_until(friday, 1, settings(wait_business_days=True)) == datetime(2026, 11, 2, 9)


def test_plan_contact_keeps_step_order_and_waits():
    now = datetime(2026, 10, 26, 8)  # Monday
    steps = [("s1", 0), ("s2", 2), ("s3", 3)]
    mails = {"s1": StepMail("m1", "SENT", sent_at=datetime(2026, 10, 26, 10)),
             "s2": StepMail("m2", "SCHEDULED", scheduled_at=datetime(2026, 10, 27, 9)),
             "s3": StepMail("m3", "PAUSED_BY_CAMPAIGN", scheduled_at=datetime(2026, 10, 28, 9))}
    plan = ContactPlan(steps=steps, mails=mails, timezone="UTC", index=0, total=1, fresh_base=now)
    out = plan_contact(plan, settings(), now)
    assert set(out) == {"m2", "m3"}
    assert out["m2"].date() == date(2026, 10, 28) and out["m3"].date() == date(2026, 11, 2)  # Sat -> Mon
    assert out["m2"] < out["m3"]
    # SENDING / SENT are never moved; only=... limits the moves
    mails["s2"] = StepMail("m2", "SENDING", scheduled_at=datetime(2026, 10, 27, 9))
    assert set(plan_contact(ContactPlan(steps, mails, "UTC", 0, 1, now), settings(), now)) == {"m3"}
    assert plan_contact(ContactPlan(steps, mails, "UTC", 0, 1, now), settings(), now, only=set()) == {}


def test_fresh_contacts_and_passed_steps():
    assert starts_fresh([("s1", 0)], {"s1": StepMail("m1", "SCHEDULED")})
    assert not starts_fresh([("s1", 0)], {"s1": StepMail("m1", "SENT", sent_at=datetime(2026, 1, 1))})
    assert passed_step(["s1", "s2", "s3"], "s2", {"s3"})
    assert not passed_step(["s1", "s2", "s3"], "s2", {"s1"})


def test_launch_times_follow_cumulative_waits():
    base = datetime(2026, 10, 26, 8)
    out = launch_times([("s1", 0), ("s2", 2)], set(), base, "UTC", settings(), 0, 1)
    assert out["s1"] == datetime(2026, 10, 26, 9) and out["s2"].date() == date(2026, 10, 28)
    assert launch_times([("s1", 0), ("s2", 2)], {"s1"}, base, "UTC", settings(), 0, 1).keys() == {"s2"}


def test_pacing_warning_when_a_day_outruns_the_mailboxes():
    minutes = window_minutes(time(9), time(17))
    assert minutes == 480
    assert inbox_daily_capacity(500, 60, minutes) == 480  # window / delay caps the daily limit
    assert inbox_daily_capacity(100, 60, minutes) == 100
    assert pacing_warning({date(2026, 11, 2): 150}, [100, 100]) is None
    warning = pacing_warning({date(2026, 11, 2): 300, date(2026, 11, 3): 250}, [100, 100])
    assert warning.planned_peak_per_day == 300 and warning.capacity_per_day == 200
    assert warning.days_over_capacity == 2 and warning.as_dict()["peak_day"] == "2026-11-02"
