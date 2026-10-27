"""D1: every table declares the one charset / collation, and the DDL carries it (no database)."""
import importlib.util
from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401  (registers every table)
from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE
from app.models import Base

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0017_one_collation.py"


def test_every_table_declares_engine_charset_and_collation():
    wrong = [t.name for t in Base.metadata.tables.values()
             if (t.dialect_kwargs.get("mysql_engine"), t.dialect_kwargs.get("mysql_charset"),
                 t.dialect_kwargs.get("mysql_collate")) != (MYSQL_ENGINE, MYSQL_CHARSET, MYSQL_COLLATION)]
    assert len(Base.metadata.tables) >= 70
    assert wrong == []


def test_create_table_ddl_names_the_collation():
    # A table that never set mysql_charset itself (it used to take the schema default)
    ddl = str(CreateTable(Base.metadata.tables["magic_login_tokens"]).compile(dialect=mysql.dialect()))
    assert f"COLLATE {MYSQL_COLLATION}" in ddl and f"CHARSET={MYSQL_CHARSET}" in ddl


def test_migration_constants_match():
    spec = importlib.util.spec_from_file_location("m0017", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.CHARSET, mod.COLLATION) == (MYSQL_CHARSET, MYSQL_COLLATION)
    assert mod.down_revision == "0016_optimistic_concurrency"
