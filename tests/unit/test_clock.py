"""D2: app.core.clock.utcnow is naive UTC, matching what datetime.utcnow used to return."""
from datetime import datetime, timedelta, timezone

from app.core.clock import utcnow


def test_utcnow_is_naive_utc():
    now = utcnow()
    assert now.tzinfo is None
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(reference - now) < timedelta(seconds=5)
