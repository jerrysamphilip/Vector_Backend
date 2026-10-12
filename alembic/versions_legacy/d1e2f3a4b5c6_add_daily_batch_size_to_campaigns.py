"""Add daily_batch_size to campaigns

Revision ID: d1e2f3a4b5c6
Revises: e1f2a3b4c5d6, b3c4d5e6f7a8
Create Date: 2026-05-28 00:00:00.000000

Adds daily_batch_size column to campaigns table.
When set, the campaign scheduler assigns prospects to consecutive
business days in groups of this size:
  prospect 0..(N-1)   → day 1
  prospect N..(2N-1)  → day 2
  etc.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c6'
down_revision = ('e1f2a3b4c5d6', 'b3c4d5e6f7a8')
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('campaigns', sa.Column(
        'daily_batch_size', sa.Integer(), nullable=True
    ))


def downgrade():
    op.drop_column('campaigns', 'daily_batch_size')
