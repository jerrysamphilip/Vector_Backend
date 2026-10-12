"""Two-factor sign-in (TOTP): users.mfa_* and tenants.require_mfa

Revision ID: 0004_user_mfa
Revises: 0003_platform_flags
Create Date: 2026-10-10

Each column is added only when missing (0001_baseline's create_all already creates them on an
empty database). users.mfa_secret holds an EncryptedString value ("enc:v1:<fernet token>").
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0004_user_mfa"
down_revision: Union[str, Sequence[str], None] = "0003_platform_flags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# (table, column, definition)
COLUMNS = (
    ("users", "mfa_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "mfa_secret", "VARCHAR(1024) NULL"),
    ("users", "mfa_recovery_codes", "TEXT NULL"),
    ("users", "mfa_enabled_at", "DATETIME NULL"),
    ("users", "mfa_last_step", "BIGINT NULL"),
    ("tenants", "require_mfa", "BOOLEAN NOT NULL DEFAULT 0"),
)


def _columns(conn, table):
    return {r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table})}


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, definition in COLUMNS:
        if column in _columns(conn, table):
            continue
        conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"))
        logger.info("Added %s.%s", table, column)


def downgrade() -> None:
    conn = op.get_bind()
    for table, column, _ in reversed(COLUMNS):
        if column in _columns(conn, table):
            conn.execute(text(f"ALTER TABLE `{table}` DROP COLUMN `{column}`"))
