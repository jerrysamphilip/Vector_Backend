"""add suppression_expires_at to global_unsubscribes

Revision ID: add_suppression_expires_at
Revises: f915baccf877
Create Date: 2026-02-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_suppression_expires_at'
down_revision = 'f915baccf877'  # Latest migration: add_campaign_timezone
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'global_unsubscribes',
        sa.Column(
            'suppression_expires_at',
            sa.TIMESTAMP(),
            nullable=True,
            server_default=None,
        )
    )


def downgrade() -> None:
    op.drop_column('global_unsubscribes', 'suppression_expires_at')
