"""D1: the database is on one collation, and migration 0017 converts tables that are not.

The conversion test builds a mixed pair the way an older build on a utf8mb4_unicode_ci schema left
them (a parent and a child with a string foreign key), runs the revision's upgrade(), and checks both
are converted, the foreign key still holds and a second run changes nothing.
"""
import importlib.util
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from app.core.database import engine
from app.core.db_charset import MYSQL_COLLATION
from tests.conftest import RUN

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0017_one_collation.py"
PARENT, CHILD = f"d1_parent_{RUN}", f"d1_child_{RUN}"


def _migration():
    spec = importlib.util.spec_from_file_location("m0017", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_upgrade(mod) -> list:
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            todo = mod._tables_to_convert()
            mod.upgrade()
        conn.commit()
    return todo


def _collations(conn, table: str) -> set:
    return set(conn.execute(text(
        "SELECT collation_name FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = :t AND collation_name IS NOT NULL UNION "
        "SELECT table_collation FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name = :t"), {"t": table}).scalars())


def test_d1_database_is_on_one_collation():
    with engine.connect() as conn:
        schema = conn.execute(text("SELECT default_collation_name FROM information_schema.schemata "
                                   "WHERE schema_name = DATABASE()")).scalar()
        tables = set(conn.execute(text("SELECT table_collation FROM information_schema.tables "
                                       "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'")).scalars())
        columns = set(conn.execute(text("SELECT collation_name FROM information_schema.columns "
                                        "WHERE table_schema = DATABASE() AND collation_name IS NOT NULL")).scalars())
        session = conn.execute(text("SELECT @@collation_connection")).scalar()
    assert schema == MYSQL_COLLATION
    assert tables == {MYSQL_COLLATION}
    assert columns == {MYSQL_COLLATION}
    assert session == MYSQL_COLLATION


def test_d1_migration_converts_mixed_tables():
    mod = _migration()
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE `{PARENT}` (id VARCHAR(36) PRIMARY KEY, name VARCHAR(50)) "
                          "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        conn.execute(text(f"CREATE TABLE `{CHILD}` (id VARCHAR(36) PRIMARY KEY, parent_id VARCHAR(36), "
                          f"FOREIGN KEY (parent_id) REFERENCES `{PARENT}` (id)) "
                          "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        conn.execute(text(f"INSERT INTO `{PARENT}` VALUES ('p1', 'Zoë')"))
        conn.execute(text(f"INSERT INTO `{CHILD}` VALUES ('c1', 'p1')"))
    try:
        assert set(_run_upgrade(mod)) == {PARENT, CHILD}
        with engine.connect() as conn:
            assert _collations(conn, PARENT) == {MYSQL_COLLATION}
            assert _collations(conn, CHILD) == {MYSQL_COLLATION}
            assert conn.execute(text(f"SELECT name FROM `{PARENT}` WHERE id = 'p1'")).scalar() == "Zoë"
            assert conn.execute(text("SELECT @@foreign_key_checks")).scalar() == 1
            fks = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.referential_constraints WHERE "
                "constraint_schema = DATABASE() AND table_name = :t"), {"t": CHILD}).scalar()
            assert fks == 1
        # Idempotent: nothing left to convert
        assert _run_upgrade(mod) == []
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS `{CHILD}`"))
            conn.execute(text(f"DROP TABLE IF EXISTS `{PARENT}`"))
