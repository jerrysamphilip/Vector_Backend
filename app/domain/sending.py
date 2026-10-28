"""
Sending engine: value objects and pure rules (chunk A8, ADR 0009).

The worker (app/services/email_scheduler_service.py), "send now" (campaign_send_now_service.py) and the
sender (email_sender_service.py) share these. No I/O: callers pass the clocks, the rows and the counts.

- Outcomes of one message pass (``SENT`` ... ``DEFERRED``) and the per-pass ``PassStats`` with the worker's
  next wake-up (``next_run_in``: the soonest paced mailbox, not a fake "processed" count).
- ``DueRow`` / ``lane_queues``: the fair, per-lane batch (B04) capped at about one cycle of each inbox's pace.
- ``send_key``: identity of "this campaign step to this address" (BR-DF-06); ``ClaimResult``.
- ``PacingState`` and the pacing maths (``next_gap`` / ``seconds_until_ready``, BR-DF-01, F021).
- ``SendAttempt``: the sender's answer for one message, typed.
- Message transitions (``requeue`` / ``fail`` / retry backoff) and the stuck-SENDING evidence rule (B08).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

# ── Outcomes of one message pass ────────────────────────────────────────────
SENT = "sent"
FAILED = "failed"
SUPPRESSED = "suppressed"
SKIPPED = "skipped"
PACED = "paced"  # due, but the inbox's gap since its last send has not passed: claim given back
DEFERRED = "deferred"  # platform / sender-identity problem: re-queued without spending retries
COUNTED_OUTCOMES = (SENT, FAILED, SUPPRESSED)  # anything else is tallied as "skipped"

# ── Statuses ────────────────────────────────────────────────────────────────
DUE_STATUSES = ("QUEUED", "SCHEDULED")
# A not-yet-sent step that must go out before the next step (B01)
PENDING_STEP_STATUSES = ("QUEUED", "SCHEDULED", "SENDING", "PAUSED_BY_CAMPAIGN")
# Outcomes the SES webhook may record on a row still SENDING: proof the provider accepted it
PROVIDER_OUTCOMES = ("DELIVERED", "BOUNCED", "COMPLAINED")
RESUME_STATUSES = ("PAUSED_BY_CAMPAIGN", "QUEUED", "SCHEDULED")
FOLLOW_UP_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")

SEND_KEY_MAX = 330
DEFAULT_MAX_RETRIES = 3
DEFAULT_LANE_CAP = 50  # rows per lane per cycle when the inbox has no pace of its own


# ── Per-pass statistics and the worker's next wake-up ───────────────────────

@dataclass
class PassStats:
    """Counters of one scheduler pass. ``recheck_in``: seconds until the soonest paced inbox is ready."""

    processed: int = 0
    sent: int = 0
    failed: int = 0
    suppressed: int = 0
    skipped: int = 0
    paced: int = 0
    recheck_in: Optional[float] = None

    def add(self, counts: Mapping[str, Any]) -> None:
        for key in ("processed", "sent", "failed", "suppressed", "skipped", "paced"):
            setattr(self, key, getattr(self, key) + int(counts.get(key, 0) or 0))

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"processed": self.processed, "sent": self.sent, "failed": self.failed,
                               "suppressed": self.suppressed, "skipped": self.skipped, "paced": self.paced}
        if self.recheck_in is not None:
            out["recheck_in"] = self.recheck_in
        out["next_run_in"] = next_run_in(out)
        return out


def tally_outcome(counts: Dict[str, int], outcome: str) -> None:
    """Count one message's outcome under its stats key (sent / failed / suppressed, else skipped)."""
    counts[outcome if outcome in COUNTED_OUTCOMES else SKIPPED] += 1


def soonest_ready(ready_at: Iterable[float], now_mono: float) -> Optional[float]:
    """Seconds until the soonest of these monotonic ready times (0.0 when none is in the future)."""
    pending = [at - now_mono for at in ready_at if at > now_mono]
    return min(pending) if pending else 0.0


BUSY_RECHECK_SECONDS = 2.0
IDLE_RECHECK_CEILING = 30.0


def next_run_in(stats: Mapping[str, Any], *, busy: float = BUSY_RECHECK_SECONDS,
                ceiling: float = IDLE_RECHECK_CEILING) -> Optional[float]:
    """
    When the worker loop should run again, in seconds; None means its normal interval.

    Messages were handled: come straight back (``busy``), so pace is set by the inboxes (BR-DF-01).
    Only pacing held mail back: wake when the soonest inbox is ready (at least ``busy``), unless that
    is ``ceiling`` or more away, when the normal interval is as good.
    """
    if stats.get("processed"):
        return busy
    if not stats.get("paced"):
        return None
    recheck = float(stats.get("recheck_in") or 0.0)
    if recheck >= ceiling:
        return None
    return max(busy, recheck)


# ── Fair batching (B04) ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class DueRow:
    """One due message: its lane is the sending inbox, or the campaign for mail with no inbox."""

    message_id: str
    inbox_id: Optional[str]
    lane: str


def lane_cap(delay: Optional[float], cycle_seconds: float) -> int:
    """Rows one lane takes per cycle: about ``cycle_seconds`` of sends at the inbox's pace."""
    delay = float(delay or 0)
    return max(1, math.ceil(cycle_seconds / delay)) if delay > 0 else DEFAULT_LANE_CAP


