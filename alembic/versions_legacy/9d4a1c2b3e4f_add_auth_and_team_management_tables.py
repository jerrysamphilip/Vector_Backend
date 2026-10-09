"""Add auth fields, invitations, and refresh tokens

Revision ID: 9d4a1c2b3e4f
Revises: a1b2c3d4e5f6
Create Date: 2026-03-13 17:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9d4a1c2b3e4f"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_unique(insp, table_name: str, constraint_name: str) -> bool:
    return any(uc.get("name") == constraint_name for uc in insp.get_unique_constraints(table_name))


def _has_fk(insp, table_name: str, fk_name: str) -> bool:
    return any(fk.get("name") == fk_name for fk in insp.get_foreign_keys(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "users" in insp.get_table_names():
        user_cols = {c["name"] for c in insp.get_columns("users")}

        with op.batch_alter_table("users") as batch_op:
            if "password_hash" not in user_cols:
                batch_op.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))
            if "auth_provider" not in user_cols:
                batch_op.add_column(sa.Column("auth_provider", sa.String(length=50), nullable=True, server_default="local"))
            if "google_id" not in user_cols:
                batch_op.add_column(sa.Column("google_id", sa.String(length=255), nullable=True))
            if "email_verified" not in user_cols:
                batch_op.add_column(sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "avatar_url" not in user_cols:
                batch_op.add_column(sa.Column("avatar_url", sa.String(length=500), nullable=True))
            if "invited_by" not in user_cols:
                batch_op.add_column(sa.Column("invited_by", sa.String(length=36), nullable=True))

        insp = sa.inspect(bind)
        if not _has_unique(insp, "users", "uq_user_tenant_email"):
            with op.batch_alter_table("users") as batch_op:
                batch_op.create_unique_constraint("uq_user_tenant_email", ["tenant_id", "email"])
        if not _has_unique(insp, "users", "uq_users_google_id"):
            with op.batch_alter_table("users") as batch_op:
                batch_op.create_unique_constraint("uq_users_google_id", ["google_id"])
        if not _has_fk(insp, "users", "fk_users_invited_by"):
            with op.batch_alter_table("users") as batch_op:
                batch_op.create_foreign_key(
                    "fk_users_invited_by",
                    "users",
                    ["invited_by"],
                    ["user_id"],
                )

    if "invitations" not in insp.get_table_names():
        op.create_table(
            "invitations",
            sa.Column("invitation_id", sa.String(length=36), nullable=False),
            sa.Column("tenant_id", sa.String(length=36), nullable=False),
            sa.Column("invited_by", sa.String(length=36), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=50), nullable=False, server_default="AGENT"),
            sa.Column("token", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=True, server_default="PENDING"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["invited_by"], ["users.user_id"]),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"]),
            sa.PrimaryKeyConstraint("invitation_id"),
            sa.UniqueConstraint("token", name="uq_invitations_token"),
            mysql_charset="utf8mb4",
            mysql_engine="InnoDB",
        )

    if "refresh_tokens" not in insp.get_table_names():
        op.create_table(
            "refresh_tokens",
            sa.Column("token_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("token_hash", sa.String(length=255), nullable=False),
            sa.Column("device_info", sa.String(length=255), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
            sa.PrimaryKeyConstraint("token_id"),
            sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
            mysql_charset="utf8mb4",
            mysql_engine="InnoDB",
        )
        op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "refresh_tokens" in insp.get_table_names():
        idx = [i.get("name") for i in insp.get_indexes("refresh_tokens")]
        if "ix_refresh_tokens_user_id" in idx:
            op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
        op.drop_table("refresh_tokens")

    if "invitations" in insp.get_table_names():
        op.drop_table("invitations")

    if "users" in insp.get_table_names():
        user_cols = {c["name"] for c in insp.get_columns("users")}
        with op.batch_alter_table("users") as batch_op:
            if _has_fk(insp, "users", "fk_users_invited_by"):
                batch_op.drop_constraint("fk_users_invited_by", type_="foreignkey")
            if _has_unique(insp, "users", "uq_users_google_id"):
                batch_op.drop_constraint("uq_users_google_id", type_="unique")
            if _has_unique(insp, "users", "uq_user_tenant_email"):
                batch_op.drop_constraint("uq_user_tenant_email", type_="unique")

            if "invited_by" in user_cols:
                batch_op.drop_column("invited_by")
            if "avatar_url" in user_cols:
                batch_op.drop_column("avatar_url")
            if "email_verified" in user_cols:
                batch_op.drop_column("email_verified")
            if "google_id" in user_cols:
                batch_op.drop_column("google_id")
            if "auth_provider" in user_cols:
                batch_op.drop_column("auth_provider")
            if "password_hash" in user_cols:
                batch_op.drop_column("password_hash")
