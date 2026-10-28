"""Background CRM imports: stored file, checkpoint, heartbeat and cancel on import_jobs; saved mappings (C-03, C-04)

Revision ID: 0026_import_jobs
Revises: 0025_contact_identity
Create Date: 2026-10-27

import_jobs gets the columns the worker's "imports" loop needs (app/services/import_service.py):

* ``mode`` VARCHAR(20): CREATE / UPDATE_ONLY / CREATE_AND_UPDATE
* ``processed_rows`` INT NOT NULL DEFAULT 0: the checkpoint; it commits with each chunk of rows, so a
  resumed job starts at the first row not yet imported
* ``skipped_count`` INT DEFAULT 0: rows skipped by the mode
* ``file_path`` VARCHAR(500): the stored upload (tenant-scoped key under IMPORT_STORAGE_DIR); NULL for
  synchronous imports
* ``file_size`` INT, ``sheet_name`` VARCHAR(255)
* ``queued_at`` / ``heartbeat_at`` DATETIME(6): queue order and liveness (a RUNNING job whose heartbeat
  is older than two minutes is resumed by the next worker pass)
* ``cancel_requested`` TINYINT(1) NOT NULL DEFAULT 0, ``attempts`` INT NOT NULL DEFAULT 0,
  ``claimed_by`` VARCHAR(100)
* index ``ix_import_jobs_status_heartbeat`` (status, heartbeat_at) for the worker's claim

and a new table ``import_mappings`` (named column mappings per workspace, unique name per workspace).

Existing jobs keep their rows; their new columns are NULL / 0 (processed_rows stays 0: they are
synchronous imports, never resumed). Every step checks information_schema first, so the revision is a
no-op where the schema already has it (a fresh build's create_all) and safe to re-run. No data is
changed. Downgrade drops the table, the index and the columns (saved mappings and the progress
columns are lost; the import history rows are not).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0026_import_jobs"
down_revision: Union[str, Sequence[str], None] = "0025_contact_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "import_jobs"
MAPPINGS = "import_mappings"
INDEX = "ix_import_jobs_status_heartbeat"
CHARSET = "utf8mb4"  # Literal copies of app/core/db_charset.py: a migration must not change later
COLLATION = "utf8mb4_0900_ai_ci"
COLUMNS = (
    ("mode", "VARCHAR(20) NULL"),
    ("processed_rows", "INT NOT NULL DEFAULT 0"),
    ("skipped_count", "INT NULL DEFAULT 0"),
    ("file_path", "VARCHAR(500) NULL"),
    ("file_size", "INT NULL"),
    ("sheet_name", "VARCHAR(255) NULL"),
    ("queued_at", "DATETIME(6) NULL"),
    ("heartbeat_at", "DATETIME(6) NULL"),
    ("cancel_requested", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("attempts", "INT NOT NULL DEFAULT 0"),
    ("claimed_by", "VARCHAR(100) NULL"),
)


def _scalar(sql: str, **params: str) -> int:
    return int(op.get_bind().execute(text(sql), params).scalar() or 0)


def _has_table(table: str) -> bool:
    return bool(_scalar("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name = :t", t=table))


def _has_column(column: str) -> bool:
    return bool(_scalar("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c", t=TABLE, c=column))


def _has_index(index: str) -> bool:
    return bool(_scalar("SELECT COUNT(*) FROM information_schema.statistics "
                        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i", t=TABLE, i=index))


def upgrade() -> None:
    missing = [f"ADD COLUMN {name} {ddl}" for name, ddl in COLUMNS if not _has_column(name)]
    if missing:
        op.execute(f"ALTER TABLE `{TABLE}` " + ", ".join(missing))
    if not _has_index(INDEX):
        op.execute(f"CREATE INDEX {INDEX} ON `{TABLE}` (status, heartbeat_at)")
    if not _has_table(MAPPINGS):
        op.execute(
            f"CREATE TABLE {MAPPINGS} ("
            " mapping_id VARCHAR(36) NOT NULL,"
            " tenant_id VARCHAR(36) NOT NULL,"
            " name VARCHAR(100) NOT NULL,"
            " mapping JSON NOT NULL,"
            " options JSON NULL,"
            " created_by VARCHAR(36) NULL,"
            " created_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
            " updated_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),"
            " PRIMARY KEY (mapping_id),"
            " UNIQUE KEY uq_import_mappings_tenant_name (tenant_id, name),"
            " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
            " FOREIGN KEY (created_by) REFERENCES users (user_id) ON DELETE SET NULL"
            f") ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"
        )


def downgrade() -> None:
    if _has_table(MAPPINGS):
        op.execute(f"DROP TABLE {MAPPINGS}")
    if _has_index(INDEX):
        op.execute(f"DROP INDEX {INDEX} ON `{TABLE}`")
    present = [f"DROP COLUMN {name}" for name, _ in COLUMNS if _has_column(name)]
    if present:
        op.execute(f"ALTER TABLE `{TABLE}` " + ", ".join(present))