def lane_queues(due: Iterable[DueRow], delays: Mapping[str, Any], cycle_seconds: float) -> Dict[str, List[str]]:
    """Message ids per lane in due order, each lane trimmed to lane_cap() of its inbox's pace."""
    queues: Dict[str, List[str]] = defaultdict(list)
    lane_inbox: Dict[str, Optional[str]] = {}
    for row in due:
        queues[row.lane].append(row.message_id)
        lane_inbox[row.lane] = row.inbox_id
    for lane, ids in queues.items():
        inbox_id = lane_inbox.get(lane)
        del ids[lane_cap(delays.get(inbox_id) if inbox_id else None, cycle_seconds):]
    return dict(queues)


# ── Claim (BR-DF-06) ────────────────────────────────────────────────────────

def send_key(campaign_id: Optional[str], sequence_id: Optional[str], to_email: Optional[str]) -> Optional[str]:
    """Identity of 'this campaign step to this address'; None for ad-hoc mail."""
    if not (campaign_id and sequence_id and to_email):
        return None
    return f"{campaign_id}:{sequence_id}:{to_email.strip().lower()}"[:SEND_KEY_MAX]


class SendKeyConflict(ValueError):
    """A concurrent worker claimed the same campaign step for this address (the unique send_key)."""


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of taking ownership of a message: claimed, or a duplicate of ``duplicate_of`` (cancelled)."""

    claimed: bool
    duplicate: bool = False
    duplicate_of: Optional[str] = None


def duplicate_reason(original_id: Optional[str]) -> str:
    return "Duplicate: this step was already sent to this address" + (
        f" (message {original_id})" if original_id else "")


# ── Pacing (BR-DF-01, F021) ─────────────────────────────────────────────────

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


@dataclass(frozen=True)
class PacingState:
    """One inbox's pace: its configured delay, the database's last send, this process's ready time."""

    delay: Optional[float]
    last_sent_at: Optional[datetime]
    ready_at_mono: Optional[float] = None

    def wait(self, jitter_max: float, *, now: datetime, now_mono: float) -> float:
        return seconds_until_ready(self.delay, jitter_max, now=now, last_sent_at=self.last_sent_at,
                                   now_mono=now_mono, ready_at_mono=self.ready_at_mono)


def pace_shortfall(delay: Optional[float], daily_limit: int) -> Optional[int]:
    """Sends a day the inbox's delay allows when that is below its daily limit (a config mismatch), else None."""
    if not delay or delay <= 0:
        return None
    reachable = int(86400 / delay)
    return reachable if reachable < daily_limit else None


# ── The sender's answer ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SendAttempt:
    """What the sender returned for one message (send_with_retry's dict, typed)."""

    success: bool
    status: str = ""
    error: Optional[str] = None
    ses_message_id: Optional[str] = None
    internet_message_id: Optional[str] = None

    @classmethod
    def from_result(cls, result: Mapping[str, Any]) -> "SendAttempt":
        return cls(success=bool(result.get("success")), status=str(result.get("status", "") or ""),
                   error=result.get("error"), ses_message_id=result.get("ses_message_id"),
                   internet_message_id=result.get("internet_message_id"))

    @property
    def blocked(self) -> bool:
        """Refused before reaching SES (personal address, spam lint): the same content would be refused again."""
        return self.status.startswith("BLOCKED")

    @property
    def provider_message_id(self) -> Optional[str]:
        """The MIME Message-ID (IMAP sync matches replies on it), else SES's id."""
        return self.internet_message_id or self.ses_message_id


# ── Message transitions (no I/O: the caller stages and commits) ─────────────

def requeue(msg: Any, *, at: Optional[datetime] = None, retry_at: Optional[datetime] = None) -> None:
    """Back to QUEUED, the step's claim freed; optionally due again ``at`` / retried at ``retry_at``."""
    msg.status = "QUEUED"
    msg.send_key = None
    if at is not None:
        msg.scheduled_at = at
    if retry_at is not None:
        msg.next_retry_at = retry_at


def fail(msg: Any, reason: Optional[str], now: datetime, *, final: bool = True) -> None:
    """FAILED with the reason (and FAILED as the final status unless ``final`` is False)."""
    msg.status = "FAILED"
    msg.failure_reason = reason
    if final:
        msg.final_status, msg.final_status_at = "FAILED", now


def retry_delay(retry_count: int) -> timedelta:
    """Exponential backoff before the next attempt."""
    return timedelta(minutes=2 ** retry_count)


def retries_exhausted(retry_count: int, max_retries: Optional[int]) -> bool:
    return retry_count >= (max_retries or DEFAULT_MAX_RETRIES)


def provider_accepted(msg: Any, provider_event_ids: Iterable[str]) -> bool:
    """Evidence SES accepted a stuck SENDING message: a provider id, a delivery, an outcome or an event."""
    return bool(msg.ses_message_id or msg.provider_message_id or msg.delivered_at
                or msg.final_status in PROVIDER_OUTCOMES or msg.message_id in provider_event_ids)


INTERRUPTED_ACCEPTED = "Sending was interrupted after the provider accepted the message; recorded as sent."
INTERRUPTED_UNKNOWN = ("Sending was interrupted and the outcome is unknown; "
                       "not retried so the contact cannot receive it twice.")
