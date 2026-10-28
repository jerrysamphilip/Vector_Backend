"""Server-side DEFAULTs (migration 0032_server_defaults): a freshly built database has the same column
DEFAULTs as one upgraded through the old migrations, so raw-SQL inserts that leave those columns out work.

* every column of the revision's list carries its DEFAULT in information_schema;
* minimal raw INSERTs (users, webhook_endpoints, webhook_deliveries, forecast_snapshots) succeed and the
  omitted columns get the defaults (rolled back afterwards);
* the revision's upgrade() sets a missing default, leaves a different existing default alone and skips a
  missing table/column, each with the expected warning, and a second run changes nothing (scratch table).
"""
import importlib.util
import logging
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from app.core.database import engine
from tests.conftest import RUN

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0032_server_defaults.py"
SCRATCH = f"sdef_scratch_{RUN}"


def _migration():
    spec = importlib.util.spec_from_file_location("m0032", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default(conn, table: str, column: str):
    return conn.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": table, "c": column}).scalar()


@pytest.mark.parametrize("table,column,literal,shown", _migration().DEFAULTS,
                         ids=[f"{t}.{c}" for t, c, _, _ in _migration().DEFAULTS])
def test_sdef_column_has_server_default(table, column, literal, shown):
    with engine.connect() as conn:
        assert _default(conn, table, column) in shown


def test_sdef_raw_inserts_get_the_defaults(tenant_a):
    tenant = tenant_a["tenant_id"]
    uid, eid, did, sid = (str(uuid.uuid4()) for _ in range(4))
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text(
                "INSERT INTO users (user_id, tenant_id, first_name, last_name, email, role) "
                "VALUES (:u, :t, 'Raw', 'Insert', :e, 'SALES_REP')"), {"u": uid, "t": tenant, "e": f"raw-{uid}@sdef.io"})
            conn.execute(text(
                "INSERT INTO webhook_endpoints (endpoint_id, tenant_id, url, secret, event_types) "
                "VALUES (:i, :t, 'https://hooks.example.com/x', 'not-a-real-secret', '[]')"), {"i": eid, "t": tenant})
            conn.execute(text(
                "INSERT INTO webhook_deliveries (delivery_id, tenant_id, endpoint_id, event_id, event_type, payload) "
                "VALUES (:d, :t, :e, :d, 'contact.created', '{}')"), {"d": did, "t": tenant, "e": eid})
            conn.execute(text(
                "INSERT INTO forecast_snapshots (snapshot_id, tenant_id, owner_id, period_type, period_start, "
                "period_end) VALUES (:s, :t, :o, 'MONTH', '2026-10-01', '2026-10-31')"),
                {"s": sid, "t": tenant, "o": uid})  # the raw user: no snapshot of theirs can exist yet
            assert conn.execute(text("SELECT notify_email FROM users WHERE user_id = :u"), {"u": uid}).scalar() == 1
            assert tuple(conn.execute(text(
                "SELECT active, consecutive_failures FROM webhook_endpoints WHERE endpoint_id = :i"),
                {"i": eid}).one()) == (1, 0)
            assert tuple(conn.execute(text(
                "SELECT status, attempts, is_test FROM webhook_deliveries WHERE delivery_id = :d"),
                {"d": did}).one()) == ("PENDING", 0, 0)
            row = conn.execute(text(
                "SELECT won, weighted, forecast, open_count, source FROM forecast_snapshots WHERE snapshot_id = :s"),
                {"s": sid}).one()
            assert (float(row[0]), float(row[1]), float(row[2]), row[3], row[4]) == (0, 0, 0, 0, "SCHEDULED")
        finally:
            tx.rollback()


def _run_upgrade(mod) -> None:
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
        conn.commit()


def test_sdef_upgrade_sets_only_missing_defaults(caplog):
    mod = _migration()
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE `{SCRATCH}` (id INT PRIMARY KEY, flag TINYINT(1) NOT NULL, "
                          "kind VARCHAR(12) NULL, other INT NOT NULL DEFAULT 7)"))
        conn.execute(text(f"INSERT INTO `{SCRATCH}` (id, flag, kind) VALUES (1, 1, 'X')"))
    mod.DEFAULTS = (
        (SCRATCH, "flag", "0", ("0",)),
        (SCRATCH, "kind", "'PENDING'", ("PENDING",)),
        (SCRATCH, "other", "0", ("0",)),            # has a different default: left alone
        (SCRATCH, "nope", "0", ("0",)),             # missing column: skipped
        (f"{SCRATCH}_missing", "flag", "0", ("0",)),  # missing table: skipped
    )
    try:
        with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
            _run_upgrade(mod)
        with engine.connect() as conn:
            assert _default(conn, SCRATCH, "flag") == "0"
            assert _default(conn, SCRATCH, "kind") == "PENDING"
            assert _default(conn, SCRATCH, "other") == "7"
            assert tuple(conn.execute(text(f"SELECT flag, kind, other FROM `{SCRATCH}` WHERE id = 1")).one()) \
                == (1, "X", 7)  # existing rows untouched
            conn.execute(text(f"INSERT INTO `{SCRATCH}` (id) VALUES (2)"))
            assert tuple(conn.execute(text(f"SELECT flag, kind, other FROM `{SCRATCH}` WHERE id = 2")).one()) \
                == (0, "PENDING", 7)
            conn.commit()
        messages = " | ".join(r.getMessage() for r in caplog.records)
        assert f"{SCRATCH}.other already has DEFAULT '7'" in messages
        assert f"{SCRATCH}.nope does not exist" in messages
        assert f"{SCRATCH}_missing.flag does not exist" in messages
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
            _run_upgrade(mod)  # second run: nothing left to set, same skips only
        assert not any("flag already" in r.getMessage() or "kind already" in r.getMessage() for r in caplog.records)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS `{SCRATCH}`"))


def test_sdef_downgrade_is_a_noop():
    mod = _migration()
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.downgrade()
        assert _default(conn, "users", "notify_email") == "1"
