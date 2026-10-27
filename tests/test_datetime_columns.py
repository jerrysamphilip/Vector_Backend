"""D2: every time column is DATETIME(6) in UTC, and migration 0018 converts TIMESTAMP columns that are not.

The conversion test builds a pair of tables the way older builds left them (TIMESTAMP with
DEFAULT CURRENT_TIMESTAMP, ON UPDATE, nullable, TIMESTAMP(6), a second-precision DATETIME), runs the
revision's upgrade() from a session on another time zone, and checks the columns are DATETIME(6), the
values, defaults and ON UPDATE survive, the session zone is restored and a second run changes nothing.
"""
import importlib.util
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from app.core.clock import utcnow
from app.core.database import engine
from app.models.tenant import Tenant
from tests.conftest import RUN

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0018_datetime6.py"
FIRST, SECOND = f"d2_first_{RUN}", f"d2_second_{RUN}"


def _migration():
    spec = importlib.util.spec_from_file_location("m0018", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_upgrade(mod, session_zone: str) -> tuple:
    """upgrade() on a connection whose session zone is session_zone; returns (tables it converted, zone after)."""
    with engine.connect() as conn:
        conn.execute(text("SET SESSION time_zone = :z"), {"z": session_zone})
        with Operations.context(MigrationContext.configure(conn)):
            todo = set(mod._columns_by_table())
            mod.upgrade()
        zone_after = conn.execute(text("SELECT @@session.time_zone")).scalar()
        conn.commit()
        conn.execute(text("SET SESSION time_zone = '+00:00'"))  # back to the pool as it came
    return todo, zone_after


def _columns(conn, table: str) -> dict:
    rows = conn.execute(text(
        "SELECT column_name, column_type, is_nullable, column_default, extra FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t"), {"t": table}).all()
    return {r[0]: tuple(r[1:]) for r in rows}


def test_d2_database_has_no_timestamp_columns():
    with engine.connect() as conn:
        wrong = conn.execute(text(
            "SELECT CONCAT(table_name, '.', column_name, ' ', column_type) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND (data_type = 'timestamp' "
            "OR (data_type = 'datetime' AND datetime_precision <> 6))")).scalars().all()
        count = conn.execute(text("SELECT COUNT(*) FROM information_schema.columns WHERE "
                                  "table_schema = DATABASE() AND column_type = 'datetime(6)'")).scalar()
    assert wrong == []
    assert count >= 150


def test_d2_session_time_zone_is_utc():
    with engine.connect() as conn:
        assert conn.execute(text("SELECT @@session.time_zone")).scalar() == "+00:00"


def test_d2_far_future_and_microseconds_round_trip():
    tenant_id = str(uuid.uuid4())
    far = datetime(2100, 1, 2, 3, 4, 5, 123456)
    table = Tenant.__table__
    try:
        with engine.begin() as conn:
            conn.execute(table.insert().values(tenant_id=tenant_id, tenant_name=f"D2 {RUN}", created_at=far))
        with engine.connect() as conn:
            row = conn.execute(table.select().where(table.c.tenant_id == tenant_id)).mappings().one()
        assert row["created_at"] == far
        # The CURRENT_TIMESTAMP(6) server default is UTC, the same clock as app.core.clock.utcnow()
        assert abs(row["updated_at"] - utcnow()) < timedelta(minutes=1)
    finally:
        with engine.begin() as conn:
            conn.execute(table.delete().where(table.c.tenant_id == tenant_id))


def test_d2_migration_converts_timestamp_tables():
    mod = _migration()
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE `{FIRST}` (id INT PRIMARY KEY, note VARCHAR(10), "
            " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
            " updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
            " seen_at TIMESTAMP NULL COMMENT 'last seen',"
            " legacy_at DATETIME NULL)"))
        conn.execute(text(
            f"CREATE TABLE `{SECOND}` (id INT PRIMARY KEY, changed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
            " on_day DATE NULL)"))
        conn.execute(text(f"INSERT INTO `{FIRST}` VALUES (1, 'a', '2030-01-02 03:04:05', '2031-02-03 04:05:06', "
                          "NULL, '2099-12-31 23:59:59')"))
        conn.execute(text(f"INSERT INTO `{SECOND}` VALUES (1, '2025-06-07 08:09:10.654321', '2025-06-07')"))
    try:
        todo, zone_after = _run_upgrade(mod, "+05:00")
        assert {FIRST, SECOND} <= todo
        assert zone_after == "+05:00"
        with engine.connect() as conn:
            first, second = _columns(conn, FIRST), _columns(conn, SECOND)
            assert first["created_at"] == ("datetime(6)", "YES", "CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED")
            assert first["updated_at"] == ("datetime(6)", "NO", "CURRENT_TIMESTAMP(6)",
                                           "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)")
            assert first["seen_at"] == ("datetime(6)", "YES", None, "")
            assert first["legacy_at"] == ("datetime(6)", "YES", None, "")
            assert second["changed_at"] == ("datetime(6)", "NO", "CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED")
            assert second["on_day"][0] == "date"
            comment = conn.execute(text("SELECT column_comment FROM information_schema.columns WHERE table_schema "
                                        "= DATABASE() AND table_name = :t AND column_name = 'seen_at'"),
                                   {"t": FIRST}).scalar()
            assert comment == "last seen"
            # Values are the UTC they were stored as, although the migrating session was on +05:00
            row = conn.execute(text(f"SELECT created_at, updated_at, seen_at, legacy_at FROM `{FIRST}`")).one()
            assert tuple(row) == (datetime(2030, 1, 2, 3, 4, 5), datetime(2031, 2, 3, 4, 5, 6), None,
                                  datetime(2099, 12, 31, 23, 59, 59))
            assert conn.execute(text(f"SELECT changed_at FROM `{SECOND}`")).scalar() == \
                datetime(2025, 6, 7, 8, 9, 10, 654321)
        # ON UPDATE still fires, now at microsecond precision and in UTC
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE `{FIRST}` SET note = 'b' WHERE id = 1"))
            conn.execute(text(f"INSERT INTO `{FIRST}` (id) VALUES (2)"))
        with engine.connect() as conn:
            updated = conn.execute(text(f"SELECT updated_at FROM `{FIRST}` WHERE id = 1")).scalar()
            created = conn.execute(text(f"SELECT created_at FROM `{FIRST}` WHERE id = 2")).scalar()
        assert abs(updated - utcnow()) < timedelta(minutes=1)
        assert abs(created - utcnow()) < timedelta(minutes=1)
        # Idempotent: nothing left to convert
        todo, _ = _run_upgrade(mod, "+00:00")
        assert not todo & {FIRST, SECOND}
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS `{FIRST}`"))
            conn.execute(text(f"DROP TABLE IF EXISTS `{SECOND}`"))
