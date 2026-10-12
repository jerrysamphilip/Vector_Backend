"""Forecast snapshots for forecast accuracy (BRD v2.0 section 8)

Revision ID: 0008_forecast_snapshots
Revises: 0007_note_mentions
Create Date: 2026-10-12

forecast_snapshots: per workspace, period (MONTH / fiscal QUARTER) and owner, the forecast
(closed won + probability-weighted open pipeline with a close date in the period) taken at
the start of the period by the sales_jobs worker loop, or on demand by an admin.
Idempotent (CREATE TABLE IF NOT EXISTS).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008_forecast_snapshots"
down_revision: Union[str, Sequence[str], None] = "0007_note_mentions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS forecast_snapshots ("
        " snapshot_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " period_type VARCHAR(10) NOT NULL,"
        " period_start DATE NOT NULL,"
        " period_end DATE NOT NULL,"
        " owner_id VARCHAR(36) NOT NULL,"
        " won NUMERIC(15, 2) NOT NULL DEFAULT 0,"
        " weighted NUMERIC(15, 2) NOT NULL DEFAULT 0,"
        " forecast NUMERIC(15, 2) NOT NULL DEFAULT 0,"
        " open_count INTEGER NOT NULL DEFAULT 0,"
        " source VARCHAR(10) NOT NULL DEFAULT 'SCHEDULED',"
        " taken_by VARCHAR(36) NULL,"
        " taken_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " UNIQUE INDEX uq_forecast_snapshots (tenant_id, period_type, period_start, owner_id),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (owner_id) REFERENCES users (user_id) ON DELETE CASCADE,"
        " FOREIGN KEY (taken_by) REFERENCES users (user_id)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS forecast_snapshots")
