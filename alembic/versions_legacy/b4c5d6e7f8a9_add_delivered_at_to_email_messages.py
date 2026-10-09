"""Add delivered_at to email_messages

Revision ID: b4c5d6e7f8a9
Revises: a7b8c9d0e1f2
Create Date: 2026-09-26 00:00:00.000000

Adds delivered_at column to email_messages so SES delivery confirmations
can be recorded distinctly from "sent" without repurposing the `status`
column (which most reporting/metrics queries filter on as == "SENT").
"""
from alembic import op
import sqlalchemy as sa

revision = 'b4c5d6e7f8a9'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('email_messages', sa.Column(
        'delivered_at', sa.TIMESTAMP(), nullable=True
    ))


def downgrade():
    op.drop_column('email_messages', 'delivered_at')
