"""add poc_country to prospects"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "dced84674915"
down_revision = "7a1d3f4b5c6e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "prospects",
        sa.Column("poc_country", sa.String(length=100), nullable=True),
    )


def downgrade():
    op.drop_column("prospects", "poc_country")