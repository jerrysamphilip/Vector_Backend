"""
The shared rate-limit store (DEC-09, ADR 0002): ``app.core.rate_limit.RateLimitStore`` over MySQL, so the
sign-in lockout, password-reset / register / two-factor limits are counted once across every API replica.
app.core sits below the repositories, so app.main installs it with ``app.core.rate_limit.configure``.

Each operation is one short READ COMMITTED transaction on its own pooled connection (never the request's
session): a hit is ``INSERT ... ON DUPLICATE KEY UPDATE hits = hits + 1`` on the current slot's row, which
row-locks it, then a read of the bucket's live slots; over the limit, the hit is taken back in the same
transaction (refused requests do not extend a lockout, as in the in-memory limiter). Concurrent hits on a
bucket therefore serialise on that row and can never admit more than the limit. Slot math:
app/core/rate_limit.py. Expired rows are deleted opportunistically, at most every PURGE_EVERY_SECONDS per
process. Errors propagate: ``FallbackLimiter`` in app.core.rate_limit degrades to in-memory counters.
"""

from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from app.core import rate_limit as rl
from app.core.database import engine as app_engine
from app.repositories.rate_limits import RateLimitRepository

__all__ = ["DatabaseRateLimitStore", "bucket_id"]

logger = logging.getLogger("app.rate_limit")


def bucket_id(key: str) -> str:
    """The stored bucket: SHA-256 of the limiter key (keys hold e-mail addresses and IPs)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class DatabaseRateLimitStore:
    PURGE_EVERY_SECONDS = 300.0

    def __init__(self, engine: Optional[Any] = None, clock: Callable[[], float] = time.time) -> None:
        self._engine = engine if engine is not None else app_engine
        self._clock = clock
        self._next_purge = 0.0

    @contextmanager
    def _repo(self) -> Iterator[RateLimitRepository]:
        with self._engine.connect() as conn:
            conn = conn.execution_options(isolation_level="READ COMMITTED")
            with conn.begin():
                yield RateLimitRepository(conn)

    def hit(self, key: str, limit: int, window: int) -> int:
        now = self._clock()
        bucket, slot = bucket_id(key), rl.slot_start(now, window)
        with self._repo() as repo:
            repo.add_hit(bucket, slot, rl.slot_expiry(slot, window))
            slots = repo.live_slots(bucket, rl.live_after(now, window))
            if sum(hits for _, hits in slots) <= limit:
                wait = 0
            else:
                repo.remove_hit(bucket, slot)
                wait = rl.retry_after([(s, h - 1 if s == slot else h) for s, h in slots], limit, window, now)
        self._maybe_purge(now)
        return wait

    def blocked(self, key: str, limit: int, window: int) -> int:
        now = self._clock()
        with self._repo() as repo:
            slots = repo.live_slots(bucket_id(key), rl.live_after(now, window))
        return rl.retry_after(slots, limit, window, now)

    def reset(self, key: str) -> None:
        with self._repo() as repo:
            repo.delete_bucket(bucket_id(key))

    def clear(self) -> None:
        with self._repo() as repo:
            repo.delete_all()

    def purge(self, now: Optional[float] = None) -> int:
        """Delete rows whose slot no longer counts. Returns how many went."""
        with self._repo() as repo:
            return repo.purge_expired(self._clock() if now is None else now)

    def _maybe_purge(self, now: float) -> None:
        if now < self._next_purge:
            return
        self._next_purge = now + self.PURGE_EVERY_SECONDS
        try:
            purged = self.purge(now)
        except Exception as exc:  # noqa: BLE001 - housekeeping only; the next interval tries again
            logger.warning("Rate-limit purge failed (%s)", type(exc).__name__)
            return
        if purged:
            logger.info("Rate-limit purge: %s expired counter row(s) deleted", purged)
