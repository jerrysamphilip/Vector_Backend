"""Contact identity: past e-mails, stored duplicate pairs, merge logs, export log; unique list names (C-07..C-11, C-19, C-21)

Revision ID: 0025_contact_identity
Revises: 0024_company_properties
Create Date: 2026-10-27

New tables (app/models/contact_identity.py, app/models/export_log.py):

* ``contact_identifiers(identifier_id PK, tenant_id, email, status ACTIVE/RETIRED/MERGED, original_prospect_id,
  current_prospect_id FK prospects ON DELETE CASCADE, source, created_at, updated_at)``, UNIQUE (tenant_id, email):
  every address a contact used to have, pointing at the contact that answers to it today (C-11).
* ``duplicate_pairs(pair_id PK, tenant_id, contact_a_id, contact_b_id, score, confidence, reasons JSON, status
  OPEN/DISMISSED/MERGED, created_at, last_seen_at, resolved_at, resolved_by)``, UNIQUE (tenant_id, a, b): stored
  duplicate suggestions; a dismissed pair is never suggested again (C-08, C-09). No foreign keys.
* ``merge_logs(merge_id PK, tenant_id, survivor_id FK prospects ON DELETE CASCADE, merged_ids, snapshot, choices,
  moved, merged_by, merged_at)``: what each merge changed, with the records as they were (C-10, C-19).
* ``export_logs(export_id PK, tenant_id, user_id, object_type, filter_summary, row_count, format, created_at)`` (C-07).

And ``uq_prospect_lists_tenant_name (tenant_id, list_name)`` on prospect_lists (C-21; the column collation
utf8mb4_0900_ai_ci makes it case- and accent-insensitive). When a workspace already has two lists with the
same name the index is NOT created: a warning names the count and the app still refuses new duplicates
(409); rename the lists, then create the index with the CREATE UNIQUE INDEX statement below.

Every step checks information_schema first (a fresh build's create_all may already have made the tables).
No data is changed. Downgrade drops the index (restoring the plain ``tenant_id`` index the tenants foreign key
needs) and the four tables (their rows are lost; contacts are not).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0025_contact_identity"
down_revision: Union[str, Sequence[str], None] = "0024_company_properties"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

CHARSET = "utf8mb4"  # Literal copies of app/core/db_charset.py: a migration must not change later
COLLATION = "utf8mb4_0900_ai_ci"
OPTIONS = f"ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"
LIST_INDEX = "uq_prospect_lists_tenant_name"

TABLES = {
    "contact_identifiers": (
        "CREATE TABLE contact_identifiers ("
        " identifier_id VARCHAR(36) NOT NULL,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " email VARCHAR(255) NOT NULL,"
        " status VARCHAR(10) NOT NULL,"
        " original_prospect_id VARCHAR(36) NULL,"
        " current_prospect_id VARCHAR(36) NOT NULL,"
        " source VARCHAR(20) NULL,"
        " created_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " updated_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " PRIMARY KEY (identifier_id),"
        " UNIQUE KEY uq_contact_identifiers_tenant_email (tenant_id, email),"
        " KEY ix_contact_identifiers_current (current_prospect_id),"
        " CONSTRAINT fk_contact_identifiers_current FOREIGN KEY (current_prospect_id)"
        " REFERENCES prospects (prospect_id) ON DELETE CASCADE"
        f") {OPTIONS}"
    ),
    "duplicate_pairs": (
        "CREATE TABLE duplicate_pairs ("
        " pair_id VARCHAR(36) NOT NULL,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " contact_a_id VARCHAR(36) NOT NULL,"
        " contact_b_id VARCHAR(36) NOT NULL,"
        " score SMALLINT NOT NULL,"
        " confidence VARCHAR(10) NOT NULL,"
        " reasons JSON NULL,"
        " status VARCHAR(10) NOT NULL DEFAULT 'OPEN',"
        " created_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " last_seen_at DATETIME(6) NULL,"
        " resolved_at DATETIME(6) NULL,"
        " resolved_by VARCHAR(36) NULL,"
        " PRIMARY KEY (pair_id),"
        " UNIQUE KEY uq_duplicate_pairs_tenant_pair (tenant_id, contact_a_id, contact_b_id),"
        " KEY ix_duplicate_pairs_tenant_status (tenant_id, status, score),"
        " KEY ix_duplicate_pairs_contact_b (contact_b_id)"
        f") {OPTIONS}"
    ),
    "merge_logs": (
        "CREATE TABLE merge_logs ("
        " merge_id VARCHAR(36) NOT NULL,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " survivor_id VARCHAR(36) NOT NULL,"
        " merged_ids JSON NOT NULL,"
        " snapshot JSON NOT NULL,"
        " choices JSON NULL,"
        " moved JSON NULL,"
        " merged_by VARCHAR(36) NULL,"
        " merged_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " PRIMARY KEY (merge_id),"
        " KEY ix_merge_logs_survivor (survivor_id, merged_at),"
        " KEY ix_merge_logs_tenant (tenant_id, merged_at),"
        " CONSTRAINT fk_merge_logs_survivor FOREIGN KEY (survivor_id)"
        " REFERENCES prospects (prospect_id) ON DELETE CASCADE"
        f") {OPTIONS}"
    ),
    "export_logs": (
        "CREATE TABLE export_logs ("
        " export_id VARCHAR(36) NOT NULL,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " user_id VARCHAR(36) NULL,"
        " object_type VARCHAR(20) NOT NULL,"
        " filter_summary TEXT NULL,"
        " row_count INT NOT NULL,"
        " format VARCHAR(10) NOT NULL,"
        " created_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6),"
        " PRIMARY KEY (export_id),"
        " KEY ix_export_logs_tenant_created (tenant_id, created_at)"
        f") {OPTIONS}"
    ),
}


def _scalar(sql: str, **params: str) -> int:
    return int(op.get_bind().execute(text(sql), params).scalar() or 0)


def _has_table(table: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :t",
        t=table))


def _has_index(table: str, index: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i", t=table, i=index))


def _duplicate_list_names() -> int:
    return _scalar(
        "SELECT COUNT(*) FROM (SELECT tenant_id, list_name FROM prospect_lists "
        "GROUP BY tenant_id, list_name HAVING COUNT(*) > 1) d")


def upgrade() -> None:
    for table, ddl in TABLES.items():
        if not _has_table(table):
            op.execute(ddl)
    if _has_index("prospect_lists", LIST_INDEX):
        return
    clashes = _duplicate_list_names()
    if clashes:
        logger.warning(
            "0025: %d list name(s) are used by more than one list in the same workspace; %s was not created. "
            "Rename those lists, then run: CREATE UNIQUE INDEX %s ON prospect_lists (tenant_id, list_name)",
            clashes, LIST_INDEX, LIST_INDEX)
        return
    op.execute(f"CREATE UNIQUE INDEX {LIST_INDEX} ON prospect_lists (tenant_id, list_name)")


def _tenant_index_besides(index: str) -> bool:
    """Another index of prospect_lists starts with tenant_id (the tenants foreign key needs one)."""
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = 'prospect_lists' AND column_name = 'tenant_id' AND seq_in_index = 1 "
        "AND index_name <> :i", i=index))


def downgrade() -> None:
    if _has_index("prospect_lists", LIST_INDEX):
        # MySQL dropped the foreign key's own index on tenant_id when the unique index covered it:
        # give it back (named as MySQL names it) in the same statement
        restore = "" if _tenant_index_besides(LIST_INDEX) else "ADD INDEX tenant_id (tenant_id), "
        op.execute(f"ALTER TABLE prospect_lists {restore}DROP INDEX {LIST_INDEX}")
    for table in reversed(list(TABLES)):
        if _has_table(table):
            op.execute(f"DROP TABLE {table}")
