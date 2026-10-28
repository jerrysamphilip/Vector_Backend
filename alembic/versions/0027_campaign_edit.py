"""campaigns.wait_business_days: wait days between steps count business days (F-04 edit after launch)

Revision ID: 0027_campaign_edit
Revises: 0026_import_jobs
Create Date: 2026-10-27

Editing a launched campaign (app/services/campaign_edit_service.py, POST /campaigns/{id}/edits) lets the
user choose whether a step's wait days are calendar days (the behaviour so far, the default) or business
days (Monday to Friday, no US federal holidays). Pre-scheduling and the edit re-schedule read it.

  campaigns.wait_business_days TINYINT(1) NOT NULL DEFAULT 0

Idempotent: the column is added only when missing (information_schema), so on a fresh build, where the
baseline's create_all already made it from app/models/campaign.py, the revision changes nothing. Existing
rows get 0 (calendar days: exactly how they are scheduled today); no other data is changed.

Downgrade drops the column (campaigns that used business days fall back to calendar days for emails
scheduled afterwards; emails already scheduled keep their times).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0027_campaign_edit"
down_revision: Union[str, Sequence[str], None] = "0026_import_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "campaigns"
COLUMN = "wait_business_days"


def _has_column() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": TABLE, "c": COLUMN}).scalar())


def upgrade() -> None:
    if not _has_column():
        op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TINYINT(1) NOT NULL DEFAULT 0")


def downgrade() -> None:
    if _has_column():
        op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
