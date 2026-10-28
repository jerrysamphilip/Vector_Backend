"""Schema parity (migration 0035_schema_parity): a fresh build has the shape an upgraded database has.

* every column, key and index of the revision's lists is in its target shape in information_schema;
* a raw-SQL UPDATE refreshes updated_at on the three ON UPDATE tables (rolled back);
* the revision's upgrade() on scratch tables: NULLs set to the default before NOT NULL, a column with another
  default / a missing column skipped with a warning, ON UPDATE added keeping the DEFAULT and nullability, a
  foreign key skipped while orphans exist and created once they are gone, a legacy index dropped only when the
  index serving its FK exists, a duplicate FK dropped and its index renamed; a second run changes nothing;
* downgrade() is a no-op.
"""
import importlib.util
import logging
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from app.core.database import engine
from tests.conftest import RUN

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0035_schema_parity.py"
P = f"sp_{RUN}"  # scratch table prefix


def _migration():
    spec = importlib.util.spec_from_file_location("m0035", MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _migration()


def _col(conn, table, column):
    return conn.execute(text(
        "SELECT is_nullable AS n, column_default AS d, extra AS e FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"), {"t": table, "c": column}).first()


def _indexes(conn, table):
    return {r[0] for r in conn.execute(text(
        "SELECT DISTINCT index_name FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = :t"), {"t": table})}


def _fks(conn, table, column):
    return [r[0] for r in conn.execute(text(
        "SELECT constraint_name FROM information_schema.key_column_usage WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = :c AND referenced_table_name IS NOT NULL"), {"t": table, "c": column})]


@pytest.mark.parametrize("table,column,literal,shown", M.NOT_NULL, ids=[f"{t}.{c}" for t, c, _, _ in M.NOT_NULL])
def test_schema_column_is_not_null_with_default(table, column, literal, shown):
    with engine.connect() as conn:
        row = _col(conn, table, column)
        assert row.n == "NO" and row.d in shown


@pytest.mark.parametrize("table,column", M.ON_UPDATE, ids=[f"{t}.{c}" for t, c in M.ON_UPDATE])
def test_schema_updated_at_has_on_update(table, column):
    with engine.connect() as conn:
        assert "on update current_timestamp(6)" in _col(conn, table, column).e.lower()


@pytest.mark.parametrize("name,table,column,ref_table,ref_column", M.FOREIGN_KEYS, ids=[f[0] for f in M.FOREIGN_KEYS])
def test_schema_foreign_key_exists(name, table, column, ref_table, ref_column):
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.key_column_usage WHERE table_schema = DATABASE() "
            "AND table_name = :t AND column_name = :c AND referenced_table_name = :rt AND referenced_column_name = :rc"),
            {"t": table, "c": column, "rt": ref_table, "rc": ref_column}).scalar() == 1


def test_schema_no_legacy_indexes_or_duplicate_fk():
    with engine.connect() as conn:
        for table, index, _column, _keeper in M.LEGACY_INDEXES:
            assert index not in _indexes(conn, table), f"{table}.{index}"
        assert len(_fks(conn, "users", "invited_by")) == 1
        assert "invited_by" in _indexes(conn, "users") and "fk_users_invited_by" not in _indexes(conn, "users")


def test_schema_raw_update_refreshes_updated_at():
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text(
                "INSERT INTO imap_sync_state (inbox_id, folder, updated_at) VALUES (:i, 'INBOX', '2020-01-01')"),
                {"i": f"inbox-{RUN}"})
            conn.execute(text("UPDATE imap_sync_state SET last_uid = 5 WHERE inbox_id = :i"), {"i": f"inbox-{RUN}"})
            stamp = conn.execute(text("SELECT updated_at FROM imap_sync_state WHERE inbox_id = :i"),
                                 {"i": f"inbox-{RUN}"}).scalar()
            assert stamp.year > 2020
        finally:
            tx.rollback()


def _run(mod) -> None:
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
        conn.commit()


SCRATCH_DDL = (
    f"CREATE TABLE {P}_parent (id VARCHAR(36) PRIMARY KEY)",
    f"CREATE TABLE {P}_t (id INT PRIMARY KEY, flag TINYINT(1) NULL DEFAULT 0, "
    f"kind VARCHAR(12) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT 'PENDING' COMMENT 'the kind', "
    f"other INT NULL DEFAULT 7, updated_at DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6), "
    f"parent_id VARCHAR(36) NULL, lead_id VARCHAR(36) NULL, sent_at DATETIME NULL, "
    f"KEY parent_id (parent_id), KEY ix_parent_sent (parent_id, sent_at), KEY lead_id (lead_id), "
    f"KEY fk_dup (lead_id), CONSTRAINT fk_dup FOREIGN KEY (lead_id) REFERENCES {P}_parent (id), "
    f"CONSTRAINT {P}_ibfk_2 FOREIGN KEY (lead_id) REFERENCES {P}_parent (id))",
)


