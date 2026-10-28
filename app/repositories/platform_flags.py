"""
Platform flags and counters repository (ADR 0009): the ``platform_flags`` name/value table every
process shares (scheduler pause switch, the LLM spend ledger) and the per-user activity days.

Platform-wide by design (no tenant). The counter updates are single conditional UPDATEs, so
concurrent API and worker processes reserve atomically. Never commits; callers own the transaction
(app/services/platform_flags.py and llm_budget.py open their own short sessions).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["PlatformFlagRepository", "Session", "record_activity_day"]


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class PlatformFlagRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, name: str) -> Optional[str]:
        value: Optional[str] = self._session.execute(
            text("SELECT value FROM platform_flags WHERE name = :n"), {"n": name}).scalar()
        return value

    def put(self, name: str, value: str) -> None:
        self._session.execute(text("INSERT INTO platform_flags (name, value) VALUES (:n, :v) "
                                   "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = CURRENT_TIMESTAMP"),
                              {"n": name, "v": value})

    def remove(self, name: str) -> None:
        self._session.execute(text("DELETE FROM platform_flags WHERE name = :n"), {"n": name})

    # counters (value holds an unsigned integer) --------------------------------------
    def ensure_counter(self, name: str) -> None:
        self._session.execute(text("INSERT IGNORE INTO platform_flags (name, value) VALUES (:n, '0')"), {"n": name})

    def add_within(self, name: str, amount: int, limit: int) -> bool:
        """Add ``amount`` when the result stays <= ``limit``; False (nothing changed) otherwise."""
        return _rowcount(self._session.execute(
            text("UPDATE platform_flags SET value = CAST(value AS UNSIGNED) + :e "
                 "WHERE name = :n AND CAST(value AS UNSIGNED) + :e <= :l"),
            {"n": name, "e": amount, "l": limit})) > 0

    def increment_below(self, name: str, limit: int) -> bool:
        """Add one when the value is < ``limit``; False (nothing changed) otherwise."""
        return _rowcount(self._session.execute(
            text("UPDATE platform_flags SET value = CAST(value AS UNSIGNED) + 1 "
                 "WHERE name = :n AND CAST(value AS UNSIGNED) < :l"),
            {"n": name, "l": limit})) > 0

    def adjust(self, name: str, delta: int) -> None:
        """Add ``delta`` (may be negative), never below zero."""
        self._session.execute(
            text("UPDATE platform_flags SET value = GREATEST(CAST(value AS SIGNED) + :d, 0) WHERE name = :n"),
            {"d": delta, "n": name})


def record_activity_day(bind: Any, tenant_id: str, user_id: str, day: date) -> None:
    """INSERT IGNORE one user_activity_days row on its own short connection (``bind``: an Engine or a
    Session's bind), so it never touches the request's transaction."""
    engine = getattr(bind, "engine", bind)
    with engine.begin() as conn:
        conn.execute(text("INSERT IGNORE INTO user_activity_days (tenant_id, user_id, day) VALUES (:t, :u, :d)"),
                     {"t": tenant_id, "u": user_id, "d": day})
