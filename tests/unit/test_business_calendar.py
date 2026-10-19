"""US business-day and send-window logic (app/utils/business_calendar.py), with fixed datetimes.

Reference calendar (2026): Fri Oct 16, Sat Oct 17, Sun Oct 18, Mon Oct 19; Thanksgiving Thu Nov 26;
Christmas Fri Dec 25; Independence Day Sat Jul 4 -> observed Fri Jul 3. New York is UTC-4 until
Sun Nov 1 2026 and UTC-5 after; Los Angeles is UTC-7 in October.
"""
from datetime import date, datetime, time, timedelta

import pytest

from app.core.config import settings
from app.utils import business_calendar as bc


@pytest.fixture
def no_jitter(monkeypatch):
    """calculate_send_time always adds random seconds; pin random.randint to its lower bound."""
    monkeypatch.setattr(bc.random, "randint", lambda a, b: a)


@pytest.fixture
def weekdays_only(monkeypatch):
    monkeypatch.setattr(settings, "SEND_ON_ANY_DAY", False)


# ── States and time zones ────────────────────────────────────

@pytest.mark.parametrize("value,code", [("CA", "CA"), ("ca", "CA"), ("California", "CA"), ("CALIFORNIA", "CA"),
                                        ("Calif", "CA"), ("new york", "NY"), (" Texas ", "TX"), ("Ohio", "OH"),
                                        ("district of columbia", "DC")])
def test_normalize_state_code(value, code):
    assert bc.normalize_state_code(value) == code


@pytest.mark.parametrize("value", [None, "", "ZZ", "Ontario"])
def test_normalize_state_code_unknown(value):
    assert bc.normalize_state_code(value) is None


def test_timezone_priority_explicit_then_state_then_default():
    assert bc.get_timezone_for_state(state="CA", timezone="Europe/London") == "Europe/London"
    assert bc.get_timezone_for_state(state="CA", timezone="Mars/Base") == "America/Los_Angeles"
    assert bc.get_timezone_for_state(state="Texas") == "America/Chicago"
    assert bc.get_timezone_for_state(state="AZ") == "America/Phoenix"
    assert bc.get_timezone_for_state() == bc.DEFAULT_TIMEZONE == "America/New_York"


# ── Weekends and holidays ────────────────────────────────────

def test_weekend_detection():
    assert bc.is_weekend(datetime(2026, 10, 17)) and bc.is_weekend(datetime(2026, 10, 18))
    assert not bc.is_weekend(datetime(2026, 10, 16))


def test_us_holidays_including_observed_dates():
    assert bc.is_us_holiday(datetime(2026, 12, 25))
    assert "Christmas" in bc.get_holiday_name(datetime(2026, 12, 25))
    assert bc.is_us_holiday(datetime(2026, 11, 26))           # Thanksgiving
    assert bc.is_us_holiday(datetime(2026, 7, 3))             # Independence Day (observed)
    assert not bc.is_us_holiday(datetime(2026, 10, 14))
    assert bc.get_holiday_name(datetime(2026, 10, 14)) is None


def test_business_day():
    assert bc.is_business_day(datetime(2026, 10, 13))
    assert not bc.is_business_day(datetime(2026, 10, 17))
    assert not bc.is_business_day(datetime(2026, 12, 25))


def test_next_business_day_skips_weekend_with_reason():
    nxt, reason = bc.get_next_business_day(datetime(2026, 10, 17, 10, 0))
    assert nxt == datetime(2026, 10, 19, 10, 0)
    assert "Saturday 2026-10-17" in reason and "Sunday 2026-10-18" in reason


def test_next_business_day_skips_holiday_then_weekend():
    nxt, reason = bc.get_next_business_day(datetime(2026, 12, 25, 9, 0))
    assert nxt == datetime(2026, 12, 28, 9, 0)
    assert "Christmas" in reason


