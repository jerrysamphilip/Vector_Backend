"""Phase 2 'Should' features (BRD v2.0 BR-SF-01..16): notifications, workflow rules, sales
settings, targets, template library, saved reports, products and quote lines, user
connections; deal links on tasks and activities; forecast category and source campaign on
deals; recycle date on leads; email-notification preference on users

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-11 00:00:00.000000
"""
from alembic import op

revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None

NEW_TABLES = ("notifications", "workflow_rules", "sales_settings", "sales_targets", "message_templates",
              "saved_reports", "products", "proposal_lines", "user_connections")


def upgrade():
    from app.db.phase2_schema import ensure_phase2_schema
    from app.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[t] for t in NEW_TABLES])
    ensure_phase2_schema(bind.engine)


def downgrade():
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    for table, columns in {"users": ("notify_email",), "leads": ("recycle_at",),
                           "opportunities": ("forecast_category", "forecast_category_manual", "campaign_id"),
                           "crm_tasks": ("opportunity_id",),
                           "contact_activities": ("opportunity_id", "source", "external_id")}.items():
        for column in columns:
            op.drop_column(table, column)
