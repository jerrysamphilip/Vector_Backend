"""CHECK constraints on status columns: the database refuses a status outside its vocabulary (D5)

Revision ID: 0029_status_checks
Revises: 0028_child_tenant_ids
Create Date: 2026-10-28

One ``CONSTRAINT ck_<table>_<column> CHECK (<column> IN (...))`` per status column, with the vocabularies of
app/domain/status.py (copied here as literals: a migration must not change later):

  email_messages.status / final_status, campaigns.status, campaign_prospects.status, leads.stage,
  opportunities.status, proposals.status / approval_status, users.status, tenants.status, invitations.status,
  import_jobs.status, webhook_deliveries.status

NULL passes a CHECK (nullable status columns stay nullable) and MySQL compares in the column collation
(utf8mb4_0900_ai_ci, case-insensitive), so 'active' passes the users check as 'ACTIVE' does.

Data is never rewritten: before adding a constraint the revision counts the rows outside the allowed set; when
there are any, that constraint is SKIPPED with a warning listing the values and their counts (fix the rows,
then run the ALTER TABLE in the warning). The other constraints are still added.

MySQL validates existing rows while adding a CHECK, which copies the table (ALGORITHM=COPY, writes wait): all
constraints of a table go in one ALTER, so each table is copied once. On a large database run this in a
maintenance window (email_messages is the big one).

Idempotent: a constraint that exists (information_schema.check_constraints) is left alone, so on a fresh build,
where 0001_baseline's create_all already made them from the models, nothing changes. Downgrade drops them.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0029_status_checks"
down_revision: Union[str, Sequence[str], None] = "0028_child_tenant_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# table -> column -> allowed values (literal copy of app/domain/status.py STATUS_CHECKS)
CHECKS: dict[str, dict[str, tuple[str, ...]]] = {
    "email_messages": {
        "status": ("QUEUED", "SCHEDULED", "SENDING", "SENT", "DELIVERED", "BOUNCED", "COMPLAINED", "REJECTED",
                   "FAILED", "CANCELLED", "PAUSED_BY_CAMPAIGN", "RECEIVED"),
        "final_status": ("DELIVERED", "BOUNCED", "COMPLAINED", "REJECTED", "FAILED", "UNCONFIRMED"),
    },
    "campaigns": {"status": ("DRAFT", "ACTIVE", "PAUSED", "COMPLETED")},
    "campaign_prospects": {"status": ("ACTIVE", "OPENED", "COMPLETED", "PAUSED", "RECONNECT_ELIGIBLE", "BOUNCED",
                                      "REPLIED", "UNSUBSCRIBED")},
    "leads": {"stage": ("NEW", "CONTACTED", "ENGAGED", "SQL", "CONVERTED", "DISQUALIFIED")},
    "opportunities": {"status": ("OPEN", "WON", "LOST")},
    "proposals": {
        "status": ("DRAFT", "SENT", "UNDER_REVIEW", "ACCEPTED", "REJECTED"),
        "approval_status": ("NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"),
    },
    "users": {"status": ("ACTIVE", "INVITED", "INACTIVE", "SUSPENDED")},
    "tenants": {"status": ("ACTIVE", "SUSPENDED", "INACTIVE", "DELETED")},
    "invitations": {"status": ("PENDING", "ACCEPTED", "EXPIRED", "REVOKED")},
    "import_jobs": {"status": ("UPLOADED", "QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")},
    "webhook_deliveries": {"status": ("PENDING", "SUCCEEDED", "FAILED", "CANCELLED")},
}


def _name(table: str, column: str) -> str:
    return f"ck_{table}_{column}"


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _has_check(name: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.check_constraints "
        "WHERE constraint_schema = DATABASE() AND constraint_name = :n"), {"n": name}).scalar())


def _has_table(table: str) -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": table}).scalar())


def _outside(table: str, column: str, values: Sequence[str]) -> list[tuple[str, int]]:
    """(value, rows) of the values the constraint would refuse."""
    rows = op.get_bind().execute(text(
        f"SELECT {column}, COUNT(*) FROM {table} WHERE {column} IS NOT NULL "
        f"AND {column} NOT IN ({_in_list(values)}) GROUP BY {column} ORDER BY COUNT(*) DESC")).all()
    return [(str(v), int(n)) for v, n in rows]


def upgrade() -> None:
    for table, columns in CHECKS.items():
        if not _has_table(table):
            continue
        parts = []
        for column, values in columns.items():
            name = _name(table, column)
            if _has_check(name):
                continue
            clause = f"ADD CONSTRAINT {name} CHECK ({column} IN ({_in_list(values)}))"
            bad = _outside(table, column, values)
            if bad:
                logger.warning(
                    "0029: %s.%s holds values outside its vocabulary (%s); %s was not created. Fix those rows, "
                    "then run: ALTER TABLE %s %s", table, column,
                    ", ".join(f"{v!r} x{n}" for v, n in bad), name, table, clause)
                continue
            parts.append(clause)
        if parts:
            op.execute(f"ALTER TABLE {table} " + ", ".join(parts))


def downgrade() -> None:
    for table, columns in CHECKS.items():
        parts = [f"DROP CHECK {_name(table, c)}" for c in columns if _has_check(_name(table, c))]
        if parts:
            op.execute(f"ALTER TABLE {table} " + ", ".join(parts))
