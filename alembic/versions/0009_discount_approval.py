"""Quote discount approval (BRD v2.0 BR-SF-17, RP-103)

Revision ID: 0009_discount_approval
Revises: 0008_forecast_snapshots
Create Date: 2026-10-13

proposals.approval_*: approval state of a quote whose discount is above the workspace threshold
(NOT_REQUIRED / PENDING / APPROVED / REJECTED, who asked, who decided, when, why). The threshold
itself lives in the per-workspace sales settings JSON (discount_approval_threshold), so it needs
no column. Each column / index is added only when missing (0001_baseline's create_all already
creates them on an empty database).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0009_discount_approval"
down_revision: Union[str, Sequence[str], None] = "0008_forecast_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# (table, column, definition)
COLUMNS = (
    ("proposals", "approval_status", "VARCHAR(20) NOT NULL DEFAULT 'NOT_REQUIRED'"),
    ("proposals", "approval_discount_pct", "NUMERIC(5, 2) NULL"),
    ("proposals", "approval_requested_by", "VARCHAR(36) NULL"),
    ("proposals", "approval_requested_at", "TIMESTAMP NULL"),
    ("proposals", "approval_decided_by", "VARCHAR(36) NULL"),
    ("proposals", "approval_decided_at", "TIMESTAMP NULL"),
    ("proposals", "approval_reason", "VARCHAR(1000) NULL"),
)
INDEX = ("proposals", "ix_proposals_tenant_approval", "(tenant_id, approval_status)")


def _columns(conn, table):
    return {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table})}


def _has_index(conn, table, name):
    return bool(conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND INDEX_NAME = :i"), {"t": table, "i": name}).scalar())


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, definition in COLUMNS:
        if column in _columns(conn, table):
            continue
        conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"))
        logger.info("Added %s.%s", table, column)
    table, name, cols = INDEX
    if not _has_index(conn, table, name):
        conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` {cols}"))


def downgrade() -> None:
    conn = op.get_bind()
    table, name, _ = INDEX
    if _has_index(conn, table, name):
        conn.execute(text(f"DROP INDEX `{name}` ON `{table}`"))
    for table, column, _ in reversed(COLUMNS):
        if column in _columns(conn, table):
            conn.execute(text(f"ALTER TABLE `{table}` DROP COLUMN `{column}`"))
