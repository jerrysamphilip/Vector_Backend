"""Drop campaign_metrics_realtime (counters written on every event, read by nothing)

Revision ID: 0036_drop_metrics_realtime
Revises: 0035_schema_parity
Create Date: 2026-10-29

``campaign_metrics_realtime`` held per-campaign counters (sent / opened / replied / bounced / sender-bounced /
positive-reply / out-of-office) that the scheduler, the SES webhook, manual tracking and campaign create /
duplicate kept incrementing, but no route, report or job read them: GET /campaigns/{id}/metrics, the campaign
detail page and every report compute the numbers from email_messages / email_events
(CampaignAnalyticsService, app/repositories/reports_campaign_metrics.py), and some event sources (open
tracking) never updated the table, so its values were wrong anyway. The writers and the model are gone; this
revision drops the table.

The rows are derived data (a recount of email_events), so nothing is lost: the row count is logged before the
drop. Metadata-quick (DROP TABLE; no other table references it).

Idempotent: the table is dropped only when information_schema lists it (a fresh build made by create_all from
the models no longer has it).

Downgrade recreates the table EMPTY with the exact DDL the model produced (fresh build at 0035); the code of
this revision never writes it again. Run the downgrade only together with the 0035 code, whose counters then
start again from zero (they were never read).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0036_drop_metrics_realtime"
down_revision: Union[str, Sequence[str], None] = "0035_schema_parity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "campaign_metrics_realtime"

# SHOW CREATE TABLE of a fresh build at 0035 (Base.metadata.create_all from app/models/metrics.py)
CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
  campaign_id varchar(36) NOT NULL,
  sent_count int DEFAULT NULL,
  opened_count int DEFAULT NULL,
  replied_count int DEFAULT NULL,
  bounced_count int DEFAULT NULL,
  sender_bounced_count int DEFAULT NULL,
  positive_replied_count int DEFAULT NULL,
  ooo_count int DEFAULT NULL,
  row_version int DEFAULT NULL,
  updated_at datetime(6) DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (campaign_id),
  CONSTRAINT campaign_metrics_realtime_ibfk_1 FOREIGN KEY (campaign_id) REFERENCES campaigns (campaign_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


def _has_table() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": TABLE}).scalar())


def upgrade() -> None:
    if not _has_table():
        return
    rows = op.get_bind().execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar() or 0  # nosec B608 (constant)
    logger.info("0036: dropping %s (%s rows of derived counters, never read)", TABLE, rows)
    op.execute(f"DROP TABLE {TABLE}")


def downgrade() -> None:
    if _has_table():
        return
    op.execute(CREATE_TABLE)
