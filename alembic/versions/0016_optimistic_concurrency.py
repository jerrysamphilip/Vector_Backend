"""Optimistic concurrency: row_version on the main editable records (rule T-3)

Revision ID: 0016_optimistic_concurrency
Revises: 0015_session_limits
Create Date: 2026-10-18

Each table gains ``row_version INT NOT NULL DEFAULT 1`` (existing rows start at 1). The API returns
it with the record and the client sends it back on update / state-change actions; a stale version
gets 409 STALE_VERSION instead of silently overwriting (app/core/concurrency.py).

  prospects, leads, opportunities, proposals, campaigns, email_sequences, accounts

MySQL 8 adds a column with a constant default as an INSTANT change (no table rebuild).
Idempotent: columns are only added when missing; downgrade drops them when present.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0016_optimistic_concurrency"
down_revision: Union[str, Sequence[str], None] = "0015_session_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("prospects", "leads", "opportunities", "proposals", "campaigns", "email_sequences", "accounts")
COLUMN = "row_version"


def _has_column(table: str, column: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = :c"), {"t": table, "c": column}).scalar())


def upgrade() -> None:
    for table in TABLES:
        if not _has_column(table, COLUMN):
            op.execute(f"ALTER TABLE {table} ADD COLUMN {COLUMN} INT NOT NULL DEFAULT 1")


def downgrade() -> None:
    for table in reversed(TABLES):
        if _has_column(table, COLUMN):
            op.execute(f"ALTER TABLE {table} DROP COLUMN {COLUMN}")
