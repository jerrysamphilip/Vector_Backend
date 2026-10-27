"""D2: every point-in-time column is DATETIME(6) with CURRENT_TIMESTAMP(6) defaults (no database)."""
import importlib.util
import re
from pathlib import Path

from sqlalchemy import TIMESTAMP, Date, DateTime, Time
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401  (registers every table)
from app.core.database import MYSQL_INIT_COMMAND, MYSQL_TIME_ZONE
from app.models import Base
from app.models.types import NOW6, UtcDateTime

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic" / "versions" / "0018_datetime6.py"
DIALECT = mysql.dialect()


def _migration():
    spec = importlib.util.spec_from_file_location("m0018", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _time_columns():
    return [c for t in Base.metadata.tables.values() for c in t.columns
            if isinstance(c.type, DateTime) and not isinstance(c.type, (Date, Time))]


def _compiled(column) -> str:
    return column.type.compile(dialect=DIALECT)


def test_no_column_is_timestamp():
    wrong = [f"{c.table.name}.{c.name}" for t in Base.metadata.tables.values() for c in t.columns
             if isinstance(c.type, TIMESTAMP) or "TIMESTAMP" in _compiled(c)]
    assert wrong == []


def test_every_time_column_compiles_to_datetime6():
    columns = _time_columns()
    assert len(columns) >= 150
    wrong = [f"{c.table.name}.{c.name}: {_compiled(c)}" for c in columns if _compiled(c) != "DATETIME(6)"]
    assert wrong == []


def test_server_defaults_and_on_update_use_current_timestamp6():
    wrong = []
    for c in _time_columns():
        if c.server_default is not None and str(c.server_default.arg) != "CURRENT_TIMESTAMP(6)":
            wrong.append(f"{c.table.name}.{c.name} default {c.server_default.arg}")
        if c.onupdate is not None and str(getattr(c.onupdate, "arg", "")) != "CURRENT_TIMESTAMP(6)":
            wrong.append(f"{c.table.name}.{c.name} onupdate {c.onupdate}")
    assert wrong == []
    assert sum(1 for c in _time_columns() if c.server_default is not None) >= 90


def test_create_table_ddl():
    ddl = str(CreateTable(Base.metadata.tables["tenants"]).compile(dialect=DIALECT))
    assert "created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)" in ddl
    assert "TIMESTAMP " not in ddl.replace("CURRENT_TIMESTAMP", "")


def test_dates_stay_dates():
    dates = [c for t in Base.metadata.tables.values() for c in t.columns if isinstance(c.type, Date)]
    assert dates and all(_compiled(c) == "DATE" for c in dates)


def test_shared_type_and_session_zone():
    assert UtcDateTime.compile(dialect=DIALECT) == "DATETIME(6)"
    assert str(NOW6) == "CURRENT_TIMESTAMP(6)"
    assert MYSQL_TIME_ZONE == "+00:00"
    assert MYSQL_INIT_COMMAND == "SET time_zone = '+00:00'"


def test_schema_helpers_emit_no_timestamp_ddl():
    sources = sorted((ROOT / "app" / "db").glob("*_schema.py")) + [ROOT / "app" / "db" / "schema_patches.py"]
    hits = [f"{p.name}: {line.strip()}" for p in sources for line in p.read_text().splitlines()
            if re.search(r"\bTIMESTAMP\b(?!\()", line.replace("CURRENT_TIMESTAMP", "").replace("UTC_TIMESTAMP", ""))]
    assert hits == []


def test_migration_revision_and_constants():
    mod = _migration()
    assert mod.revision == "0018_datetime6"
    assert mod.down_revision == "0017_one_collation"
    assert (mod.TARGET, mod.NOW6, mod.UTC) == ("DATETIME(6)", "CURRENT_TIMESTAMP(6)", MYSQL_TIME_ZONE)


def test_migration_column_clause_keeps_null_default_and_on_update():
    clause = _migration().column_clause
    assert clause("a", "YES", None, "", "") == "MODIFY COLUMN `a` DATETIME(6) NULL"
    assert clause("a", "NO", None, "", "") == "MODIFY COLUMN `a` DATETIME(6) NOT NULL"
    for now in ("now()", "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP(6)", "current_timestamp()"):
        assert clause("a", "YES", now, "DEFAULT_GENERATED", "") == \
            "MODIFY COLUMN `a` DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6)"
    assert clause("u", "NO", "CURRENT_TIMESTAMP", "DEFAULT_GENERATED on update CURRENT_TIMESTAMP", "") == \
        "MODIFY COLUMN `u` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"
    assert clause("a", "NO", "2000-01-01 00:00:00", "", "it's") == \
        "MODIFY COLUMN `a` DATETIME(6) NOT NULL DEFAULT '2000-01-01 00:00:00' COMMENT 'it''s'"
