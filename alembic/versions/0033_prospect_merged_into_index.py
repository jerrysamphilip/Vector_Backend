"""Tenant-first index on prospects.merged_into_id (D1: merge / erasure deadlocks)

Revision ID: 0033_prospect_merged_into_index
Revises: 0032_server_defaults
Create Date: 2026-10-28

Merging contacts (POST /contacts/merge) and erasing them (DELETE /prospects/{id}, POST /contacts/purge, the
90-day recycle-bin purge job) re-point or clear the ``merged_into_id`` of contacts that were merged into
the affected ones:

    UPDATE prospects SET merged_into_id = ... WHERE tenant_id = ... AND merged_into_id = / IN (...)

There was no index on ``merged_into_id`` (and the statements had no tenant filter), so InnoDB scanned the
primary key of the whole table and X-locked every contact of every workspace; it deadlocked (MySQL 1213)
with the worker's background-import inserts (live regression ev6, CM-063/068/069/080). This revision adds
``ix_prospects_tenant_merged (tenant_id, merged_into_id)``; app/repositories/contacts.py now filters both
statements by tenant, so they lock only the matching index range of one workspace.

Online: ``ALGORITHM=INPLACE, LOCK=NONE`` -- reads and writes to prospects continue while the index builds
(a few seconds per 100k contacts; a metadata lock is taken briefly at the start and the end, so a
long-running transaction on prospects delays it). Should the server refuse the online build, the
migration fails without changes rather than silently locking the table: run it in a quiet period
without the clause.

Idempotent: the index is created only when information_schema does not list it (a fresh build made by
create_all from the models already has it). No data changes. Downgrade drops the index (also online).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0033_prospect_merged_into_index"
down_revision: Union[str, Sequence[str], None] = "0032_server_defaults"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "prospects"
INDEX = "ix_prospects_tenant_merged"
COLUMNS = "tenant_id, merged_into_id"


def _has_index() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": TABLE, "i": INDEX}).scalar())


def upgrade() -> None:
    if _has_index():
        return
    op.execute(f"ALTER TABLE {TABLE} ADD INDEX {INDEX} ({COLUMNS}), ALGORITHM=INPLACE, LOCK=NONE")


def downgrade() -> None:
    if not _has_index():
        return
    op.execute(f"ALTER TABLE {TABLE} DROP INDEX {INDEX}, ALGORITHM=INPLACE, LOCK=NONE")