def test_next_business_day_is_identity_on_a_business_day():
    assert bc.get_next_business_day(datetime(2026, 10, 13, 9, 0)) == (datetime(2026, 10, 13, 9, 0), None)


def test_add_business_days():
    assert bc.add_business_days(datetime(2026, 10, 16, 9, 0), 1) == datetime(2026, 10, 19, 9, 0)
    assert bc.add_business_days(datetime(2026, 10, 17, 9, 0), 0) == datetime(2026, 10, 19, 9, 0)
    # Thanksgiving week: Tue(1) Wed(2) [Thu holiday] Fri(3) Mon(4) Tue Dec 1(5)
    assert bc.add_business_days(datetime(2026, 11, 23, 9, 0), 5) == datetime(2026, 12, 1, 9, 0)


def test_batch_day_start_counts_from_the_first_business_day():
    # Start on Saturday 2026-11-07: batches Mon 9, Tue 10, [Wed 11 Veterans Day] Thu 12, Fri 13, Mon 16 (OUT-031)
    sat = datetime(2026, 11, 7, 0, 0)
    days = [bc.batch_day_start(sat, n) for n in range(1, 5)]
    assert days == [datetime(2026, 11, 10), datetime(2026, 11, 12), datetime(2026, 11, 13), datetime(2026, 11, 16)]
    assert bc.batch_day_start(sat, 0) == sat  # batch 0 is placed by the send-window planner (next business day)
    # A business-day start is unchanged by the fix
    mon = datetime(2026, 10, 19, 9, 0)
    assert [bc.batch_day_start(mon, n) for n in range(3)] == [mon, datetime(2026, 10, 20, 9, 0), datetime(2026, 10, 21, 9, 0)]


# ── calculate_send_time ──────────────────────────────────────

def test_send_time_before_window_goes_out_at_window_start(no_jitter):
    # 10:00 UTC = 06:00 New York -> 09:00 New York = 13:00 UTC
    assert bc.calculate_send_time(datetime(2026, 10, 13, 10, 0), 0, state="NY") == datetime(2026, 10, 13, 13, 0)


def test_send_time_inside_window_goes_out_now(no_jitter):
    assert bc.calculate_send_time(datetime(2026, 10, 13, 15, 30), 0, state="NY") == datetime(2026, 10, 13, 15, 30)


def test_send_time_after_window_goes_to_next_morning(no_jitter):
    assert bc.calculate_send_time(datetime(2026, 10, 13, 23, 0), 0, state="NY") == datetime(2026, 10, 14, 13, 0)


def test_send_time_friday_evening_moves_to_monday(no_jitter):
    assert bc.calculate_send_time(datetime(2026, 10, 16, 22, 0), 0, state="NY") == datetime(2026, 10, 19, 13, 0)


def test_send_time_wait_days_are_business_days(no_jitter):
    assert bc.calculate_send_time(datetime(2026, 10, 13, 10, 0), 2, state="NY") == datetime(2026, 10, 15, 13, 0)
    # Fri Oct 16 + 1 business day = Mon Oct 19
    assert bc.calculate_send_time(datetime(2026, 10, 16, 10, 0), 1, state="NY") == datetime(2026, 10, 19, 13, 0)


def test_send_time_uses_the_prospects_time_zone(no_jitter):
    # 10:00 UTC = 03:00 Los Angeles -> 09:00 PDT = 16:00 UTC
    assert bc.calculate_send_time(datetime(2026, 10, 13, 10, 0), 0, state="California") == datetime(2026, 10, 13, 16, 0)


def test_send_time_after_dst_change(no_jitter):
    # Mon Nov 2 2026, New York on EST (UTC-5): 09:00 local = 14:00 UTC
    assert bc.calculate_send_time(datetime(2026, 11, 2, 12, 0), 0, state="NY") == datetime(2026, 11, 2, 14, 0)