def _configure(mod) -> None:
    t = f"{P}_t"
    mod.NOT_NULL = ((t, "flag", "0", ("0",)), (t, "kind", "'PENDING'", ("PENDING",)), (t, "other", "0", ("0",)),
                    (t, "nope", "0", ("0",)))
    mod.ON_UPDATE = ((t, "updated_at"), (t, "sent_at"))
    mod.FOREIGN_KEYS = ((f"fk_{P}_parent", t, "parent_id", f"{P}_parent", "id"),)
    mod.LEGACY_INDEXES = ((t, "parent_id", "parent_id", "ix_parent_sent"), (t, "lead_id", "lead_id", "ix_missing"))
    mod.DUPLICATE_FKS = ((t, "fk_dup", "lead_id", "lead_id_ix"),)


def _check_first_run(conn, messages: str) -> None:
    t = f"{P}_t"
    assert tuple(_col(conn, t, "flag"))[:2] == ("NO", "0")
    assert tuple(_col(conn, t, "kind"))[:2] == ("NO", "PENDING")
    assert tuple(_col(conn, t, "other"))[:2] == ("YES", "7")  # other default: left NULLable
    assert conn.execute(text(f"SELECT flag, kind FROM {t} WHERE id = 1")).one() == (0, "PENDING")  # NULLs filled
    assert conn.execute(text(f"SELECT flag, kind FROM {t} WHERE id = 2")).one() == (1, "X")       # kept
    assert conn.execute(text(
        "SELECT column_comment, collation_name FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = 'kind'"), {"t": t}).one() == ("the kind", "utf8mb4_0900_ai_ci")
    upd = _col(conn, t, "updated_at")
    assert upd.n == "YES" and upd.d == "CURRENT_TIMESTAMP(6)" and "on update current_timestamp(6)" in upd.e.lower()
    assert "on update" not in (_col(conn, t, "sent_at").e or "").lower()  # no CURRENT_TIMESTAMP default
    assert _fks(conn, t, "parent_id") == []  # orphan row 2 blocks the key
    assert "parent_id" not in _indexes(conn, t) and "lead_id" in _indexes(conn, t)
    assert _fks(conn, t, "lead_id") == [f"{P}_ibfk_2"]
    assert "lead_id_ix" in _indexes(conn, t) and "fk_dup" not in _indexes(conn, t)
    assert f"{t}.other has DEFAULT '7'" in messages and f"{t}.nope does not exist" in messages
    assert f"{t}.sent_at is datetime DEFAULT None; ON UPDATE not set" in messages
    assert f"1 row(s) of {t} have a parent_id that is not in {P}_parent; fk_{P}_parent was not created" in messages
    assert f"{t}.lead_id is kept: ix_missing" in messages


@pytest.fixture()
def scratch():
    with engine.begin() as conn:
        for ddl in SCRATCH_DDL:
            conn.execute(text(ddl))
        conn.execute(text(f"INSERT INTO {P}_parent (id) VALUES ('p1')"))
        conn.execute(text(f"INSERT INTO {P}_t (id, flag, kind, other, parent_id) VALUES "
                          f"(1, NULL, NULL, NULL, 'p1'), (2, 1, 'X', 3, 'gone')"))
    yield
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {P}_t"))
        conn.execute(text(f"DROP TABLE IF EXISTS {P}_parent"))


def test_schema_upgrade_on_scratch_tables(scratch, caplog):
    mod = _migration()
    _configure(mod)
    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        _run(mod)
    with engine.connect() as conn:
        _check_first_run(conn, " | ".join(r.getMessage() for r in caplog.records))
    with engine.begin() as conn:  # repair the orphan: the key is created on the next run
        conn.execute(text(f"UPDATE {P}_t SET parent_id = NULL WHERE id = 2"))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        _run(mod)
    with engine.connect() as conn:
        assert _fks(conn, f"{P}_t", "parent_id") == [f"fk_{P}_parent"]
    assert not any("NULL row(s) set" in r.getMessage() for r in caplog.records)
    caplog.clear()
    _run(mod)  # third run: nothing left to do, no error
    with engine.connect() as conn:
        _check_shape_stable(conn)


def _check_shape_stable(conn) -> None:
    t = f"{P}_t"
    assert tuple(_col(conn, t, "flag"))[:2] == ("NO", "0")
    assert _fks(conn, t, "parent_id") == [f"fk_{P}_parent"] and _fks(conn, t, "lead_id") == [f"{P}_ibfk_2"]


def test_schema_downgrade_is_a_noop():
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            M.downgrade()
        assert _col(conn, "campaigns", "auto_paused").n == "NO"
