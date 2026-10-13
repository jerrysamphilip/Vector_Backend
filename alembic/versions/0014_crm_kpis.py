"""CRM KPIs (BRD v2.0 section 8): committed forecast on snapshots, daily user activity

Revision ID: 0014_crm_kpis
Revises: 0013_revoked_access_tokens
Create Date: 2026-10-13

forecast_snapshots gains the committed forecast taken at the start of the period (BR-SF-08):
  commit_open  - open deals in the Commit forecast category closing in the period
  committed    - closed won to date + commit_open; forecast accuracy = actual won / committed x 100
Both are NULL on snapshots taken before this revision (no committed figure was captured then).

user_activity_days: one row per user per UTC day on which they signed in or made an authenticated
API request (written at most once per user per day per API process), for CRM adoption = weekly
active users / licensed users.

Idempotent: columns and table are only added when missing; downgrade drops them when present.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0014_crm_kpis"
down_revision: Union[str, Sequence[str], None] = "0013_revoked_access_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SNAPSHOT_COLUMNS = (
    ("commit_open", "NUMERIC(15, 2) NULL"),
    ("committed", "NUMERIC(15, 2) NULL"),
)


def _has_column(table: str, column: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = :c"), {"t": table, "c": column}).scalar())


def upgrade() -> None:
    for name, ddl in SNAPSHOT_COLUMNS:
        if not _has_column("forecast_snapshots", name):
            op.execute(f"ALTER TABLE forecast_snapshots ADD COLUMN {name} {ddl}")
    op.execute(
        "CREATE TABLE IF NOT EXISTS user_activity_days ("
        " tenant_id VARCHAR(36) NOT NULL,"
        " user_id VARCHAR(36) NOT NULL,"
        " day DATE NOT NULL,"
        " PRIMARY KEY (user_id, day),"
        " INDEX ix_user_activity_days_tenant_day (tenant_id, day),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id) ON DELETE CASCADE,"
        " FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_activity_days")
    for name, _ in SNAPSHOT_COLUMNS:
        if _has_column("forecast_snapshots", name):
            op.execute(f"ALTER TABLE forecast_snapshots DROP COLUMN {name}")
