"""Sending defect fixes (BRD v2.0 BR-DF-01, 04, 05, 06, 07): duplicate-send key and
claim time, SES message id and final delivery status on messages; pause reason on
campaigns; Microsoft 365 OAuth fields and sync error on inboxes; rolling 24h health
on sending domains

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-10-09 00:00:00.000000

The app applies the same changes on start-up (app/db/sending_schema.py); every step
checks what already exists.
"""
from alembic import op

revision = 'b0c1d2e3f4a5'
down_revision = 'a9b0c1d2e3f4'
branch_labels = None
depends_on = None


def upgrade():
    from app.db.sending_schema import ensure_sending_schema
    ensure_sending_schema(op.get_bind().engine)


def downgrade():
    from app.db.sending_schema import _COLUMNS, _INDEXES
    for table, name, _cols, _unique in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    for table, columns in _COLUMNS.items():
        for column in columns:
            op.drop_column(table, column)
