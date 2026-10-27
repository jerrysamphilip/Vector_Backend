"""DATETIME(6) everywhere: every TIMESTAMP and second-precision DATETIME column becomes DATETIME(6) (chunk D2)

Revision ID: 0018_datetime6
Revises: 0017_one_collation
Create Date: 2026-10-27

TIMESTAMP ends at 2038-01-19 03:14:07 UTC (a contract end date, a retention expiry or a recycle date
can already reach past it) and converts through the session time zone on every read and write. The
models now declare every point in time as DATETIME(6) holding naive UTC (app/models/types.py) and every
connection runs with time_zone '+00:00' (app/core/database.py, alembic/env.py); this revision converts
what is already there.

TIMESTAMP -> DATETIME converts each stored value from UTC to the session time zone, so the session is
set to '+00:00' first: the values come out as the UTC they always meant, whatever the server's zone.
Each column keeps its nullability, its default and its ON UPDATE: a CURRENT_TIMESTAMP / NOW() default
becomes CURRENT_TIMESTAMP(6) (MySQL rejects a DATETIME(6) default or ON UPDATE without the matching
precision), a literal or expression default is kept as it is, as are column comments and INVISIBLE.
One ALTER TABLE per table carries all of its columns, so each table is rebuilt once; on a large
database run it in a maintenance window.

Only columns that are still TIMESTAMP, or DATETIME without precision 6, are touched, so on a database
that is already converted (every fresh build: create_all already makes DATETIME(6)) the revision changes
nothing and a second run is a no-op.

Downgrade is a no-op on purpose: a value after 2038-01-19 written since the upgrade cannot go back into
a TIMESTAMP, and the earlier revisions work on DATETIME(6) as well. Restore from a backup to go back.
"""
import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0018_datetime6"
down_revision: Union[str, Sequence[str], None] = "0017_one_collation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TARGET = "DATETIME(6)"  # Literal copies of app/models/types.py: a migration must not change later
NOW6 = "CURRENT_TIMESTAMP(6)"
UTC = "+00:00"

# How information_schema reports a "now" default: CURRENT_TIMESTAMP, CURRENT_TIMESTAMP(6), now(), ...
_NOW_DEFAULT = re.compile(r"^\(?\s*(current_timestamp|now|localtime|localtimestamp)\s*(\(\s*\d*\s*\))?\s*\)?$", re.I)


def _quote(value: str) -> str:
    """A MySQL string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _default_clause(default: Optional[str], extra: str) -> Optional[str]:
    if default is None:
        return None  # NULL default (nullable) or no default (NOT NULL): nothing to state
    if _NOW_DEFAULT.match(default.strip()):
        return f"DEFAULT {NOW6}"
    if "default_generated" in extra.lower():
        return f"DEFAULT ({default})"  # an expression default, kept as written
    return f"DEFAULT {_quote(default)}"  # a literal such as '2000-01-01 00:00:00'


def column_clause(name: str, nullable: str, default: Optional[str], extra: str, comment: str) -> str:
    """The MODIFY clause that turns one column into DATETIME(6) with the same null / default / on update."""
    lowered = extra.lower()
    if "virtual generated" in lowered or "stored generated" in lowered:
        raise RuntimeError(f"Generated time column `{name}` must be converted by hand")
    parts = [f"MODIFY COLUMN `{name}` {TARGET}", "NULL" if nullable == "YES" else "NOT NULL"]
    default_sql = _default_clause(default, extra)
    if default_sql:
        parts.append(default_sql)
    if "on update" in lowered:
        parts.append(f"ON UPDATE {NOW6}")
    if comment:
        parts.append(f"COMMENT {_quote(comment)}")
    if "invisible" in lowered:
        parts.append("INVISIBLE")
    return " ".join(parts)


def _columns_by_table() -> Dict[str, List[str]]:
    """Base-table columns still TIMESTAMP, or DATETIME without fractional precision 6, as MODIFY clauses."""
    rows = op.get_bind().execute(text(
        "SELECT c.table_name, c.column_name, c.is_nullable, c.column_default, c.extra, c.column_comment "
        "FROM information_schema.columns c JOIN information_schema.tables t "
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE' "
        "WHERE c.table_schema = DATABASE() AND (c.data_type = 'timestamp' "
        "  OR (c.data_type = 'datetime' AND COALESCE(c.datetime_precision, 0) <> 6)) "
        "ORDER BY c.table_name, c.ordinal_position")).all()
    tables: Dict[str, List[str]] = defaultdict(list)
    for table, column, nullable, default, extra, comment in rows:
        tables[str(table)].append(column_clause(str(column), str(nullable), default, str(extra or ""), str(comment or "")))
    return dict(tables)


def upgrade() -> None:
    bind = op.get_bind()
    tables = _columns_by_table()
    if not tables:
        return
    previous_zone = bind.execute(text("SELECT @@session.time_zone")).scalar()
    op.execute(f"SET SESSION time_zone = '{UTC}'")  # TIMESTAMP -> DATETIME converts through it
    try:
        for table, clauses in tables.items():
            op.execute(f"ALTER TABLE `{table}` " + ", ".join(clauses))
    finally:
        bind.execute(text("SET SESSION time_zone = :z"), {"z": previous_zone})


def downgrade() -> None:
    pass
