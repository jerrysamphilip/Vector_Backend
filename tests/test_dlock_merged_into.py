"""D1 (live regression ev6, CM-063/068/069/080): the ``merged_into_id`` writers of merge and erasure.

``UPDATE prospects SET merged_into_id = ... WHERE merged_into_id = / IN (...)`` had no index and no tenant
filter: it scanned (and X-locked) every contact of every tenant and deadlocked (MySQL 1213) with the worker's
background-import inserts. These tests pin:

* merge and erasure find the pointing contacts on ``ix_prospects_tenant_merged`` and update them by unique
  key, tenant-filtered (EXPLAIN of the statements the API really sends);
* tenant B's rows are never touched by tenant A's merge / erasure;
* a deadlock on the first attempt re-runs the whole unit of work (merge, erase, purge) and succeeds;
* a merge in tenant A is not blocked by an open import transaction in tenant B, and an open merge/erase
  in tenant A does not block tenant B's inserts (two real connections, short lock wait timeout).
"""
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta

import pymysql
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from app.core.clock import utcnow
from tests.conftest import RUN, SessionLocal, engine, register_tenant, sql

INDEX = "ix_prospects_tenant_merged"


@pytest.fixture(scope="module")
def ws_a(client):
    t = register_tenant(client, "dlka")
    with engine.begin() as conn:  # a workspace of some size, so the plans are the ones production gets
        for _ in range(40):
            _insert_prospect(conn, t["tenant_id"])
    return t


@pytest.fixture(scope="module")
def ws_b(client):
    return register_tenant(client, "dlkb")


def _contact(client, ws, name="Dee"):
    email = f"{name.lower()}.{uuid.uuid4().hex[:8]}@dlock{RUN}.example.com"
    r = client.post("/contacts", headers=ws["headers"], json={"email": email, "first_name": name,
                                                               "last_name": "Lock"})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _point(prospect_id, target):
    """Make a contact look merged into ``target`` (crafted: the pointer of an earlier merge)."""
    sql("UPDATE prospects SET merged_into_id = :t WHERE prospect_id = :p", t=target, p=prospect_id)


def _merged_into(prospect_id):
    return sql("SELECT merged_into_id FROM prospects WHERE prospect_id = :p", p=prospect_id)


def _merge(client, ws, primary, duplicate):
    return client.post("/contacts/merge", headers=ws["headers"],
                       json={"primary_id": primary, "duplicate_ids": [duplicate]})


@contextmanager
def _captured():
    """The statements on prospects filtered by merged_into_id the app sends while the block runs:
    {"select": [(sql, params)], "update": [(sql, params)]}."""
    seen = {"select": [], "update": []}

    def grab(conn, cursor, statement, parameters, context, executemany):
        flat = " ".join(statement.split()).upper()
        if "PROSPECTS.MERGED_INTO_ID IN" not in flat and "PROSPECTS.MERGED_INTO_ID =" not in flat:
            return
        if flat.startswith("UPDATE PROSPECTS SET MERGED_INTO_ID"):
            seen["update"].append((statement, parameters))
        elif flat.startswith("SELECT PROSPECTS.PROSPECT_ID") and "FROM PROSPECTS" in flat:
            seen["select"].append((statement, parameters))

    event.listen(engine, "before_cursor_execute", grab)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", grab)


def _explain_key(statement, parameters):
    with engine.connect() as conn:
        row = conn.exec_driver_sql("EXPLAIN " + statement, parameters).mappings().first()
    return row["key"], row["type"]


# ── index + tenant filter ─────────────────────────────────────────────

UNIQUE_KEYS = ("PRIMARY", "uq_prospects_tenant_prospect")


def _assert_plans(seen):
    assert seen["select"], "no lookup of the contacts merged into the affected ones"
    assert seen["update"], "no merged_into_id update"
    for statement, params in seen["select"] + seen["update"]:
        assert "prospects.tenant_id = " in statement, statement
    for statement, params in seen["select"]:
        assert _explain_key(statement, params)[0] == INDEX, statement
    for statement, params in seen["update"]:  # by unique key: just those rows, no scan, no gap locks
        assert _explain_key(statement, params)[0] in UNIQUE_KEYS, statement


