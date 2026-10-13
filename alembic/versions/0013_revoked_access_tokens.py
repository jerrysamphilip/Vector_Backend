"""Revoked access tokens: sign-out invalidates the access JWT, not only the refresh token

Revision ID: 0013_revoked_access_tokens
Revises: 0012_contact_scoring
Create Date: 2026-10-13

Access tokens now carry a jti; POST /api/auth/logout stores it here and get_current_user refuses a
listed jti (app/services/token_revocation.py). Rows are kept until the token's own expiry
(expires_at) and pruned on the next revocation.

Idempotent: CREATE TABLE IF NOT EXISTS / DROP TABLE IF EXISTS.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_revoked_access_tokens"
down_revision: Union[str, Sequence[str], None] = "0012_contact_scoring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS revoked_access_tokens ("
        " jti VARCHAR(64) NOT NULL PRIMARY KEY,"
        " user_id VARCHAR(36) NULL,"
        " expires_at DATETIME NOT NULL,"
        " revoked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " INDEX ix_revoked_access_tokens_expires_at (expires_at)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS revoked_access_tokens")
