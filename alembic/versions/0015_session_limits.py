"""Session limits: absolute cap, idle timeout and refresh-token reuse detection

Revision ID: 0015_session_limits
Revises: 0014_crm_kpis
Create Date: 2026-10-14

refresh_tokens gains (ADR 0007, app/services/session_limits.py):
  session_id          - rotation family: every token issued by one sign-in shares it
  session_started_at  - sign-in time, carried through rotation (SESSION_ABSOLUTE_HOURS)
  last_activity_at    - last user-driven activity, carried through rotation (SESSION_IDLE_MINUTES)
  revoked_reason      - rotated / logout / reuse / idle / absolute (NULL: older or bulk revocations)

Existing rows are backfilled: each token is its own family, started at its created_at, last active
now (sessions alive at deploy time get a fresh idle window instead of being signed out at once).

Idempotent: columns and the index are only added when missing; downgrade drops them when present.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0015_session_limits"
down_revision: Union[str, Sequence[str], None] = "0014_crm_kpis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("session_id", "VARCHAR(36) NULL"),
    ("session_started_at", "DATETIME NULL"),
    ("last_activity_at", "DATETIME NULL"),
    ("revoked_reason", "VARCHAR(16) NULL"),
)
INDEX = "ix_refresh_tokens_session_id"


def _has_column(table: str, column: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = :c"), {"t": table, "c": column}).scalar())


def _has_index(table: str, index: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = :t AND index_name = :i"), {"t": table, "i": index}).scalar())


def upgrade() -> None:
    for name, ddl in COLUMNS:
        if not _has_column("refresh_tokens", name):
            op.execute(f"ALTER TABLE refresh_tokens ADD COLUMN {name} {ddl}")
    if not _has_index("refresh_tokens", INDEX):
        op.execute(f"CREATE INDEX {INDEX} ON refresh_tokens (session_id)")
    op.execute(
        "UPDATE refresh_tokens SET session_id = token_id,"
        " session_started_at = COALESCE(session_started_at, created_at, UTC_TIMESTAMP()),"
        " last_activity_at = COALESCE(last_activity_at, UTC_TIMESTAMP())"
        " WHERE session_id IS NULL")


def downgrade() -> None:
    if _has_index("refresh_tokens", INDEX):
        op.execute(f"DROP INDEX {INDEX} ON refresh_tokens")
    for name, _ in reversed(COLUMNS):
        if _has_column("refresh_tokens", name):
            op.execute(f"ALTER TABLE refresh_tokens DROP COLUMN {name}")
