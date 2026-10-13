"""A/B testing of email subject lines (BRD v2.0 BR-SF-18, RP-104)

Revision ID: 0010_ab_tests
Revises: 0009_discount_approval
Create Date: 2026-10-13

email_subject_variants: 2-4 subject variants (A..D, optional weight) per sequence step, with
the promoted winner. email_messages.subject_variant: the variant label each pre-scheduled
email was assigned (kept when the schedule is re-planned). Idempotent: the table is created
IF NOT EXISTS and the column only when missing (0001_baseline's create_all already creates
both on an empty database).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0010_ab_tests"
down_revision: Union[str, Sequence[str], None] = "0009_discount_approval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def _columns(conn, table):
    return {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table})}


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS email_subject_variants ("
        " variant_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " campaign_id VARCHAR(36) NOT NULL,"
        " sequence_id VARCHAR(36) NOT NULL,"
        " label VARCHAR(1) NOT NULL,"
        " subject VARCHAR(500) NOT NULL,"
        " weight INTEGER NOT NULL DEFAULT 1,"
        " is_winner BOOLEAN NOT NULL DEFAULT 0,"
        " promoted_at TIMESTAMP NULL,"
        " promoted_by VARCHAR(36) NULL,"
        " created_by VARCHAR(36) NULL,"
        " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " CONSTRAINT uq_subject_variants_sequence_label UNIQUE (sequence_id, label),"
        " INDEX ix_email_subject_variants_campaign_id (campaign_id),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (campaign_id) REFERENCES campaigns (campaign_id) ON DELETE CASCADE,"
        " FOREIGN KEY (sequence_id) REFERENCES email_sequences (sequence_id) ON DELETE CASCADE,"
        " FOREIGN KEY (promoted_by) REFERENCES users (user_id),"
        " FOREIGN KEY (created_by) REFERENCES users (user_id)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
    if "subject_variant" not in _columns(conn, "email_messages"):
        conn.execute(text("ALTER TABLE `email_messages` ADD COLUMN `subject_variant` VARCHAR(1) NULL"))
        logger.info("Added email_messages.subject_variant")


def downgrade() -> None:
    conn = op.get_bind()
    if "subject_variant" in _columns(conn, "email_messages"):
        conn.execute(text("ALTER TABLE `email_messages` DROP COLUMN `subject_variant`"))
    conn.execute(text("DROP TABLE IF EXISTS email_subject_variants"))
