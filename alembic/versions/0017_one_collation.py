"""One collation: the schema, every table and every text column on utf8mb4 / utf8mb4_0900_ai_ci (chunk D1)

Revision ID: 0017_one_collation
Revises: 0016_optimistic_concurrency
Create Date: 2026-10-27

A database whose schema default was not utf8mb4_0900_ai_ci (an RDS parameter group or an older build
created it as utf8mb4_unicode_ci) ended up with mixed collations: tables whose model set
mysql_charset="utf8mb4" took utf8mb4's default, the others the schema's. String foreign keys and joins
across the two fail (MySQL 3780 / 1267). The models now set the collation on every table
(app/core/db_charset.py); this revision converts what is already there.

Only tables that are not already uniform are converted, so on a database that already is (every fresh
build on MySQL 8.4) the revision changes nothing. ``CONVERT TO`` rebuilds the table; on a large
database run it in a maintenance window. Foreign-key checks are off for the session while the tables
are converted one by one, because a referencing column and its parent briefly differ.

Downgrade is a no-op: no earlier revision depends on another collation, and the previous mix is not
worth restoring.
"""
from typing import List, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0017_one_collation"
down_revision: Union[str, Sequence[str], None] = "0016_optimistic_concurrency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHARSET = "utf8mb4"  # Literal copies of app/core/db_charset.py: a migration must not change later
COLLATION = "utf8mb4_0900_ai_ci"


def _tables_to_convert() -> List[str]:
    """Base tables whose own collation, or any of whose columns' collation, is not COLLATION."""
    rows = op.get_bind().execute(text(
        "SELECT t.table_name FROM information_schema.tables t "
        "WHERE t.table_schema = DATABASE() AND t.table_type = 'BASE TABLE' AND ("
        "  t.table_collation <> :c OR EXISTS ("
        "    SELECT 1 FROM information_schema.columns k WHERE k.table_schema = t.table_schema "
        "    AND k.table_name = t.table_name AND k.collation_name IS NOT NULL AND k.collation_name <> :c)) "
        "ORDER BY t.table_name"), {"c": COLLATION}).scalars().all()
    return [str(r) for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    schema_collation = bind.execute(text(
        "SELECT default_collation_name FROM information_schema.schemata WHERE schema_name = DATABASE()")).scalar()
    if schema_collation != COLLATION:
        op.execute(f"ALTER DATABASE CHARACTER SET {CHARSET} COLLATE {COLLATION}")
    tables = _tables_to_convert()
    if not tables:
        return
    op.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
    try:
        for table in tables:
            op.execute(f"ALTER TABLE `{table}` CONVERT TO CHARACTER SET {CHARSET} COLLATE {COLLATION}")
    finally:
        op.execute("SET SESSION FOREIGN_KEY_CHECKS = 1")


def downgrade() -> None:
    pass
