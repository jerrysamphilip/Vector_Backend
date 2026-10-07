"""Add sending schedule fields to campaigns

Revision ID: e1f2a3b4c5d6
Revises: c3d4e5f6a7b8
Create Date: 2026-04-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'e1f2a3b4c5d6'
down_revision = ('c3d4e5f6a7b8', '0dbb31ba2ed0')
branch_labels = None
depends_on = None


def upgrade():
    # sending_mode: how emails are distributed across the send window
    # 'spread'  = evenly distributed (default)
    # 'random'  = random time per email within the window
    # 'batch'   = sent in groups with a gap between batches
    op.add_column('campaigns', sa.Column(
        'sending_mode', sa.String(20), nullable=True, server_default='spread'
    ))
    # Minimum gap between consecutive emails in the same window (random mode)
    op.add_column('campaigns', sa.Column(
        'min_gap_minutes', sa.Integer(), nullable=True, server_default='2'
    ))
    # Number of emails per batch (batch mode only)
    op.add_column('campaigns', sa.Column(
        'batch_size', sa.Integer(), nullable=True
    ))
    # Minutes between the start of consecutive batches (batch mode only)
    op.add_column('campaigns', sa.Column(
        'batch_gap_minutes', sa.Integer(), nullable=True, server_default='30'
    ))


def downgrade():
    op.drop_column('campaigns', 'batch_gap_minutes')
    op.drop_column('campaigns', 'batch_size')
    op.drop_column('campaigns', 'min_gap_minutes')
    op.drop_column('campaigns', 'sending_mode')
