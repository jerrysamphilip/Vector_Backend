"""Reply classification on inbound emails: label, confidence, source, classified_at, attempts, review flag (S-09)

Revision ID: 0023_reply_classification
Revises: 0022_campaign_source
Create Date: 2026-10-27

email_messages gets, for INBOUND rows (NULL / 0 on every other row):

* ``reply_label`` VARCHAR(20): POSITIVE / NEUTRAL / NEGATIVE / OUT_OF_OFFICE / UNSUBSCRIBE
* ``reply_confidence`` FLOAT (0-1) and ``reply_label_source`` VARCHAR(10): HEADER / RULES / LLM
* ``reply_classified_at`` DATETIME(6): NULL = not classified yet (the reply_classification job retries)
* ``reply_classify_attempts`` INT NOT NULL DEFAULT 0
* ``reply_needs_review`` TINYINT(1) NOT NULL DEFAULT 0: a low-confidence UNSUBSCRIBE, flagged for a
  person instead of suppressing the contact
* index ``ix_email_messages_reply_pending`` (direction, reply_classified_at, sent_at) for the job

Existing replies stay unlabelled; the job labels those of the last 30 days (app/domain/inbox.py
CLASSIFY_LOOKBACK_DAYS), without adding a second positive-reply event where the old pipeline already
wrote one. Every step checks information_schema first, so the revision is a no-op on a fresh build
(create_all in 0001 already makes the columns from app/models/email_message.py) and safe to re-run.
No data is changed. Downgrade drops the index and the columns (the labels are lost; the replies are not).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0023_reply_classification"
down_revision: Union[str, Sequence[str], None] = "0022_campaign_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "email_messages"
PENDING_INDEX = "ix_email_messages_reply_pending"
COLUMNS = (
    ("reply_label", "VARCHAR(20) NULL"),
    ("reply_confidence", "FLOAT NULL"),
    ("reply_label_source", "VARCHAR(10) NULL"),
    ("reply_classified_at", "DATETIME(6) NULL"),
    ("reply_classify_attempts", "INT NOT NULL DEFAULT 0"),
    ("reply_needs_review", "TINYINT(1) NOT NULL DEFAULT 0"),
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


def upgrade() -> None:
    missing = [f"ADD COLUMN {name} {ddl}" for name, ddl in COLUMNS if not _has_column(name)]
    if missing:
        op.execute(f"ALTER TABLE `{TABLE}` " + ", ".join(missing))
    if not _has_index(PENDING_INDEX):
        op.execute(f"CREATE INDEX {PENDING_INDEX} ON `{TABLE}` (direction, reply_classified_at, sent_at)")


def downgrade() -> None:
    if _has_index(PENDING_INDEX):
        op.execute(f"DROP INDEX {PENDING_INDEX} ON `{TABLE}`")
    present = [f"DROP COLUMN {name}" for name, _ in COLUMNS if _has_column(name)]
    if present:
        op.execute(f"ALTER TABLE `{TABLE}` " + ", ".join(present))
