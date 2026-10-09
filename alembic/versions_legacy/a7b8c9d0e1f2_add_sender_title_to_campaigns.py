"""Add sender_title to campaigns

Revision ID: a7b8c9d0e1f2
Revises: d1e2f3a4b5c6
Create Date: 2026-07-13 00:00:00.000000

Adds sender_title column to campaigns table, used to build the
{{signature_block}} token (sender_name / sender_title / company) in
generated emails instead of a bare sender name.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7b8c9d0e1f2'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('campaigns', sa.Column(
        'sender_title', sa.String(length=150), nullable=True
    ))


def downgrade():
    op.drop_column('campaigns', 'sender_title')
