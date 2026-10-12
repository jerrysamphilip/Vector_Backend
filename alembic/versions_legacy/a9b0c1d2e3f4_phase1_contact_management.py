"""Phase 1 Contact Management (BRD v2.0): lifecycle stage, lead status, lead source, legal
basis and soft delete on contacts; domain-keyed companies with revenue and address;
static/active lists; property groups; property history, tasks, saved views, import jobs

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-10-08 00:00:00.000000

The app applies the same changes on start-up (app/db/contact_schema.py); every step
checks what already exists.
"""
from alembic import op

revision = 'a9b0c1d2e3f4'
down_revision = 'f8a9b0c1d2e3'
branch_labels = None
depends_on = None

NEW_TABLES = ("property_changes", "crm_tasks", "saved_views", "import_jobs")


def upgrade():
    from app.db.contact_schema import ensure_phase1_schema
    from app.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[t] for t in NEW_TABLES])
    ensure_phase1_schema(bind.engine)


def downgrade():
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    op.drop_index("uq_account_tenant_domain", table_name="accounts")
    op.drop_index("ix_prospects_lifecycle_stage", table_name="prospects")
    op.drop_index("ix_prospects_deleted_at", table_name="prospects")
    for table, columns in {
        "prospects": ("lifecycle_stage", "lead_status", "lead_source", "legal_basis", "deleted_at", "deleted_by", "merged_into_id"),
        "accounts": ("street", "postal_code", "annual_revenue", "lifecycle_stage", "deleted_at", "deleted_by"),
        "prospect_lists": ("list_type", "filters", "description"),
        "contact_field_definitions": ("group_name", "required"),
    }.items():
        for column in columns:
            op.drop_column(table, column)
