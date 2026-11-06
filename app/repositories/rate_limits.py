"""
Shared rate-limit counters repository (DEC-09, ADR 0002): the ``rate_limit_counters`` table (alembic
0037_rate_limit_counters) behind app/core/rate_limit.py. One row per (bucket, slot): ``bucket`` is the
SHA-256 of the limiter key (no e-mail address or IP is stored), ``slot_start`` the slot's epoch second,
``hits`` the count, ``expires_at`` the epoch second after which the row counts for nothing.

Platform-wide by design (no tenant). Works on a Connection the caller owns (app/services/rate_limit_store.py
opens a short READ COMMITTED transaction per operation, apart from the request's session). Never commits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

__all__ = ["RateLimitRepository"]


class RateLimitRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def add_hit(self, bucket: str, slot_start: int, expires_at: int) -> None:
        """One more hit in the slot (row-locked until the transaction ends, so hits on a slot serialise)."""
        self._conn.execute(text(
            "INSERT INTO rate_limit_counters (bucket, slot_start, hits, expires_at) VALUES (:b, :s, 1, :e) "
            "ON DUPLICATE KEY UPDATE hits = hits + 1"), {"b": bucket, "s": slot_start, "e": expires_at})

    def remove_hit(self, bucket: str, slot_start: int) -> None:
        self._conn.execute(text(
            "UPDATE rate_limit_counters SET hits = GREATEST(hits - 1, 0) WHERE bucket = :b AND slot_start = :s"),
            {"b": bucket, "s": slot_start})

    def live_slots(self, bucket: str, after: float) -> list[tuple[int, int]]:
        """(slot_start, hits) of the bucket's slots starting after ``after``."""
        rows = self._conn.execute(text(
            "SELECT slot_start, hits FROM rate_limit_counters WHERE bucket = :b AND slot_start > :a"),
            {"b": bucket, "a": after}).all()
        return [(int(start), int(hits)) for start, hits in rows]

    def delete_bucket(self, bucket: str) -> None:
        self._conn.execute(text("DELETE FROM rate_limit_counters WHERE bucket = :b"), {"b": bucket})

    def purge_expired(self, now: float, batch: int = 5000) -> int:
        result = self._conn.execute(text(
            "DELETE FROM rate_limit_counters WHERE expires_at <= :n ORDER BY expires_at LIMIT :l"),
            {"n": int(now), "l": int(batch)})
        return int(getattr(result, "rowcount", 0) or 0)

    def delete_all(self) -> None:
        self._conn.execute(text("DELETE FROM rate_limit_counters"))
