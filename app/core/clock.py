"""The one clock for application code (chunk D2): current time in UTC.

``utcnow()`` returns a *naive* datetime in UTC, the same value ``datetime.utcnow()`` gave (deprecated
since Python 3.12). Naive on purpose: the database columns are ``DATETIME(6)`` holding UTC with no zone,
and the code compares database values with this one, so an aware value would raise TypeError there.
Every connection runs with ``time_zone = '+00:00'`` so ``CURRENT_TIMESTAMP`` defaults agree with it.

Tests can monkeypatch ``app.core.clock.utcnow`` only where callers use ``clock.utcnow()``; most call
sites import the function, so freeze time by passing ``now`` explicitly where a function accepts it.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time, naive (no tzinfo), microsecond precision."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
