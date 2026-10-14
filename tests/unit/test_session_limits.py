"""Session limits decision logic (app/services/session_limits.py, ADR 0007): idle, absolute, reuse."""
from datetime import datetime, timedelta

import pytest

from app.services import session_limits as sl

NOW = datetime(2026, 10, 14, 12, 0, 0)
ABS = timedelta(hours=12)
IDLE = timedelta(minutes=30)
GRACE = timedelta(seconds=30)


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("  ", None), ("abc", None), ("-5", None), ("1.5", None),
    ("0", 0), ("1", 1), (" 42 ", 42), (str(10 ** 12), None),
])
def test_parse_activity_header(value, expected):
    assert sl.parse_activity_header(value) == expected


def test_no_activity_report_keeps_the_stored_time():
    stored = NOW - timedelta(minutes=20)
    assert sl.effective_last_activity(stored, NOW, None) == stored


def test_activity_report_moves_last_activity_forward_only():
    stored = NOW - timedelta(minutes=20)
    assert sl.effective_last_activity(stored, NOW, 60) == NOW - timedelta(seconds=60)
    assert sl.effective_last_activity(stored, NOW, 0) == NOW
    # An interaction older than the stored activity does not move it backwards
    assert sl.effective_last_activity(stored, NOW, 3600) == stored


def test_activity_never_in_the_future():
    stored = NOW + timedelta(minutes=5)  # clock skew between API replicas
    assert sl.effective_last_activity(stored, NOW, 0) == NOW


def test_within_limits():
    assert sl.limit_exceeded(now=NOW, session_started_at=NOW - timedelta(hours=11),
                             last_activity_at=NOW - timedelta(minutes=29), absolute=ABS, idle=IDLE) is None
    assert sl.limit_exceeded(now=NOW, session_started_at=NOW, last_activity_at=NOW - IDLE,
                             absolute=ABS, idle=IDLE) is None  # exactly at the idle limit


def test_idle_exceeded():
    assert sl.limit_exceeded(now=NOW, session_started_at=NOW - timedelta(hours=1),
                             last_activity_at=NOW - IDLE - timedelta(seconds=1),
                             absolute=ABS, idle=IDLE) == sl.REASON_IDLE


def test_absolute_exceeded_even_when_active():
    assert sl.limit_exceeded(now=NOW, session_started_at=NOW - ABS, last_activity_at=NOW,
                             absolute=ABS, idle=IDLE) == sl.REASON_ABSOLUTE


def test_absolute_wins_over_idle():
    assert sl.limit_exceeded(now=NOW, session_started_at=NOW - timedelta(hours=13),
                             last_activity_at=NOW - timedelta(hours=2), absolute=ABS, idle=IDLE) == sl.REASON_ABSOLUTE


def test_classify_active_race_and_reuse():
    assert sl.classify_presented(revoked_at=None, revoked_reason=None, now=NOW, grace=GRACE) is sl.Presented.ACTIVE
    recent = NOW - timedelta(seconds=10)
    assert sl.classify_presented(revoked_at=recent, revoked_reason=sl.REASON_ROTATED, now=NOW,
                                 grace=GRACE) is sl.Presented.RACE
    old = NOW - timedelta(seconds=31)
    assert sl.classify_presented(revoked_at=old, revoked_reason=sl.REASON_ROTATED, now=NOW,
                                 grace=GRACE) is sl.Presented.REUSE
    # Revoked for any other reason (logout, password change, idle, ...) is reuse even when recent
    for reason in (sl.REASON_LOGOUT, sl.REASON_IDLE, sl.REASON_REUSE, None):
        assert sl.classify_presented(revoked_at=recent, revoked_reason=reason, now=NOW,
                                     grace=GRACE) is sl.Presented.REUSE


def test_refresh_expiry_capped_by_absolute_limit():
    ttl = timedelta(days=7)
    assert sl.refresh_expiry(now=NOW, session_started_at=NOW, refresh_ttl=ttl, absolute=ABS) == NOW + ABS
    assert sl.refresh_expiry(now=NOW, session_started_at=NOW, refresh_ttl=timedelta(hours=1),
                             absolute=ABS) == NOW + timedelta(hours=1)


def test_refused_detail_and_messages():
    assert sl.SessionRefused(sl.CODE_INVALID, "Invalid or expired refresh token").detail == \
        "Invalid or expired refresh token"
    refused = sl.SessionRefused(sl.CODE_IDLE, sl.idle_message(IDLE))
    assert refused.detail == {"code": "SESSION_IDLE_TIMEOUT", "message": "Signed out after 30 minutes of inactivity"}
    assert refused.clear_cookies
    assert sl.idle_message(timedelta(minutes=1)) == "Signed out after 1 minute of inactivity"


def test_new_lineage_starts_a_family():
    a, b = sl.new_lineage(NOW), sl.new_lineage(NOW)
    assert a.session_id != b.session_id
    assert a.session_started_at == a.last_activity_at == NOW
