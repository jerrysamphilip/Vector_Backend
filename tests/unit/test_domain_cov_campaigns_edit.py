"""Coverage of campaign edit / schedule edges (app/domain/campaigns_edit.py)."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app.domain.campaigns_edit import (FOLLOW_UP_MIN_GAP, ContactPlan, EditRefused, ScheduleSettings, SendCalendar,
                                       StepEdit, StepMail, StepPlan, check_settings, contact_timezone, fresh_start,
                                       pacing_warning, plan_contact, plan_steps, schedule_base, wait_until,
                                       window_minutes, zone)

NOW = datetime(2026, 3, 2, 12, 0)  # a Monday


class Calendar:
    """Weekdays only for business days; the window keeps the base time; NY maps to New York."""

    def __init__(self, place=None):
        self.place = place or (lambda base, tz, settings, index, total: base)
        self.batch_calls = []

    def add_business_days(self, moment, days):
        while days:
            moment += timedelta(days=1)
            if moment.weekday() < 5:
                days -= 1
        return moment

    def batch_day_start(self, base, day_offset):
        self.batch_calls.append(day_offset)
        return self.add_business_days(base, day_offset)

    def timezone_for_state(self, state, default):
        return {"NY": "America/New_York", "XX": None}.get(state, default)

    def place_in_window(self, base, timezone, settings, index, total):
        return self.place(base, timezone, settings, index, total)


def test_end_date_before_start_date_is_refused():
    merged = {"start_date": date(2026, 3, 10), "end_date": date(2026, 3, 9)}
    with pytest.raises(EditRefused, match="end_date"):
        check_settings("DRAFT", {"end_date": merged["end_date"]}, merged)
    check_settings("DRAFT", {}, {"start_date": date(2026, 3, 10), "end_date": date(2026, 3, 10)})


def test_zone_falls_back_to_utc_unless_strict():
    assert zone("Not/AZone").key == "UTC"
    assert zone(None).key == "UTC"
    assert zone("Europe/Paris").key == "Europe/Paris"
    with pytest.raises(EditRefused, match="Unknown timezone"):
        zone("Not/AZone", strict=True)


def test_step_plan_changes_order_only_when_steps_move():
    existing = [("s1", 1), ("s2", 2)]
    same = plan_steps(existing, [StepEdit("s1"), StepEdit("s2")])
    assert same.changes_order is False
    assert plan_steps(existing, [StepEdit("s1"), StepEdit(), StepEdit("s2")]).changes_order is True
    assert plan_steps(existing, [StepEdit("s2")]).changes_order is True
    assert StepPlan(renumbered=True).changes_order is True


def test_a_step_listed_twice_is_refused():
    with pytest.raises(EditRefused, match="twice"):
        plan_steps([("s1", 1)], [StepEdit("s1"), StepEdit("s1")])


def test_settings_without_calendar_cannot_plan():
    with pytest.raises(ValueError, match="calendar"):
        _ = ScheduleSettings().cal
    with pytest.raises(ValueError, match="calendar"):
        wait_until(NOW, 2, ScheduleSettings(wait_business_days=True))
    assert wait_until(NOW, 2, ScheduleSettings()) == NOW + timedelta(days=2)


def test_settings_of_a_campaign_applies_defaults_and_floors():
    campaign = SimpleNamespace(campaign_timezone=None, send_window_start=time(9), send_window_end=time(17),
                               sending_mode=None, min_gap_minutes=-5, batch_size=10, batch_gap_minutes=0,
                               daily_batch_size=50, respect_timezone=1, start_date=date(2026, 3, 9))
    cal = Calendar()
    s = ScheduleSettings.of(campaign, cal)
    assert (s.timezone, s.sending_mode, s.min_gap_minutes, s.batch_gap_minutes) == ("UTC", "spread", 0, 30)
    assert s.respect_timezone is True and s.wait_business_days is False and s.cal is cal
    assert s.window_start == time(9) and s.daily_batch_size == 50 and s.start_date == date(2026, 3, 9)
    campaign.batch_gap_minutes, campaign.wait_business_days = -10, True
    s2 = ScheduleSettings.of(campaign)
    assert s2.batch_gap_minutes == 1 and s2.wait_business_days is True and s2.calendar is None


def test_schedule_base_is_the_later_of_now_and_local_midnight_of_the_start_date():
    assert schedule_base(NOW, ScheduleSettings()) == NOW
    later = ScheduleSettings(timezone="America/New_York", start_date=date(2026, 3, 9))
    assert schedule_base(NOW, later) == datetime(2026, 3, 9, 4, 0)  # midnight EDT (DST began 8 March)
    earlier = ScheduleSettings(timezone="UTC", start_date=date(2026, 1, 1))
    assert schedule_base(NOW, earlier) == NOW


def test_fresh_start_moves_every_daily_batch_to_the_next_business_day():
    cal = Calendar()
    s = ScheduleSettings(daily_batch_size=2, calendar=cal)
    friday = datetime(2026, 3, 6, 9, 0)
    assert fresh_start(friday, s, 1) == friday
    assert fresh_start(friday, s, 2) == datetime(2026, 3, 9, 9, 0)
    assert cal.batch_calls == [0, 1]
    assert fresh_start(friday, ScheduleSettings(daily_batch_size=0), 99) == friday
    assert fresh_start(friday, ScheduleSettings(), 99) == friday


def test_contact_timezone_uses_the_state_only_when_respected():
    s = ScheduleSettings(timezone="Europe/Paris", calendar=Calendar())
    assert contact_timezone(s, "NY") == "America/New_York"
    assert contact_timezone(s, "XX") == "Europe/Paris"
    assert contact_timezone(s, None) == "Europe/Paris"
    off = ScheduleSettings(timezone="Europe/Paris", respect_timezone=False)
    assert contact_timezone(off, "NY") == "Europe/Paris"


def _plan(steps, mails, fresh_base=NOW):
    return ContactPlan(steps=steps, mails=mails, timezone="UTC", index=0, total=1, fresh_base=fresh_base)


def test_plan_contact_skips_steps_without_mail_but_keeps_their_wait():
    s = ScheduleSettings(calendar=Calendar())
    plan = _plan([("s1", 0), ("s2", 2), ("s3", 1)], {"s3": StepMail("m3", "QUEUED")})
    assert plan_contact(plan, s, NOW) == {"m3": NOW + timedelta(days=3)}


def test_a_failed_mail_neither_moves_nor_anchors_the_next_step():
    s = ScheduleSettings(calendar=Calendar())
    mails = {"s1": StepMail("m1", "FAILED"), "s2": StepMail("m2", "SCHEDULED")}
    out = plan_contact(_plan([("s1", 0), ("s2", 1)], mails), s, NOW)
    assert out == {"m2": NOW + timedelta(days=1)}  # counted from the fresh base, not from m1


def test_a_follow_up_placed_before_its_previous_mail_is_pushed_just_after_it():
    sent = datetime(2026, 3, 2, 15, 0)
    s = ScheduleSettings(calendar=Calendar(place=lambda base, *_: base.replace(hour=9, minute=0)))
    mails = {"s1": StepMail("m1", "SENT", sent_at=sent), "s2": StepMail("m2", "QUEUED")}
    out = plan_contact(_plan([("s1", 0), ("s2", 0)], mails), s, NOW)
    assert out == {"m2": sent + FOLLOW_UP_MIN_GAP}


def test_window_minutes_without_a_window_is_the_whole_day():
    assert window_minutes(None, time(17)) == 24 * 60
    assert window_minutes(time(9), None) == 24 * 60
    assert window_minutes(time(9), time(17, 30)) == 510
    assert window_minutes(time(17), time(9)) == 0


def test_pacing_warning_needs_planned_days():
    assert pacing_warning({}, [100]) is None


def test_send_calendar_protocol_stub_returns_none():
    assert SendCalendar.place_in_window(Calendar(), NOW, "UTC", ScheduleSettings(), 0, 1) is None
