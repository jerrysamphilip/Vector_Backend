"""Indexes for hot queries and unique keys the data already satisfies

Revision ID: 0002_indexes_constraints
Revises: 0001_baseline
Create Date: 2026-10-08

Every index is created only when it is missing and its columns exist. A unique key is
added only when the table has no duplicates for it; otherwise it is skipped with a warning
(no data is changed) and can be added by a later revision once the duplicates are resolved.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0002_indexes_constraints"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# (table, index name, column list or key expression)
INDEXES = (
    # SES webhook and IMAP reply threading look messages up by provider Message-ID
    ("email_messages", "ix_email_messages_provider_message_id", ("provider_message_id",)),
    # Per-message event lookups (opens/clicks for one message) and daily event rollups
    ("email_events", "ix_email_events_message_type", ("message_id", "event_type")),
    ("email_events", "ix_email_events_time_type", ("event_time", "event_type")),
    # Sequence executor: ACTIVE enrolments whose next step is due
    ("campaign_prospects", "ix_campaign_prospects_status_next", ("status", "next_scheduled_at")),
    # Inbox list: a tenant's conversations, newest first
    ("conversations", "ix_conversations_tenant_last_message", ("tenant_id", "last_message_at")),
)

# Case-insensitive email lookups (func.lower(Prospect.email) == ...) use a functional index
FUNCTIONAL_INDEXES = (
    ("prospects", "ix_prospects_email_lower", "email", "(lower(`email`))"),
)

# (table, constraint name, columns). Skipped when duplicates exist.
UNIQUES = (
    # One thread per contact per mailbox (scheduler and IMAP sync find-or-create on this key)
    ("conversations", "uq_conversations_prospect_inbox", ("prospect_id", "inbox_id")),
    # A contact appears once in a list
    ("prospect_list_members", "uq_list_member", ("list_id", "prospect_id")),
)


def _columns(conn, table):
    return {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table})}


def _index_exists(conn, table, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND INDEX_NAME = :n"), {"t": table, "n": name}).first())


def _quoted(cols):
    return ", ".join(f"`{c}`" for c in cols)


def upgrade() -> None:
    conn = op.get_bind()

    for table, name, cols in INDEXES:
        if _index_exists(conn, table, name):
            continue
        missing = set(cols) - _columns(conn, table)
        if missing:
            logger.warning("Skipping index %s: %s has no column(s) %s", name, table, sorted(missing))
            continue
        conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` ({_quoted(cols)})"))
        logger.info("Added index %s on %s(%s)", name, table, ", ".join(cols))

    for table, name, column, expr in FUNCTIONAL_INDEXES:
        if not _index_exists(conn, table, name) and column in _columns(conn, table):
            conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` ({expr})"))
            logger.info("Added functional index %s on %s %s", name, table, expr)

    for table, name, cols in UNIQUES:
        if _index_exists(conn, table, name):
            continue
        missing = set(cols) - _columns(conn, table)
        if missing:
            logger.warning("Skipping unique key %s: %s has no column(s) %s", name, table, sorted(missing))
            continue
        dupes = conn.execute(text(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM `{table}` GROUP BY {_quoted(cols)} HAVING COUNT(*) > 1) d"
        )).scalar()
        if dupes:
            logger.warning("Skipping unique key %s on %s(%s): %d duplicate group(s) exist. Resolve them, "
                           "then add the key in a new revision.", name, table, ", ".join(cols), dupes)
            continue
        conn.execute(text(f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` UNIQUE ({_quoted(cols)})"))
        logger.info("Added unique key %s on %s(%s)", name, table, ", ".join(cols))


def downgrade() -> None:
    conn = op.get_bind()
    for table, name, _ in UNIQUES:
        if _index_exists(conn, table, name):
            conn.execute(text(f"ALTER TABLE `{table}` DROP INDEX `{name}`"))
    for table, name, *_ in INDEXES + FUNCTIONAL_INDEXES:
        if _index_exists(conn, table, name):
            conn.execute(text(f"DROP INDEX `{name}` ON `{table}`"))