def test_merge_repoint_uses_the_index(client, ws_a):
    primary, dup, earlier = (_contact(client, ws_a, n) for n in ("Prim", "Dup", "Earlier"))
    _point(earlier, dup)
    with _captured() as seen:
        r = _merge(client, ws_a, primary, dup)
    assert r.status_code == 200, r.text
    _assert_plans(seen)


def test_erase_uses_the_index(client, ws_a):
    gone, earlier = _contact(client, ws_a, "Gone"), _contact(client, ws_a, "Earlier")
    _point(earlier, gone)
    with _captured() as seen:
        r = client.delete(f"/prospects/{gone}", headers=ws_a["headers"])
    assert r.status_code == 200, r.text
    _assert_plans(seen)


def test_nothing_pointing_sends_no_update(client, ws_a):
    gone = _contact(client, ws_a, "Gone")
    with _captured() as seen:
        assert client.delete(f"/prospects/{gone}", headers=ws_a["headers"]).status_code == 200
    assert seen["select"] and not seen["update"]


def test_tenant_a_merge_never_touches_tenant_b(client, ws_a, ws_b):
    primary, dup, earlier = (_contact(client, ws_a, n) for n in ("Prim", "Dup", "Earlier"))
    foreign = _contact(client, ws_b, "Foreign")
    _point(earlier, dup)
    _point(foreign, dup)  # cross-tenant pointer: only crafted data can hold one, never follow it
    r = _merge(client, ws_a, primary, dup)
    assert r.status_code == 200, r.text
    assert _merged_into(earlier) == primary
    assert _merged_into(foreign) == dup


def test_tenant_a_erase_never_touches_tenant_b(client, ws_a, ws_b):
    gone, earlier = _contact(client, ws_a, "Gone"), _contact(client, ws_a, "Earlier")
    foreign = _contact(client, ws_b, "Foreign")
    _point(earlier, gone)
    _point(foreign, gone)
    r = client.delete(f"/prospects/{gone}", headers=ws_a["headers"])
    assert r.status_code == 200, r.text
    assert _merged_into(earlier) is None
    assert _merged_into(foreign) == gone


# ── deadlock retry (1213 on the first attempt) ─────────────────────────

def _deadlock():
    return OperationalError("UPDATE prospects ...", {}, pymysql.err.OperationalError(
        1213, "Deadlock found when trying to get lock; try restarting transaction"))


def _fail_once_after(real, calls):
    """Run the real work, then raise a deadlock the first time: the retry must start from a clean state."""
    def wrapper(*args, **kwargs):
        calls.append(1)
        result = real(*args, **kwargs)
        if len(calls) == 1:
            raise _deadlock()
        return result
    return wrapper


def test_merge_is_retried_after_a_deadlock(client, ws_a, monkeypatch):
    from app.services import contact_write_service as module
    primary, dup = _contact(client, ws_a, "Prim"), _contact(client, ws_a, "Dup")
    calls = []
    monkeypatch.setattr(module, "merge_into", _fail_once_after(module.merge_into, calls))
    r = _merge(client, ws_a, primary, dup)
    assert r.status_code == 200, r.text
    assert len(calls) == 2
    assert r.json()["merged"] == 1
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=dup) == 0
    assert sql("SELECT COUNT(*) FROM merge_logs WHERE survivor_id = :p", p=primary) == 1
    assert sql("SELECT COUNT(*) FROM contact_activities WHERE prospect_id = :p AND subject = "
               "'Merged duplicate contacts'", p=primary) == 1


def test_erase_is_retried_after_a_deadlock(client, ws_a, monkeypatch):
    from app.services import prospect_list_service as module
    gone = _contact(client, ws_a, "Gone")
    calls = []
    monkeypatch.setattr(module, "delete_contacts", _fail_once_after(module.delete_contacts, calls))
    r = client.delete(f"/prospects/{gone}", headers=ws_a["headers"])
    assert r.status_code == 200, r.text
    assert len(calls) == 2
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=gone) == 0
    assert sql("SELECT COUNT(*) FROM audit_logs WHERE entity_id = :p AND action = 'ERASE_CONTACT'", p=gone) == 1


