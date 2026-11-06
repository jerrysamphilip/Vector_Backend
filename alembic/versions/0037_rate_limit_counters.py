"""Shared rate-limit counters (DEC-09): sign-in lockout and auth limits counted once across API replicas

Revision ID: 0037_rate_limit_counters
Revises: 0036_drop_metrics_realtime
Create Date: 2026-11-06

rate_limit_counters: one row per (bucket, slot) for app/core/rate_limit.py through
app/services/rate_limit_store.py. ``bucket`` is the SHA-256 hex of the limiter key (no e-mail address or IP
is stored), ``slot_start`` the slot's epoch second, ``hits`` the count in that slot, ``expires_at`` the epoch
second after which the row no longer counts (expired rows are deleted by the store). Platform-wide, no
foreign keys: the rows are short-lived throttling state, never business data.

Idempotent (CREATE TABLE IF NOT EXISTS). Downgrade drops the table; the older code keeps its counters in
process memory and never reads it.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0037_rate_limit_counters"
down_revision: Union[str, Sequence[str], None] = "0036_drop_metrics_realtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS rate_limit_counters ("
        " bucket CHAR(64) CHARACTER SET ascii NOT NULL,"
        " slot_start BIGINT NOT NULL,"
        " hits INT NOT NULL DEFAULT 0,"
        " expires_at BIGINT NOT NULL,"
        " PRIMARY KEY (bucket, slot_start),"
        " INDEX ix_rate_limit_counters_expires_at (expires_at)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rate_limit_counters")