def test_send_time_jitter_is_bounded(monkeypatch):
    monkeypatch.setattr(bc.random, "randint", lambda a, b: b)   # maximum jitter: +5 min, +59 s
    assert bc.calculate_send_time(datetime(2026, 10, 13, 10, 0), 0, state="NY") == datetime(2026, 10, 13, 13, 5, 59)


def test_send_time_with_window_uses_the_window_start(no_jitter):
    got = bc.calculate_send_time_with_window(datetime(2026, 10, 13, 10, 0), 0, state="NY",
                                             send_window_start=time(10, 30))
    assert got == datetime(2026, 10, 13, 14, 30)


# ── calculate_spread_send_time ───────────────────────────────

def test_spread_send_time_caps_the_gap_at_min_gap():
    got = bc.calculate_spread_send_time(time(9, 0), "America/New_York", date(2026, 10, 13), total_prospects=10,
                                        prospect_index=3, send_window_end=time(17, 0), min_gap_minutes=2)
    assert got == datetime(2026, 10, 13, 13, 6)


def test_spread_send_time_without_end_spaces_by_min_gap():
    got = bc.calculate_spread_send_time(time(9, 0), "America/New_York", date(2026, 10, 13), total_prospects=10,
                                        prospect_index=3, min_gap_minutes=5)
    assert got == datetime(2026, 10, 13, 13, 15)


def test_spread_send_time_rolls_weekend_to_monday_window_start():
    got = bc.calculate_spread_send_time(time(9, 0), "America/New_York", date(2026, 10, 17), total_prospects=1,
                                        prospect_index=0, send_window_end=time(17, 0))
    assert got == datetime(2026, 10, 19, 13, 0)


def test_spread_send_time_invalid_zone_falls_back_to_new_york():
    got = bc.calculate_spread_send_time(time(9, 0), "Nope/Zone", datetime(2026, 10, 13, 20, 0), total_prospects=1,
                                        prospect_index=0, send_window_end=time(17, 0))
    assert got == datetime(2026, 10, 13, 13, 0)


# ── Send window ──────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, time(8, 0)), (time(9, 30), time(9, 30)), (timedelta(hours=9, minutes=30), time(9, 30)),
    (timedelta(hours=25, seconds=5), time(1, 0, 5)), ("09:00", time(8, 0))])
def test_coerce_time(value, expected):
    assert bc._coerce_time(value, time(8, 0)) == expected


def test_within_send_window_on_a_weekday(weekdays_only):
    ny, start, end = "America/New_York", time(9, 0), time(17, 0)
    assert bc.is_within_send_window(datetime(2026, 10, 13, 14, 0), ny, start, end)         # 10:00 local
    assert bc.is_within_send_window(datetime(2026, 10, 13, 13, 0), ny, start, end)         # 09:00 (inclusive)
    assert bc.is_within_send_window(datetime(2026, 10, 13, 21, 0), ny, start, end)         # 17:00 (inclusive)
    assert not bc.is_within_send_window(datetime(2026, 10, 13, 22, 0), ny, start, end)     # 18:00
    assert not bc.is_within_send_window(datetime(2026, 10, 13, 12, 59), ny, start, end)    # 08:59


def test_within_send_window_null_windows_default_to_9_to_17(weekdays_only):
    assert bc.is_within_send_window(datetime(2026, 10, 13, 14, 0), "America/New_York", None, None)
    assert not bc.is_within_send_window(datetime(2026, 10, 13, 22, 0), "America/New_York", None, None)
    assert bc.is_within_send_window(datetime(2026, 10, 13, 14, 0), "America/New_York",
                                    timedelta(hours=9), timedelta(hours=17))


def test_within_send_window_refuses_weekends_and_holidays(weekdays_only):
    assert not bc.is_within_send_window(datetime(2026, 10, 17, 14, 0), "America/New_York", time(9), time(17))
    assert not bc.is_within_send_window(datetime(2026, 12, 25, 14, 0), "America/New_York", time(9), time(17))