def test_recycle_bin_purge_is_retried_after_a_deadlock(client, ws_a, monkeypatch):
    from app.services import contact_write_service as module
    gone = _contact(client, ws_a, "Binned")
    assert client.delete(f"/contacts/{gone}", headers=ws_a["headers"]).status_code == 200
    calls = []
    monkeypatch.setattr(module, "delete_contacts", _fail_once_after(module.delete_contacts, calls))
    r = client.post("/contacts/purge", headers=ws_a["headers"], json={"prospect_ids": [gone]})
    assert r.status_code == 200, r.text
    assert r.json()["purged"] == 1 and len(calls) == 2
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=gone) == 0
    assert sql("SELECT COUNT(*) FROM audit_logs WHERE entity_id = :p AND action = 'PURGE_CONTACT'", p=gone) == 1


def test_expiry_purge_job_is_retried_after_a_deadlock(client, ws_a, monkeypatch):
    from app.services import crm
    gone = _contact(client, ws_a, "Expired")
    sql("UPDATE prospects SET deleted_at = :d WHERE prospect_id = :p",
        d=utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS + 5), p=gone)
    calls = []
    monkeypatch.setattr(crm, "delete_contacts", _fail_once_after(crm.delete_contacts, calls))
    with SessionLocal() as db:
        assert crm.purge_expired(db) >= 1
    assert len(calls) == 2
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=gone) == 0
    assert sql("SELECT COUNT(*) FROM audit_logs WHERE entity_id = :p AND action = 'PURGE_CONTACT'", p=gone) == 1


def test_other_errors_are_not_retried(client, ws_a, monkeypatch):
    from app.services import contact_write_service as module
    primary, dup = _contact(client, ws_a, "Prim"), _contact(client, ws_a, "Dup")
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise OperationalError("UPDATE ...", {}, pymysql.err.OperationalError(1054, "Unknown column"))

    monkeypatch.setattr(module, "merge_into", boom)
    r = _merge(client, ws_a, primary, dup)
    assert r.status_code == 500
    assert len(calls) == 1
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id IN (:a, :b)", a=primary, b=dup) == 2


# ── two connections: tenant A's merge vs tenant B's import inserts ───────

def _insert_prospect(conn, tenant_id):
    conn.execute(text("INSERT INTO prospects (prospect_id, tenant_id, email) VALUES (:p, :t, :e)"),
                 {"p": str(uuid.uuid4()), "t": tenant_id, "e": f"imp.{uuid.uuid4().hex[:10]}@dlock{RUN}.example.com"})


def test_merge_is_not_blocked_by_another_tenants_open_import(client, ws_a, ws_b):
    """Tenant B's import holds an uncommitted insert; tenant A's merge must not wait for it."""
    primary, dup = _contact(client, ws_a, "Prim"), _contact(client, ws_a, "Dup")
    for _ in range(3):
        _contact(client, ws_b, "Bee")
    conn = engine.connect()
    trans = conn.begin()
    _insert_prospect(conn, ws_b["tenant_id"])
    # Safety valve: if the merge waits on the import's locks, release them after 8 s instead of 50 s
    valve = threading.Timer(8.0, trans.rollback)
    valve.start()
    try:
        started = time.monotonic()
        r = _merge(client, ws_a, primary, dup)
        elapsed = time.monotonic() - started
    finally:
        valve.cancel()
        if trans.is_active:
            trans.rollback()
        conn.close()
    assert r.status_code == 200, r.text
    assert elapsed < 5.0, f"merge waited {elapsed:.1f}s on tenant B's open import"


def test_open_merge_and_erase_do_not_block_another_tenants_inserts(client, ws_a, ws_b):
    """Tenant A's merge / erase statements are open (uncommitted); tenant B's import inserts go through."""
    from app.repositories.contacts import SqlAlchemyContactRepository
    primary, dup, gone, earlier = (_contact(client, ws_a, n) for n in ("Prim", "Dup", "Gone", "Earlier"))
    _point(earlier, dup)
    _contact(client, ws_b, "Bee")
    with SessionLocal() as db:
        repo = SqlAlchemyContactRepository(db)
        repo.repoint_merged(ws_a["tenant_id"], dup, primary)
        repo.erase_rows(ws_a["tenant_id"], [gone])
        with engine.connect() as other:
            with other.begin():
                other.execute(text("SET SESSION innodb_lock_wait_timeout = 3"))
                for _ in range(5):
                    _insert_prospect(other, ws_b["tenant_id"])  # 1205 here = blocked by tenant A
                other.execute(text("UPDATE prospects SET first_name = 'Bee2' WHERE tenant_id = :t"),
                              {"t": ws_b["tenant_id"]})
        db.rollback()
