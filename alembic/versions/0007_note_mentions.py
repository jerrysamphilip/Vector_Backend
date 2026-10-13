"""@mentions of colleagues in notes (BR-CM-16, CM-033)

Revision ID: 0007_note_mentions
Revises: 0006_contact_companies
Create Date: 2026-10-12

note_mentions: one row per user mentioned in a note (contact_activities row). Deleting the
note removes its mentions. Idempotent (CREATE TABLE IF NOT EXISTS).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_note_mentions"
down_revision: Union[str, Sequence[str], None] = "0006_contact_companies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS note_mentions ("
        " mention_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " activity_id VARCHAR(36) NOT NULL,"
        " user_id VARCHAR(36) NOT NULL,"
        " created_by VARCHAR(36) NULL,"
        " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " CONSTRAINT uq_note_mentions_activity_user UNIQUE (activity_id, user_id),"
        " INDEX ix_note_mentions_user (user_id),"
        " INDEX ix_note_mentions_tenant (tenant_id),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (activity_id) REFERENCES contact_activities (activity_id) ON DELETE CASCADE,"
        " FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,"
        " FOREIGN KEY (created_by) REFERENCES users (user_id)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS note_mentions")
