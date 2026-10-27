"""
Send-side protection when SES itself, or a mailbox's identity, is the problem (item 14).

Circuit breaker (platform failures)
    PLATFORM_FAILURE_THRESHOLD consecutive platform failures (SES account paused, throttling,
    service unavailable, configuration set missing, no connection) open the breaker: no campaign
    mail is sent for PLATFORM_COOLDOWN_SECONDS. After the cool-down one send is let through
    (half-open): a success closes the breaker, another platform failure opens it again at once.
    Any successful send resets the consecutive count.

Mailbox auto-pause (sender-identity rejections)
    SENDER_REJECTION_THRESHOLD identity rejections for one mailbox within SENDER_REJECTION_WINDOW
    (address / MAIL FROM domain not verified) set that mailbox PAUSED, record the reason in its
    activity log and tell the workspace admins. The alert centre shows a paused mailbox as an
    INBOX_PAUSED alert (app/services/alert_center_service.py); campaign mail moves to the
    campaign's other active mailboxes, or waits until one is active again.

The breaker and the counters are pure (unit-tested with a fake clock); acting on them (pausing the
mailbox, notifying) is record_deferred_failure() in app/services/email_scheduler_service.py.

State is in memory, per process. That is deliberate: campaign mail is sent by the single worker
process (app/worker.py runs the scheduler loop); the API's "send now" shares the same rules in its
own process. A restart closes the breaker and forgets the rejection counts, which only means a
broken platform is probed again: messages are re-queued, never lost or sent twice. The re-queue
count per message lives in the database (email_messages.deferred_count), so the cap on extra
attempts survives restarts.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional

PLATFORM_FAILURE_THRESHOLD = 5
PLATFORM_COOLDOWN_SECONDS = 300.0
SENDER_REJECTION_THRESHOLD = 5
SENDER_REJECTION_WINDOW = 30 * 60.0
# A message deferred this often (platform / sender problems, about 5 minutes apart: ~4 hours)
# is failed with the reason instead of waiting forever
MAX_DEFERRED_ATTEMPTS = 50
DEFER_MINUTES = 5



class CircuitBreaker:
    """Consecutive-failure breaker with a cool-down and a half-open probe. Thread-safe."""

    def __init__(self, threshold: int = PLATFORM_FAILURE_THRESHOLD,
                 cooldown: float = PLATFORM_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._half_open = False

    def is_open(self) -> bool:
        """True while sending must stay stopped. The first call after the cool-down half-opens it."""
        with self._lock:
            if self._opened_at is None:
                return False
            if self._clock() - self._opened_at < self.cooldown:
                return True
            self._opened_at = None
            self._half_open = True
            return False

    def retry_in(self) -> float:
        """Seconds until the breaker lets a send through again (0 when closed)."""
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self.cooldown - (self._clock() - self._opened_at))

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._half_open = False
            self._opened_at = None

    def record_failure(self) -> bool:
        """Count one platform failure; True when this one opened the breaker."""
        with self._lock:
            self._failures += 1
            if self._opened_at is None and (self._half_open or self._failures >= self.threshold):
                self._opened_at = self._clock()
                self._half_open = False
                self._failures = 0
                return True
            return False


class RejectionTracker:
    """Sliding-window count of sender-identity rejections per mailbox. Thread-safe."""

    def __init__(self, threshold: int = SENDER_REJECTION_THRESHOLD, window: float = SENDER_REJECTION_WINDOW,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def record(self, inbox_id: str) -> bool:
        """Count one rejection; True when the mailbox has reached the threshold (and resets its count)."""
        with self._lock:
            now = self._clock()
            hits = self._hits[inbox_id]
            hits.append(now)
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.threshold:
                hits.clear()
                return True
            return False

    def reset(self, inbox_id: Optional[str] = None) -> None:
        with self._lock:
            if inbox_id is None:
                self._hits.clear()
            else:
                self._hits.pop(inbox_id, None)


# Process-wide state (see the module docstring)
platform_breaker = CircuitBreaker()
sender_rejections = RejectionTracker()
