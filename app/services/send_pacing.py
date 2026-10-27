"""
Per-mailbox pacing without waiting (BR-DF-01, F021).

A mailbox is ready to send when the gap since its previous send has passed. The gap is the
mailbox's delay_between_emails varied by up to +/- a quarter of it (at most
SENDING_JITTER_MAX_SECONDS), or, for a mailbox with no delay of its own, a random
SENDING_JITTER_MIN..MAX seconds, so the rhythm is not machine-regular. The gap is drawn once, when
a send happens, so readiness does not flicker between checks.

The scheduler never sleeps on a gap: a mailbox that is not ready is skipped for this pass and its
email stays due (in order) for a later pass. One slow mailbox therefore never holds up another
mailbox or tenant.

Pure functions: the caller supplies the clocks.
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Optional


def jitter_spread(delay: float, jitter_max: float) -> float:
    """How far one gap may differ from the configured delay."""
    return max(0.0, min(float(jitter_max), float(delay) * 0.25))


def next_gap(delay: Optional[float], jitter_min: float, jitter_max: float,
             rng: Optional[random.Random] = None) -> float:
    """Seconds this mailbox must wait after the send that just happened."""
    draw = (rng or random).uniform
    delay = float(delay or 0)
    if delay > 0:
        spread = jitter_spread(delay, jitter_max)
        return max(0.0, delay + draw(-spread, spread))
    if jitter_max > 0 and jitter_min < jitter_max:
        return draw(float(jitter_min), float(jitter_max))
    return 0.0


def seconds_until_ready(delay: Optional[float], jitter_max: float, *, now: datetime,
                        last_sent_at: Optional[datetime], now_mono: float,
                        ready_at_mono: Optional[float]) -> float:
    """
    0 when the mailbox may send now, else how many seconds are left.

    ``ready_at_mono`` is this process's own record (monotonic clock: the exact jittered gap drawn
    at its last send). ``last_sent_at`` is the database's record, which also covers sends made by
    another process (the API's "send now"); against it the shortest allowed gap applies (delay
    minus the jitter spread), so the in-process jitter still averages out to the configured pace.
    """
    left = 0.0
    if ready_at_mono is not None:
        left = max(left, ready_at_mono - now_mono)
    delay = float(delay or 0)
    if delay > 0 and last_sent_at is not None:
        shortest = delay - jitter_spread(delay, jitter_max)
        left = max(left, shortest - (now - last_sent_at).total_seconds())
    return max(0.0, left)
