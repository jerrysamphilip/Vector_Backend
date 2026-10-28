"""Server-side column DEFAULTs that a fresh build lacked (fresh DB = upgraded DB)

Revision ID: 0032_server_defaults
Revises: 0031_property_safety
Create Date: 2026-10-28

A database built from an empty schema gets its tables from the baseline's create_all(), which only
emits the DEFAULT clauses the models declare as server_default. Several columns had just a Python-side
default= there, while databases upgraded through the old start-up DDL / legacy chain carry a server
DEFAULT for them. Raw-SQL inserts that leave such a column out (system tests, scripts, manual fixes)
then worked on an upgraded database and failed on a fresh one ("Field 'notify_email' doesn't have a
default value"). The models now declare the server defaults; this revision adds them to existing
databases that are missing them:

  campaigns.auto_paused                    0
  contact_companies.is_primary             0
  contact_field_definitions.sort_order     0
  email_subject_variants.is_winner         0
  email_subject_variants.weight            1
  forecast_snapshots.forecast              0
  forecast_snapshots.open_count            0
  forecast_snapshots.source                'SCHEDULED'
  forecast_snapshots.weighted              0
  forecast_snapshots.won                   0
  imap_sync_state.last_uid                 0
  import_jobs.skipped_count                0   (0026 adds it with DEFAULT 0, the model had none)
  opportunities.forecast_category_manual   0
  sending_domains.bounce_rate_24h          0
  sending_domains.complaint_rate_24h       0
  sending_domains.sends_24h                0
  sending_inboxes.auth_type                'PASSWORD'
  users.notify_email                       1
  webhook_deliveries.attempts              0
  webhook_deliveries.is_test               0
  webhook_deliveries.status                'PENDING'
  webhook_endpoints.active                 1
  webhook_endpoints.consecutive_failures   0

Idempotent: information_schema is read first and `ALTER TABLE .. ALTER COLUMN .. SET DEFAULT ..` runs only
for a column that has no default yet. That statement only changes the data dictionary (no table copy, no
lock beyond a brief metadata lock) and no row is changed. A missing table/column, or a column that already
has a DIFFERENT default, is skipped with a warning (a deliberate local default is not overwritten).

Downgrade is a documented no-op: the defaults are harmless (every insert through the ORM sets the column
anyway), databases upgraded through the old migrations always had them, and a fresh build at 0031 made by
this code base gets them from create_all() too, so there is no reliable record of which databases lacked
them; dropping them would only bring the fresh-vs-upgraded difference back.
"""
import logging
from typing import Optional, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0032_server_defaults"
down_revision: Union[str, Sequence[str], None] = "0031_property_safety"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# (table, column, SQL literal of the default, value information_schema.COLUMN_DEFAULT shows for it)
DEFAULTS = (
    ("campaigns", "auto_paused", "0", ("0",)),
    ("contact_companies", "is_primary", "0", ("0",)),
    ("contact_field_definitions", "sort_order", "0", ("0",)),
    ("email_subject_variants", "is_winner", "0", ("0",)),
    ("email_subject_variants", "weight", "1", ("1",)),
    ("forecast_snapshots", "forecast", "0", ("0", "0.00")),
    ("forecast_snapshots", "open_count", "0", ("0",)),
    ("forecast_snapshots", "source", "'SCHEDULED'", ("SCHEDULED",)),
    ("forecast_snapshots", "weighted", "0", ("0", "0.00")),
    ("forecast_snapshots", "won", "0", ("0", "0.00")),
    ("imap_sync_state", "last_uid", "0", ("0",)),
    ("import_jobs", "skipped_count", "0", ("0",)),
    ("opportunities", "forecast_category_manual", "0", ("0",)),
    ("sending_domains", "bounce_rate_24h", "0", ("0",)),
    ("sending_domains", "complaint_rate_24h", "0", ("0",)),
    ("sending_domains", "sends_24h", "0", ("0",)),
    ("sending_inboxes", "auth_type", "'PASSWORD'", ("PASSWORD",)),
    ("users", "notify_email", "1", ("1",)),
    ("webhook_deliveries", "attempts", "0", ("0",)),
    ("webhook_deliveries", "is_test", "0", ("0",)),
    ("webhook_deliveries", "status", "'PENDING'", ("PENDING",)),
    ("webhook_endpoints", "active", "1", ("1",)),
    ("webhook_endpoints", "consecutive_failures", "0", ("0",)),
)


def _column_default(table: str, column: str) -> tuple[bool, Optional[str]]:
    """(column exists, its current COLUMN_DEFAULT or None)."""
    row = op.get_bind().execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": table, "c": column}).first()
    if row is None:
        return False, None
    return True, row[0]


def upgrade() -> None:
    for table, column, literal, shown in DEFAULTS:
        exists, current = _column_default(table, column)
        if not exists:
            logger.warning("0032_server_defaults: %s.%s does not exist; default %s not set", table, column, literal)
        elif current is None:
            op.execute(f"ALTER TABLE `{table}` ALTER COLUMN `{column}` SET DEFAULT {literal}")
        elif current not in shown:
            logger.warning("0032_server_defaults: %s.%s already has DEFAULT %r; left as it is (expected %s)",
                           table, column, current, literal)


def downgrade() -> None:
    # Documented no-op (see the module docstring): the defaults stay.
    pass
