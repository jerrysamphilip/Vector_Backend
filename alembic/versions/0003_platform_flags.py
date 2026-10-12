"""Platform flags (scheduler pause shared by the API and the worker)

Revision ID: 0003_platform_flags
Revises: 0002_indexes_constraints
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_platform_flags"
down_revision: Union[str, Sequence[str], None] = "0002_indexes_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS platform_flags ("
        " name VARCHAR(64) NOT NULL PRIMARY KEY,"
        " value VARCHAR(255) NOT NULL,"
        " updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_flags")
