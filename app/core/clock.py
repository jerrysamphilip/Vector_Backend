"""The one clock for application code (chunk D2): current time in UTC.

``utcnow()`` returns a *naive* datetime in UTC, the same value ``datetime.utcnow()`` gave (deprecated
since Python 3.12). Naive on purpose: the database columns are ``DATETIME(6)`` holding UTC with no zone,
and the code compares database values with this one, so an aware value would raise TypeError there.
Every connection runs with ``time_zone = '+00:00'`` so ``CURRENT_TIMESTAMP`` defaults agree with it.

``iso_utc()`` is how a naive UTC value leaves the API (``...Z``); ``to_utc_naive()`` brings a zone-aware value
back to the database form (SES timestamps: app.domain.ses_events.parse_ses_timestamp).

Tests can monkeypatch ``app.core.clock.utcnow`` only where callers use ``clock.utcnow()``; most call
sites import the function, so freeze time by passing ``now`` explicitly where a function accepts it.
"""
from datetime import datetime, timezone
from typing import Optional, overload


def utcnow() -> datetime:
    """Current UTC time, naive (no tzinfo), microsecond precision."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(value: datetime) -> datetime:
    """The database form of a datetime: naive UTC. Naive values are taken as UTC already; aware ones
    are converted."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@overload
def iso_utc(value: datetime) -> str: ...


@overload
def iso_utc(value: None) -> None: ...


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """ISO 8601 in UTC with a trailing ``Z`` for an API response (``2026-10-26T17:54:09.654321Z``), so a
    browser reads the instant correctly in any time zone. Naive values are UTC (the database convention);
    aware ones are converted. None stays None."""
    if value is None:
        return None
    return to_utc_naive(value).isoformat() + "Z"

