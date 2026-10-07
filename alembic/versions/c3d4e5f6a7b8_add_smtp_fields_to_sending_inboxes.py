"""Add SMTP fields to sending_inboxes for per-inbox warmup sending

Revision ID: c3d4e5f6a7b8
Revises: a4b1c2d3e4f5
Create Date: 2026-04-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'a4b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sending_inboxes', sa.Column('smtp_host', sa.String(255), nullable=True))
    op.add_column('sending_inboxes', sa.Column('smtp_port', sa.Integer(), nullable=True, server_default='587'))
    op.add_column('sending_inboxes', sa.Column('smtp_username', sa.String(255), nullable=True))
    op.add_column('sending_inboxes', sa.Column('smtp_password', sa.String(255), nullable=True))
    op.add_column('sending_inboxes', sa.Column('smtp_use_ssl', sa.Boolean(), nullable=True, server_default='0'))


def downgrade():
    op.drop_column('sending_inboxes', 'smtp_use_ssl')
    op.drop_column('sending_inboxes', 'smtp_password')
    op.drop_column('sending_inboxes', 'smtp_username')
    op.drop_column('sending_inboxes', 'smtp_port')
    op.drop_column('sending_inboxes', 'smtp_host')
