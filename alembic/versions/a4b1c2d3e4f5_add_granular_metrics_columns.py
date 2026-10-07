"""add granular metrics columns

Revision ID: a4b1c2d3e4f5
Revises: f915baccf877
Create Date: 2026-03-26

Adds three new columns to campaign_metrics_realtime for:
  - sender_bounced_count  (SMTP 4xx / SPF-DKIM failures)
  - positive_replied_count (AI-intent: positive buying signals)
  - ooo_count             (OOO/auto-response replies)
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a4b1c2d3e4f5'
down_revision = ('3a896eb84020', '9d4a1c2b3e4f')
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'campaign_metrics_realtime',
        sa.Column('sender_bounced_count', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column(
        'campaign_metrics_realtime',
        sa.Column('positive_replied_count', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column(
        'campaign_metrics_realtime',
        sa.Column('ooo_count', sa.Integer(), nullable=False, server_default='0')
    )


def downgrade() -> None:
    op.drop_column('campaign_metrics_realtime', 'ooo_count')
    op.drop_column('campaign_metrics_realtime', 'positive_replied_count')
    op.drop_column('campaign_metrics_realtime', 'sender_bounced_count')
