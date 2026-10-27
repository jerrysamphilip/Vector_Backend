"""
Sending-pipeline alert rules and mailbox capacity (S-11 / F-20). Pure functions: callers supply the
counts. Thresholds are named here so they are tuned in one place.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

SEVERITY_INFO = "INFO"
SEVERITY_WARNING = "WARNING"
SEVERITY_CRITICAL = "CRITICAL"

ALERT_NO_ACTIVE_MAILBOX = "NO_ACTIVE_MAILBOX"
ALERT_INBOX_LIMIT_REACHED = "INBOX_LIMIT_REACHED"
ALERT_SEND_FAILURE_RATE = "SEND_FAILURE_RATE"
ALERT_DELIVERY_EVENTS_MISSING = "DELIVERY_EVENTS_MISSING"
ALERT_SES_UNAVAILABLE = "SES_UNAVAILABLE"
ALERT_STUCK_SENDING = "STUCK_SENDING"

SENDING_ALERT_TYPES = (
    ALERT_NO_ACTIVE_MAILBOX,
    ALERT_INBOX_LIMIT_REACHED,
    ALERT_SEND_FAILURE_RATE,
    ALERT_DELIVERY_EVENTS_MISSING,
    ALERT_SES_UNAVAILABLE,
    ALERT_STUCK_SENDING,
)

# Failed sends over the last SEND_FAILURE_WINDOW_HOURS; fewer than SEND_FAILURE_MIN_COUNT is noise.
SEND_FAILURE_WINDOW_HOURS = 24
SEND_FAILURE_MIN_COUNT = 5
SEND_FAILURE_WARNING_PCT = 5.0
SEND_FAILURE_CRITICAL_PCT = 25.0

# Mail sent between DELIVERY_GAP_WINDOW_MINUTES and DELIVERY_CONFIRM_MINUTES ago should have a
# delivery, bounce or complaint report by now.
DELIVERY_GAP_WINDOW_MINUTES = 60
DELIVERY_CONFIRM_MINUTES = 15
DELIVERY_GAP_MIN_SAMPLE = 10
DELIVERY_GAP_PCT = 20.0

# SES unavailable: shown while the breaker is open and for this long after.
SES_OUTAGE_REMEMBER_MINUTES = 30

# The scheduler reaps SENDING rows after 15 minutes; one still there after twice that was not.
STUCK_SENDING_MINUTES = 30

DAY_SECONDS = 24 * 3600


def send_failure_severity(failed: int, sent: int) -> Optional[str]:
    """WARNING at >= 5 % of attempts failed, CRITICAL at >= 25 %, None below or with too few failures."""
    attempts = failed + sent
    if failed < SEND_FAILURE_MIN_COUNT or attempts <= 0:
        return None
    share = 100.0 * failed / attempts
    if share >= SEND_FAILURE_CRITICAL_PCT:
        return SEVERITY_CRITICAL
    if share >= SEND_FAILURE_WARNING_PCT:
        return SEVERITY_WARNING
    return None


def delivery_reports_missing(sample: int, unconfirmed: int) -> bool:
    return sample >= DELIVERY_GAP_MIN_SAMPLE and 100.0 * unconfirmed / sample >= DELIVERY_GAP_PCT


def ses_outage_severity(open_until: Optional[datetime], deferred_recently: int, now: datetime) -> Optional[str]:
    """CRITICAL while the SES breaker is open (or mail is being deferred for platform failures right
    now), WARNING for SES_OUTAGE_REMEMBER_MINUTES after it closed, else None."""
    if (open_until is not None and open_until > now) or deferred_recently > 0:
        return SEVERITY_CRITICAL
    if open_until is not None and now - open_until <= timedelta(minutes=SES_OUTAGE_REMEMBER_MINUTES):
        return SEVERITY_WARNING
    return None


def daily_capacity(daily_limit: Optional[int], delay_between_emails: Optional[int],
                   window_seconds: int = DAY_SECONDS) -> int:
    """Emails a mailbox can send in a day: its effective limit, capped by its pacing
    (one email every delay_between_emails seconds across the sending window)."""
    capacity = max(0, int(daily_limit or 0))
    delay = int(delay_between_emails or 0)
    if delay > 0:
        capacity = min(capacity, max(0, window_seconds) // delay)
    return capacity


def remaining_today(capacity: int, sent_today: int) -> int:
    return max(0, capacity - max(0, sent_today))
