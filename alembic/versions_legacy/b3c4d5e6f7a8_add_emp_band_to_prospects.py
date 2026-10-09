"""add emp_band to prospects

Revision ID: b3c4d5e6f7a8
Revises: 0dbb31ba2ed0
Create Date: 2026-04-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = '0dbb31ba2ed0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add emp_band column to prospects table."""
    op.add_column('prospects', sa.Column('emp_band', sa.String(length=50), nullable=True))


def downgrade() -> None:
    """Remove emp_band column from prospects table."""
    op.drop_column('prospects', 'emp_band')
