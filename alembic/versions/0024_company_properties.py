"""Company custom properties and company backfill run history (C-16, C-14)

Revision ID: 0024_company_properties
Revises: 0023_reply_classification
Create Date: 2026-10-27

* ``contact_field_definitions.object_type`` VARCHAR(20) NOT NULL DEFAULT 'CONTACT': a custom property belongs
  to contacts (CONTACT) or companies (COMPANY). Every existing definition is a contact property, which the
  column default records; no row is rewritten.
* ``accounts.custom_fields`` JSON NULL: company custom property values keyed by field_key (as
  prospects.custom_fields is for contacts).
* ``account_backfill_runs``: one row per "link contacts to companies" run (who, when, status, counts, undo).
* ``account_backfill_items``: what a run did, so it can be undone: ACCOUNT_CREATED, ACCOUNT_DOMAIN_SET,
  CONTACT_LINKED and CONTACT_FIELD (old / new value of a contact field the run filled).

No foreign keys on the two ledger tables on purpose (same reason as automation_rule_runs in 0021): they must
never block deleting or purging a company or a contact; the undo checks that each row still matches.

Idempotent: every step checks information_schema first, so the revision is a no-op on a fresh build (create_all
in 0001 already makes the columns from the models; the run tables are created here when missing) and safe to
re-run. No data is changed or deleted on upgrade.

Downgrade drops the two run tables and the two columns when present. That would lose the run history, company
property values and COMPANY definitions, so it refuses (with a warning, and changes nothing) while any COMPANY
definition, stored company value or backfill run exists: remove those first if the downgrade is really intended.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0024_company_properties"
down_revision: Union[str, Sequence[str], None] = "0023_reply_classification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

CHARSET = "utf8mb4"  # Literal copies of app/core/db_charset.py: a migration must not change later
COLLATION = "utf8mb4_0900_ai_ci"

FIELDS = "contact_field_definitions"
ACCOUNTS = "accounts"
RUNS = "account_backfill_runs"
ITEMS = "account_backfill_items"


def _scalar(sql: str, **params: str) -> int:
    return int(op.get_bind().execute(text(sql), params).scalar() or 0)


def _has_table(table: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :t",
        t=table))


def _has_column(table: str, column: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c", t=table, c=column))


def upgrade() -> None:
    if _has_table(FIELDS) and not _has_column(FIELDS, "object_type"):
        op.execute(f"ALTER TABLE {FIELDS} ADD COLUMN object_type VARCHAR(20) NOT NULL DEFAULT 'CONTACT'")
    if _has_table(ACCOUNTS) and not _has_column(ACCOUNTS, "custom_fields"):
        op.execute(f"ALTER TABLE {ACCOUNTS} ADD COLUMN custom_fields JSON NULL")
    if not _has_table(RUNS):
        op.execute(
            f"CREATE TABLE {RUNS} ("
            " run_id VARCHAR(36) NOT NULL,"
            " tenant_id VARCHAR(36) NOT NULL,"
            " started_by VARCHAR(36) NULL,"
            " mode VARCHAR(20) NOT NULL DEFAULT 'RUN',"
            " status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',"
            " stats JSON NULL,"
            " error VARCHAR(500) NULL,"
            " started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
            " finished_at DATETIME(6) NULL,"
            " undone_at DATETIME(6) NULL,"
            " undone_by VARCHAR(36) NULL,"
            " PRIMARY KEY (run_id),"
            " KEY ix_account_backfill_runs_tenant_started (tenant_id, started_at)"
            f") ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"
        )
    if not _has_table(ITEMS):
        op.execute(
            f"CREATE TABLE {ITEMS} ("
            " item_id VARCHAR(36) NOT NULL,"
            " run_id VARCHAR(36) NOT NULL,"
            " tenant_id VARCHAR(36) NOT NULL,"
            " kind VARCHAR(20) NOT NULL,"
            " account_id VARCHAR(36) NULL,"
            " prospect_id VARCHAR(36) NULL,"
            " field VARCHAR(64) NULL,"
            " old_value TEXT NULL,"
            " new_value TEXT NULL,"
            " created_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
            " PRIMARY KEY (item_id),"
            " KEY ix_account_backfill_items_run_kind (run_id, kind)"
            f") ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"
        )


def _company_data_exists() -> bool:
    definitions = _has_column(FIELDS, "object_type") and _scalar(
        f"SELECT COUNT(*) FROM {FIELDS} WHERE object_type <> 'CONTACT'")
    values = _has_column(ACCOUNTS, "custom_fields") and _scalar(
        f"SELECT COUNT(*) FROM {ACCOUNTS} WHERE custom_fields IS NOT NULL AND JSON_LENGTH(custom_fields) > 0")
    runs = _has_table(RUNS) and _scalar(f"SELECT COUNT(*) FROM {RUNS}")
    return bool(definitions or values or runs)


def downgrade() -> None:
    if _company_data_exists():
        logger.warning("0024_company_properties downgrade skipped: company custom properties, values or backfill "
                       "runs exist. Remove them first; nothing was changed.")
        return
    for table in (ITEMS, RUNS):
        if _has_table(table):
            op.execute(f"DROP TABLE {table}")
    if _has_column(ACCOUNTS, "custom_fields"):
        op.execute(f"ALTER TABLE {ACCOUNTS} DROP COLUMN custom_fields")
    if _has_column(FIELDS, "object_type"):
        op.execute(f"ALTER TABLE {FIELDS} DROP COLUMN object_type")