def test_send_on_any_day_opens_weekends_outside_production(monkeypatch):
    monkeypatch.setattr(settings, "SEND_ON_ANY_DAY", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    assert bc.is_within_send_window(datetime(2026, 10, 17, 14, 0), "America/New_York", time(9), time(17))
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert not bc.is_within_send_window(datetime(2026, 10, 17, 14, 0), "America/New_York", time(9), time(17))


def test_within_send_window_invalid_zone_uses_utc(weekdays_only):
    assert bc.is_within_send_window(datetime(2026, 10, 13, 10, 0), "Nope/Zone", time(9), time(17))
    assert not bc.is_within_send_window(datetime(2026, 10, 13, 18, 0), "Nope/Zone", time(9), time(17))


# ── schedule_email_in_window ─────────────────────────────────

NY = "America/New_York"


def test_schedule_without_window_returns_base_naive():
    base = datetime(2026, 10, 13, 10, 0)
    assert bc.schedule_email_in_window(base, NY, None, None) == base


def test_schedule_spread_before_window():
    got = bc.schedule_email_in_window(datetime(2026, 10, 13, 11, 0), NY, time(9, 0), time(17, 0),
                                      "spread", prospect_index=3, total_prospects=10, min_gap_minutes=2)
    assert got == datetime(2026, 10, 13, 13, 6)


def test_schedule_accepts_string_windows():
    got = bc.schedule_email_in_window(datetime(2026, 10, 13, 11, 0), NY, "09:00", "17:00",
                                      "spread", prospect_index=3, total_prospects=10, min_gap_minutes=2)
    assert got == datetime(2026, 10, 13, 13, 6)


def test_schedule_batch_mode_groups_by_batch_size():
    kw = dict(sending_mode="batch", total_prospects=10, batch_size=5, batch_gap_minutes=30)
    base = datetime(2026, 10, 13, 11, 0)
    assert bc.schedule_email_in_window(base, NY, time(9), time(17), prospect_index=4, **kw) == datetime(2026, 10, 13, 13, 0)
    assert bc.schedule_email_in_window(base, NY, time(9), time(17), prospect_index=7, **kw) == datetime(2026, 10, 13, 13, 30)


def test_schedule_batch_offset_is_capped_inside_the_window():
    got = bc.schedule_email_in_window(datetime(2026, 10, 13, 11, 0), NY, time(9), time(10), "batch",
                                      prospect_index=5, total_prospects=10, batch_size=1, batch_gap_minutes=30)
    assert got == datetime(2026, 10, 13, 13, 59)


def test_schedule_random_mode_is_deterministic_distinct_and_on_the_gap_grid():
    base = datetime(2026, 10, 13, 11, 0)
    runs = [[bc.schedule_email_in_window(base, NY, time(9), time(17), "random", prospect_index=i,
                                         total_prospects=5, min_gap_minutes=2) for i in range(5)] for _ in range(2)]
    assert runs[0] == runs[1]
    assert len(set(runs[0])) == 5
    for t in runs[0]:
        assert datetime(2026, 10, 13, 13, 0) <= t <= datetime(2026, 10, 13, 21, 0)
        assert int((t - datetime(2026, 10, 13, 13, 0)).total_seconds() // 60) % 2 == 0


def test_schedule_mid_window_starts_at_the_next_minute():
    got = bc.schedule_email_in_window(datetime(2026, 10, 13, 15, 30, 30), NY, time(9), time(17), "spread",
                                      prospect_index=0, total_prospects=10, min_gap_minutes=2)
    assert got == datetime(2026, 10, 13, 15, 31)


def test_schedule_after_close_moves_to_next_business_morning():
    assert bc.schedule_email_in_window(datetime(2026, 10, 13, 22, 0), NY, time(9), time(17)) == datetime(2026, 10, 14, 13, 0)
    assert bc.schedule_email_in_window(datetime(2026, 10, 16, 22, 0), NY, time(9), time(17)) == datetime(2026, 10, 19, 13, 0)
