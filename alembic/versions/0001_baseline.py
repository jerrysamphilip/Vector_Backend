"""Baseline: the full current schema, built idempotently

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-08

Works on an empty database and on any database created by an older build (the start-up
DDL that used to live in app/main.py, or the legacy chain kept in alembic/versions_legacy/).
create_all() adds missing tables; app.db.schema_patches.apply_all() adds the columns,
indexes and constraints that create_all() cannot add to existing tables. Both check before
they change anything, so re-running this revision is a no-op.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.models import Base
    from app.db.schema_patches import apply_all

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind.engine)
    apply_all(bind)


def downgrade() -> None:
    raise NotImplementedError("The baseline cannot be downgraded; restore from a backup instead.")
