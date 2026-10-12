"""Phase 2 (BRD v2.0 5.4 - 5.10): sales hierarchy on users; leads, sales stages,
opportunities and proposals

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-10-10 00:00:00.000000
"""
from alembic import op

revision = 'c1d2e3f4a5b6'
down_revision = 'b0c1d2e3f4a5'
branch_labels = None
depends_on = None

NEW_TABLES = ("leads", "sales_stages", "opportunities", "proposals")


def upgrade():
    from app.db.phase2_schema import ensure_phase2_schema
    from app.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[t] for t in
                                                ("leads", "sales_stages", "opportunities", "proposals")])
    ensure_phase2_schema(bind.engine)


def downgrade():
    for table in ("proposals", "opportunities", "sales_stages", "leads"):
        op.drop_table(table)
    op.drop_index("ix_email_messages_sent_at", table_name="email_messages")
    op.drop_index("ix_users_manager_id", table_name="users")
    op.drop_column("users", "manager_id")
    op.drop_column("users", "sales_level")
