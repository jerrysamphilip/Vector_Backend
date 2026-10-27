"""Send integrity keys: one email per campaign step per contact, an (inbox_id, sent_at) index, deferred_count

Revision ID: 0020_send_integrity_keys
Revises: 0019_unique_active_email
Create Date: 2026-10-27

email_messages gets:

* ``ix_email_messages_inbox_sent`` (inbox_id, sent_at): per-inbox send history and daily counts no
  longer scan every message of the inbox.
* ``step_key``, a VIRTUAL generated column, and the UNIQUE index ``ux_email_messages_step_key`` on
  it: at most one email per (campaign_id, prospect_id, sequence_id). The key is non-NULL only for
  campaign OUTBOUND step mail that is not CANCELLED, so inbound rows, manual unified-inbox replies
  and automation sends (no sequence_id), SES bounce notices (INBOUND) and duplicates already
  cancelled by the send-time guard are never constrained (a UNIQUE index ignores NULLs). A/B
  variants live on the one row of their step, and a retry re-uses its row, so neither needs a
  second row. Pre-scheduling skips a step that already has its row (app/services/campaign_email_service.py).
* ``deferred_count``: re-queues for a platform / sender-identity failure, which do not spend the
  message's retry budget (app/services/send_breaker.py).

Each step checks information_schema first, so the revision is a no-op on a fresh build (create_all in
0001 already makes all three from the models) and safe to re-run. If live data already holds two
non-cancelled rows for one step, the UNIQUE index is NOT created and a warning lists a few of them:
no data is deleted. Cancel or remove the extra rows, then re-run this revision (downgrade to
0019_unique_active_email and upgrade again). Until then the scheduler's send_key guard still stops a
step going twice to one address.

Downgrade drops what the upgrade adds (the inbox index only when the inbox_id foreign key has another).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0020_send_integrity_keys"
down_revision: Union[str, Sequence[str], None] = "0019_unique_active_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "email_messages"
INBOX_SENT_INDEX = "ix_email_messages_inbox_sent"
STEP_KEY_INDEX = "ux_email_messages_step_key"
# Literal copy of app/models/email_message.py STEP_KEY_SQL: a migration must not change later
STEP_KEY_SQL = (
    "CASE WHEN COALESCE(direction, 'OUTBOUND') = 'OUTBOUND' AND campaign_id IS NOT NULL "
    "AND sequence_id IS NOT NULL AND COALESCE(status, '') <> 'CANCELLED' "
    "THEN CONCAT(campaign_id, ':', prospect_id, ':', sequence_id) END"
)


def _has_column(column: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": TABLE, "c": column}).scalar())


def _has_index(index: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": TABLE, "i": index}).scalar())


def _other_inbox_index() -> bool:
    """Another index leads with inbox_id, so the foreign key does not need ours."""
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = 'inbox_id' AND seq_in_index = 1 AND index_name <> :i"),
        {"t": TABLE, "i": INBOX_SENT_INDEX}).scalar())


def _duplicate_steps(limit: int = 5) -> list:
    return list(op.get_bind().execute(text(
        f"SELECT step_key, COUNT(*) AS n FROM `{TABLE}` WHERE step_key IS NOT NULL "
        f"GROUP BY step_key HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT {int(limit)}")).all())


def upgrade() -> None:
    if not _has_column("deferred_count"):
        op.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN deferred_count INT NOT NULL DEFAULT 0")
    if not _has_index(INBOX_SENT_INDEX):
        op.execute(f"CREATE INDEX {INBOX_SENT_INDEX} ON `{TABLE}` (inbox_id, sent_at)")
    if not _has_column("step_key"):
        op.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN step_key VARCHAR(110) "
                   f"GENERATED ALWAYS AS ({STEP_KEY_SQL}) VIRTUAL")
    if _has_index(STEP_KEY_INDEX):
        return
    duplicates = _duplicate_steps()
    if duplicates:
        sample = ", ".join(f"{key} (x{n})" for key, n in duplicates)
        logger.warning(
            f"0020_send_integrity_keys: NOT creating {STEP_KEY_INDEX}: {TABLE} already holds more than one "
            f"non-cancelled email for some campaign steps, e.g. campaign:prospect:step {sample}. No data was "
            f"changed. Cancel the extra rows, then downgrade to 0019_unique_active_email and upgrade again.")
        return
    op.execute(f"CREATE UNIQUE INDEX {STEP_KEY_INDEX} ON `{TABLE}` (step_key)")


def downgrade() -> None:
    if _has_index(STEP_KEY_INDEX):
        op.execute(f"DROP INDEX {STEP_KEY_INDEX} ON `{TABLE}`")
    if _has_column("step_key"):
        op.execute(f"ALTER TABLE `{TABLE}` DROP COLUMN step_key")
    # On a fresh build the foreign key on inbox_id uses this index (MySQL then makes no index of its
    # own) and the index cannot be dropped; it is harmless to keep
    if _has_index(INBOX_SENT_INDEX) and _other_inbox_index():
        op.execute(f"DROP INDEX {INBOX_SENT_INDEX} ON `{TABLE}`")
    if _has_column("deferred_count"):
        op.execute(f"ALTER TABLE `{TABLE}` DROP COLUMN deferred_count")
