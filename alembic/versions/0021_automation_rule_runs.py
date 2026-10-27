"""automation_rule_runs: an automation rule acts on a contact once, enforced by the database (19b)

Revision ID: 0021_automation_rule_runs
Revises: 0020_send_integrity_keys
Create Date: 2026-10-27

A rule fires on every matching event (each open, each click), and the send-email action used to
check "already queued?" and then insert, so two events processed at the same time could both queue
the rule's email. app/services/automation_rule_service.py now inserts one row per (rule, contact)
in a savepoint before queuing; the unique key makes the second insert fail and the action is skipped.

  automation_rule_runs(run_id PK, rule_id, prospect_id, tenant_id, created_at DATETIME(6))
  UNIQUE uq_automation_rule_runs_rule_prospect (rule_id, prospect_id), INDEX on tenant_id

No foreign keys on purpose: the rows are a small ledger and must never block deleting a rule or a
contact. Idempotent: the table is created only when missing (a fresh build's baseline create_all
already makes it from app/models/automation_rule_run.py); downgrade drops it when present.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0021_automation_rule_runs"
down_revision: Union[str, Sequence[str], None] = "0020_send_integrity_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "automation_rule_runs"
CHARSET = "utf8mb4"  # Literal copies of app/core/db_charset.py: a migration must not change later
COLLATION = "utf8mb4_0900_ai_ci"


def _has_table(table: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": table}).scalar())


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.execute(
        f"CREATE TABLE {TABLE} ("
        " run_id VARCHAR(36) NOT NULL,"
        " rule_id VARCHAR(36) NOT NULL,"
        " prospect_id VARCHAR(36) NOT NULL,"
        " tenant_id VARCHAR(36) NULL,"
        " created_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " PRIMARY KEY (run_id),"
        " UNIQUE KEY uq_automation_rule_runs_rule_prospect (rule_id, prospect_id),"
        " KEY ix_automation_rule_runs_tenant_id (tenant_id)"
        f") ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"
    )


def downgrade() -> None:
    if _has_table(TABLE):
        op.execute(f"DROP TABLE {TABLE}")
