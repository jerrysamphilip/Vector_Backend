"""Pure helpers extracted from the campaign routers into app/services (no DB, no network)."""
from datetime import datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import BadRequestError, DomainError, OperationFailed
from app.services.campaign_query_service import build_lead_stats, build_schedule, pct, step_label
from app.services.campaign_schedule_service import (
    UTC,
    campaign_zone,
    describe_adjustment,
    jittered,
    snap_to_send_window,
)
from app.services.campaign_send_now_service import format_cursor, normalize_run_start, parse_cursor
from app.services.campaign_template_service import fallback_template

NY = ZoneInfo("America/New_York")
NINE, FIVE = time(9, 0), time(17, 0)


# ── campaign_query_service ───────────────────────────────────

def test_pct_rounds_and_guards_zero_denominator():
    assert pct(1, 3) == 33.33
    assert pct(5, 0) == 0.0
    assert pct(0, 4) == 0.0


def test_step_label():
    assert step_label(1) == "Initial Email"
    assert step_label(2) == "Follow-up 1"
    assert step_label(7) == "Follow-up 6"


def test_lead_stats_lists_non_zero_known_statuses_in_order_plus_other():
    stats = build_lead_stats(10, {"REPLIED": 2, "ACTIVE": 5, "BOUNCED": 0})
    assert stats == {"total": 10, "data": [{"name": "Active", "value": 5}, {"name": "Replied", "value": 2},
                                           {"name": "Other", "value": 3}]}
    assert build_lead_stats(0, {}) == {"total": 0, "data": []}


def _row(pid, status, sent=None, scheduled=None, seq="s1", first="Ann", last=None):
    return SimpleNamespace(message_id=f"m-{pid}-{status}", prospect_id=pid, status=status, sent_at=sent,
                           scheduled_at=scheduled, sequence_id=seq, subject_variant=None, first_name=first,
                           last_name=last, email=f"{pid}@x.io")


def test_schedule_buckets_by_day_step_and_state_counting_each_prospect_once():
    day = datetime(2031, 3, 4, 10, 0)
    rows = [
        _row("p1", "SENT", sent=day),
        _row("p1", "SENT", sent=day.replace(hour=11)),        # same prospect, same bucket -> once
        _row("p2", "QUEUED", scheduled=day),
        _row("p3", "SCHEDULED", scheduled=day, seq="s2"),
        _row("p4", "QUEUED"),                                  # no date -> skipped
        _row("p5", "FAILED", scheduled=datetime(2031, 3, 3, 9)),  # anything not SENT is QUEUED
    ]
    out = build_schedule(rows, {"s1": 1, "s2": 2})
    assert out["total_sent"] == 1 and out["total_scheduled"] == 3
    keys = [(b["date"], b["step_number"], b["status"], b["prospect_count"]) for b in out["schedule"]]
    assert keys == [("2031-03-03", 1, "QUEUED", 1), ("2031-03-04", 1, "SENT", 1),
                    ("2031-03-04", 1, "QUEUED", 1), ("2031-03-04", 2, "QUEUED", 1)]
    sent_bucket = out["schedule"][1]
    assert set(sent_bucket) == {"date", "status", "prospect_count", "step_number", "prospects"}
    assert sent_bucket["prospects"][0]["sent_at"] == day.isoformat() + "Z"  # UTC, named (app.core.clock.iso_utc)
    assert sent_bucket["prospects"][0]["name"] == "Ann"


def test_schedule_lists_at_most_five_prospects_per_bucket():
    day = datetime(2031, 3, 4, 10, 0)
    out = build_schedule([_row(f"p{i}", "QUEUED", scheduled=day) for i in range(8)], {})
    bucket = out["schedule"][0]
    assert bucket["step_number"] == 1  # unknown sequence -> step 1
    assert bucket["prospect_count"] == 8 and len(bucket["prospects"]) == 5


# ── campaign_send_now_service ────────────────────────────────

def test_send_now_cursor_round_trip():
    assert format_cursor(2, "abc") == "2:abc"
    assert format_cursor(None, "abc") == "0:abc"
    assert parse_cursor("2:abc") == (2, "abc")
    assert parse_cursor(None) is None and parse_cursor("") is None


@pytest.mark.parametrize("bad", ["x:1", "1", "1:", ":abc", "-1:abc", "1:" + "a" * 65])
def test_send_now_cursor_garbage_is_a_bad_request(bad):
    with pytest.raises(BadRequestError) as exc:
        parse_cursor(bad)
    assert exc.value.status_code == 400 and exc.value.detail == "Invalid cursor"


