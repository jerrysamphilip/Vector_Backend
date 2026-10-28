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

Pure functions: the caller supplies the clocks. They live in app/domain/sending.py (chunk A8);
this module re-exports them for existing callers.
"""
from __future__ import annotations

from app.domain.sending import PacingState, jitter_spread, next_gap, seconds_until_ready

__all__ = ["PacingState", "jitter_spread", "next_gap", "seconds_until_ready"]
