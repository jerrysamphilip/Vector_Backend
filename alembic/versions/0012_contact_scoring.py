"""Contact scoring on fit and engagement (BR-CM-43, CM-083)

Revision ID: 0012_contact_scoring
Revises: 0011_webhooks
Create Date: 2026-10-13

prospects gets score / fit_score / engagement_score (SMALLINT 0-100), score_grade (A-D) and
scored_at, with indexes for sorting and filtering the contact list by score and grade and for
the worker's "stale scores" sweep. contact_scoring_models holds each workspace's scoring model
(JSON) and the state of its last full recompute.

Idempotent: each column / index is added only when missing (0001_baseline's create_all already
creates them on an empty database), CREATE TABLE IF NOT EXISTS.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0012_contact_scoring"
down_revision: Union[str, Sequence[str], None] = "0011_webhooks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

COLUMNS = (
    ("score", "SMALLINT NULL"),
    ("fit_score", "SMALLINT NULL"),
    ("engagement_score", "SMALLINT NULL"),
    ("score_grade", "VARCHAR(1) NULL"),
    ("scored_at", "TIMESTAMP NULL"),
)
INDEXES = (
    ("ix_prospects_tenant_live_score", "tenant_id, deleted_at, score"),
    ("ix_prospects_tenant_live_grade", "tenant_id, deleted_at, score_grade"),
    ("ix_prospects_scored_at", "scored_at"),
)


def _columns(conn):
    return {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'prospects'"))}


def _indexes(conn):
    return {r[0] for r in conn.execute(text(
        "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'prospects'"))}


def upgrade() -> None:
    conn = op.get_bind()
    existing = _columns(conn)
    for column, definition in COLUMNS:
        if column not in existing:
            conn.execute(text(f"ALTER TABLE prospects ADD COLUMN `{column}` {definition}"))
            logger.info("Added prospects.%s", column)
    existing = _indexes(conn)
    for name, cols in INDEXES:
        if name not in existing:
            conn.execute(text(f"CREATE INDEX `{name}` ON prospects ({cols})"))
            logger.info("Added index %s", name)
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS contact_scoring_models ("
        " tenant_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " model JSON NOT NULL,"
        " recompute_requested_at TIMESTAMP NULL,"
        " last_recomputed_at TIMESTAMP NULL,"
        " last_recompute_count INTEGER NULL,"
        " updated_by VARCHAR(36) NULL,"
        " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (updated_by) REFERENCES users (user_id) ON DELETE SET NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS contact_scoring_models"))
    existing = _indexes(conn)
    for name, _ in INDEXES:
        if name in existing:
            conn.execute(text(f"DROP INDEX `{name}` ON prospects"))
    existing = _columns(conn)
    for column, _ in reversed(COLUMNS):
        if column in existing:
            conn.execute(text(f"ALTER TABLE prospects DROP COLUMN `{column}`"))