def test_router_cursor_parser_still_maps_to_http_400():
    from fastapi import HTTPException

    from app.routers.campaign_router import parse_send_now_cursor
    with pytest.raises(HTTPException) as exc:
        parse_send_now_cursor("nope")
    assert (exc.value.status_code, exc.value.detail) == (400, "Invalid cursor")


def test_run_start_is_naive_utc_microseconds():
    aware = datetime(2031, 3, 4, 12, 0, 5, 999, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert normalize_run_start(aware) == datetime(2031, 3, 4, 6, 30, 5, 999)
    assert normalize_run_start(datetime(2031, 3, 4, 1, 2, 3, 400)) == datetime(2031, 3, 4, 1, 2, 3, 400)
    assert normalize_run_start(None, now=datetime(2031, 1, 1, 0, 0, 0, 7)) == datetime(2031, 1, 1, 0, 0, 0, 7)
    assert normalize_run_start(datetime(2031, 1, 1, tzinfo=timezone.utc)).tzinfo is None


# ── campaign_schedule_service ────────────────────────────────

def test_snap_keeps_a_time_inside_the_window():
    # Tue 2031-03-04 15:00 UTC = 10:00 New York (EST)
    s = snap_to_send_window(datetime(2031, 3, 4, 15, 0, 42), NY, NINE, FIVE)
    assert (s.local.hour, s.local.minute, s.local.second) == (10, 0, 0)
    assert s.utc_naive == datetime(2031, 3, 4, 15, 0)
    assert s.adjusted is True  # seconds were dropped
    assert snap_to_send_window(datetime(2031, 3, 4, 15, 0), NY, NINE, FIVE).adjusted is False


def test_snap_before_window_moves_to_window_open_same_day():
    s = snap_to_send_window(datetime(2031, 3, 4, 12, 0), NY, NINE, FIVE)  # 07:00 local
    assert s.local.date().isoformat() == "2031-03-04" and (s.local.hour, s.local.minute) == (9, 0)
    assert s.utc_naive == datetime(2031, 3, 4, 14, 0) and s.adjusted


def test_snap_after_window_moves_to_next_business_day():
    s = snap_to_send_window(datetime(2031, 3, 4, 23, 0), NY, NINE, FIVE)  # 18:00 local Tue
    assert s.local.date().isoformat() == "2031-03-05" and (s.local.hour, s.local.minute) == (9, 0)


def test_snap_weekend_moves_to_monday_window_open():
    s = snap_to_send_window(datetime(2031, 3, 8, 15, 0, tzinfo=UTC), NY, NINE, FIVE)  # Saturday
    assert s.local.strftime("%A") == "Monday" and (s.local.hour, s.local.minute) == (9, 0)
    assert s.utc_naive == s.local.astimezone(UTC).replace(tzinfo=None)


def test_jitter_adds_minutes_wrapping_the_hour_and_day():
    assert jittered(datetime(2031, 3, 4, 10, 58), 5, 30) == datetime(2031, 3, 4, 11, 3, 30)
    assert jittered(datetime(2031, 3, 4, 23, 58), 5, 0) == datetime(2031, 3, 4, 0, 3, 0)


def test_unknown_campaign_timezone_falls_back_to_utc():
    assert campaign_zone("America/New_York") == NY
    assert campaign_zone("Not/AZone") == UTC


def test_adjustment_message_is_human_readable():
    msg = describe_adjustment(datetime(2031, 3, 5, 14, 7, tzinfo=NY), "America/New_York")
    assert msg.endswith("Emails rescheduled to Wednesday, March 5 at 2:07 PM (America/New_York).")


# ── campaign_template_service ────────────────────────────────

def test_fallback_template_adds_cta_line_only_with_a_link():
    with_link = fallback_template(2, "CFO", "https://cal.example.com/x")
    assert with_link["subject"] == "[Step 2] CFO Outreach" and with_link["model_used"] == "fallback"
    assert "Would you be open to a quick call?: https://cal.example.com/x" in with_link["body"]
    assert with_link["body"].startswith("Hi {{first_name}},") and with_link["body"].endswith("{{signature_block}}")
    assert "quick call" not in fallback_template(1, "CFO", None)["body"]


def test_operation_failed_is_a_500_domain_error():
    err = OperationFailed("AI generation failed: boom")
    assert isinstance(err, DomainError) and err.status_code == 500 and err.detail == "AI generation failed: boom"
