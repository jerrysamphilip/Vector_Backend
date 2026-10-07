"""add notes to prospect_list_members"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = ("dced84674915", "add_suppression_expires_at")
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "prospect_list_members",
        sa.Column("notes", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("prospect_list_members", "notes")
