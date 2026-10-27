"""One live account per email across every workspace: users.active_email + UNIQUE index (F017)

Revision ID: 0019_unique_active_email
Revises: 0018_datetime6
Create Date: 2026-10-27

Only (tenant_id, email) was unique, so an invite, an accepted invitation or a reactivation could
leave the same address ACTIVE (or INVITED) in two workspaces, and sign-in / forgot-password / Google
sign-in then picked one of them arbitrarily. The models now declare ``users.active_email``, a STORED
generated column holding the email while the account is ACTIVE or INVITED and NULL otherwise, with
the UNIQUE index ``uq_users_active_email`` (NULLs do not collide, so any number of deactivated
accounts may share an address). PLATFORM_ADMIN accounts (tenant_id NULL) are covered like the others.
The application checks first and answers 409 (app/services/user_lookup.py); the index is the backstop.

Idempotent: the column and the index are added only when missing (information_schema), so on a fresh
build (create_all already makes both) the revision changes nothing. Adding a STORED column rebuilds
the table; on a large database run it in a maintenance window.

Existing duplicates do not fail the upgrade: when two live accounts already share an address the
column is still added but the index is NOT created, and a warning gives the counts. Resolve them
(deactivate or rename the extra accounts), then run ``alembic downgrade 0018_datetime6 && alembic
upgrade head`` (or create the index by hand) to add it. Ops query listing them:

    SELECT active_email, COUNT(*) AS accounts, GROUP_CONCAT(user_id, ':', IFNULL(tenant_id, 'platform'),
           ':', status ORDER BY created_at) AS which
    FROM users WHERE active_email IS NOT NULL
    GROUP BY active_email HAVING COUNT(*) > 1;

Downgrade drops the index and the column.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0019_unique_active_email"
down_revision: Union[str, Sequence[str], None] = "0018_datetime6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "users"
COLUMN = "active_email"
INDEX = "uq_users_active_email"
# Literal copy of app/models/user.py ACTIVE_EMAIL_EXPRESSION: a migration must not change later
EXPRESSION = "CASE WHEN status IN ('ACTIVE','INVITED') THEN email ELSE NULL END"


def _has_column() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": TABLE, "c": COLUMN}).scalar())


def _has_index() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": TABLE, "i": INDEX}).scalar())


def _duplicates() -> tuple[int, int]:
    """(addresses with more than one live account, live accounts involved)."""
    row = op.get_bind().execute(text(
        f"SELECT COUNT(*), COALESCE(SUM(n), 0) FROM (SELECT COUNT(*) AS n FROM {TABLE} "
        f"WHERE {COLUMN} IS NOT NULL GROUP BY {COLUMN} HAVING COUNT(*) > 1) d")).one()
    return int(row[0] or 0), int(row[1] or 0)


def upgrade() -> None:
    if not _has_column():
        op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(255) "
                   f"GENERATED ALWAYS AS ({EXPRESSION}) STORED")
    if _has_index():
        return
    addresses, accounts = _duplicates()
    if addresses:
        logger.warning(
            "0019_unique_active_email: %d email address(es) have more than one ACTIVE/INVITED account "
            "(%d accounts in total). The unique index %s was NOT created; sign-in picks the ACTIVE, then "
            "oldest, account meanwhile. Resolve the duplicates (query in this revision's docstring), then "
            "re-run the revision.", addresses, accounts, INDEX)
        return
    op.execute(f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} ({COLUMN})")


def downgrade() -> None:
    if _has_index():
        op.execute(f"DROP INDEX {INDEX} ON {TABLE}")
    if _has_column():
        op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
