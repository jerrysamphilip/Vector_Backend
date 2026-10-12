"""Add cta_link to email_templates

Revision ID: 7a1d3f4b5c6e
Revises: 614634adcede
Create Date: 2026-01-31 16:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a1d3f4b5c6e'
down_revision: Union[str, Sequence[str], None] = '614634adcede'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('email_templates', sa.Column('cta_link', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('email_templates', 'cta_link')
