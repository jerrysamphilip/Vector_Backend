"""Contact management: phone, owner, account, tags and custom fields on prospects;
accounts, contact_activities and contact_field_definitions tables

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-07 00:00:00.000000

The app also applies these changes on startup (app/db/contact_schema.py), so every
step checks what already exists.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e7f8a9b0c1d2'
down_revision = 'd6e7f8a9b0c1'
branch_labels = None
depends_on = None

MYSQL_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade():
    from app.db.contact_schema import ensure_contact_schema

    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "accounts" not in tables:
        op.create_table(
            "accounts",
            sa.Column("account_id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.tenant_id"), nullable=False, index=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("domain", sa.String(255)),
            sa.Column("website", sa.String(500)),
            sa.Column("phone", sa.String(50)),
            sa.Column("industry", sa.String(100)),
            sa.Column("emp_band", sa.String(50)),
            sa.Column("city", sa.String(100)),
            sa.Column("state", sa.String(100)),
            sa.Column("country", sa.String(100)),
            sa.Column("description", sa.Text),
            sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.user_id")),
            sa.Column("created_at", sa.TIMESTAMP, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "name", name="uq_account_tenant_name"),
            **MYSQL_OPTS,
        )
    if "contact_activities" not in tables:
        op.create_table(
            "contact_activities",
            sa.Column("activity_id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.tenant_id"), nullable=False),
            sa.Column("prospect_id", sa.String(36), sa.ForeignKey("prospects.prospect_id"), nullable=False, index=True),
            sa.Column("activity_type", sa.String(20), nullable=False),
            sa.Column("subject", sa.String(255)),
            sa.Column("body", sa.Text),
            sa.Column("outcome", sa.String(100)),
            sa.Column("duration_minutes", sa.Integer),
            sa.Column("occurred_at", sa.TIMESTAMP, nullable=False, server_default=sa.func.now()),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.user_id")),
            sa.Column("created_at", sa.TIMESTAMP, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP, server_default=sa.func.now()),
            **MYSQL_OPTS,
        )
    if "contact_field_definitions" not in tables:
        op.create_table(
            "contact_field_definitions",
            sa.Column("field_id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.tenant_id"), nullable=False),
            sa.Column("field_key", sa.String(64), nullable=False),
            sa.Column("label", sa.String(100), nullable=False),
            sa.Column("field_type", sa.String(20), nullable=False),
            sa.Column("options", sa.JSON),
            sa.Column("sort_order", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.TIMESTAMP, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "field_key", name="uq_contact_field_key"),
            **MYSQL_OPTS,
        )

    ensure_contact_schema(op.get_bind().engine)


def downgrade():
    op.drop_constraint("fk_prospects_account", "prospects", type_="foreignkey")
    op.drop_constraint("fk_prospects_owner", "prospects", type_="foreignkey")
    op.drop_index("ix_prospects_account_id", table_name="prospects")
    op.drop_index("ix_prospects_owner_id", table_name="prospects")
    for column in ("custom_fields", "tags", "account_id", "owner_id", "mobile_phone", "phone"):
        op.drop_column("prospects", column)
    op.drop_table("contact_field_definitions")
    op.drop_table("contact_activities")
    op.drop_table("accounts")
