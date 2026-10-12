"""recreate sending_domains table

Revision ID: 0dbb31ba2ed0
Revises: a4b1c2d3e4f5
Create Date: 2026-03-27 12:43:52.175217

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0dbb31ba2ed0'
down_revision: Union[str, Sequence[str], None] = 'a4b1c2d3e4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
