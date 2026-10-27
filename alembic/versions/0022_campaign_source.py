"""campaigns.source_campaign_id: which campaign a duplicate was copied from (S-12)

Revision ID: 0022_campaign_source
Revises: 0021_automation_rule_runs
Create Date: 2026-10-27

POST /campaigns/{id}/duplicates (app/services/campaign_duplicate_service.py) creates a DRAFT copy
of a campaign and records the original here, so the copy can be traced back to its source.

  campaigns.source_campaign_id VARCHAR(36) NULL
  INDEX ix_campaigns_source_campaign_id (source_campaign_id)
  FOREIGN KEY fk_campaigns_source_campaign -> campaigns(campaign_id) ON DELETE SET NULL

ON DELETE SET NULL: deleting the original keeps its copies (they only lose the link).

Idempotent: each piece is added only when missing (information_schema), so on a fresh build, where
the baseline's create_all already made the column, its index and a foreign key from
app/models/campaign.py, the revision changes nothing. Existing data is never changed: if the column
already exists and holds ids of campaigns that no longer exist, the foreign key is NOT added and a
warning gives the count; clear those values (UPDATE campaigns SET source_campaign_id = NULL WHERE
source_campaign_id NOT IN (SELECT campaign_id FROM campaigns)) and re-run downgrade/upgrade.

Downgrade drops the foreign key, the index and the column (the duplicate -> source links are lost;
the campaigns themselves are untouched).
"""
import logging
from typing import Optional, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0022_campaign_source"
down_revision: Union[str, Sequence[str], None] = "0021_automation_rule_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "campaigns"
COLUMN = "source_campaign_id"
INDEX = "ix_campaigns_source_campaign_id"
FK = "fk_campaigns_source_campaign"


def _scalar(sql: str, **params: str) -> int:
    return int(op.get_bind().execute(text(sql), params).scalar() or 0)


def _has_column() -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c", t=TABLE, c=COLUMN))


def _indexes_on_column() -> list[str]:
    rows = op.get_bind().execute(text(
        "SELECT DISTINCT index_name FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c AND seq_in_index = 1"),
        {"t": TABLE, "c": COLUMN}).all()
    return [r[0] for r in rows]


def _foreign_key() -> Optional[str]:
    row = op.get_bind().execute(text(
        "SELECT constraint_name FROM information_schema.key_column_usage "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c "
        "AND referenced_table_name IS NOT NULL LIMIT 1"), {"t": TABLE, "c": COLUMN}).first()
    return row[0] if row else None


def _orphans() -> int:
    return _scalar(
        f"SELECT COUNT(*) FROM {TABLE} c WHERE c.{COLUMN} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {TABLE} s WHERE s.campaign_id = c.{COLUMN})")


def upgrade() -> None:
    if not _has_column():
        op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(36) NULL")
    if not _indexes_on_column():
        op.execute(f"CREATE INDEX {INDEX} ON {TABLE} ({COLUMN})")
    if _foreign_key():
        return
    orphans = _orphans()
    if orphans:
        logger.warning("0022_campaign_source: %s campaigns point at a source campaign that no longer exists; "
                       "foreign key %s NOT added (see the revision docstring)", orphans, FK)
        return
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {FK} FOREIGN KEY ({COLUMN}) "
               f"REFERENCES {TABLE} (campaign_id) ON DELETE SET NULL")


def downgrade() -> None:
    if not _has_column():
        return
    fk = _foreign_key()
    if fk:
        op.execute(f"ALTER TABLE {TABLE} DROP FOREIGN KEY {fk}")
    for index in _indexes_on_column():
        op.execute(f"DROP INDEX {index} ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
