"""
a1b functional (MySQL): contact identity features -- stored duplicate pairs with scan / dismiss / reopen
(C-08, C-09, F-21), merge preview, locks, merge log and merge history (C-10, C-19), e-mail history so old
and merged addresses resolve to the current contact (C-11), and the export log with the export cap (C-07).

The new routers (duplicates, exports) are mounted next to the app's own routes in a test app, so the suite
runs whether or not app.main registers them yet.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.errors import ConflictError, register_domain_error_handler
from app.main import app as main_app
from app.models.prospect import Prospect
from app.repositories.contacts import SqlAlchemyContactRepository, SqlAlchemyContactsUnitOfWork
from app.repositories.duplicates import SqlAlchemyDuplicateRepository
from app.routers import duplicates_router, exports_router
from app.services import contact_query_service, duplicate_scan
from app.services.contact_service import merge_into
from tests.conftest import RUN, register_tenant, sql


@pytest.fixture(scope="module")
def api():
    test_app = FastAPI()
    register_domain_error_handler(test_app)
    test_app.include_router(main_app.router)
    test_app.include_router(duplicates_router.router)
    test_app.include_router(exports_router.router)
    return TestClient(test_app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "a1bi")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "a1bo")


def new_contact(api, ws, email, **kw):
    r = api.post("/contacts", headers=ws["headers"], json={"email": email, **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def test_email_change_retires_old_address_and_create_resolves_it(api, ws):
    h = ws["headers"]
    pid = new_contact(api, ws, f"old.{RUN}@acme-a1b.com", first_name="Old", last_name="Mail")
    r = api.patch(f"/contacts/{pid}", headers=h, json={"email": f"new.{RUN}@acme-a1b.com"})
    assert r.status_code == 200, r.text
    hist = api.get(f"/contacts/{pid}/emails", headers=h).json()
    assert hist["current"] == f"new.{RUN}@acme-a1b.com"
    assert [(i["email"], i["status"], i["source"]) for i in hist["items"]] == [
        (f"old.{RUN}@acme-a1b.com", "RETIRED", "EMAIL_CHANGE")]
    # Creating a contact with the retired address finds the existing person (409, with its id)
    r = api.post("/contacts", headers=h, json={"email": f"OLD.{RUN}@acme-a1b.com"})
    assert r.status_code == 409 and r.json()["detail"]["prospect_id"] == pid
    with SessionLocal() as db:
        repo = SqlAlchemyContactRepository(db)
        assert repo.resolve_contact_by_email(ws["tenant_id"], f" old.{RUN}@ACME-a1b.com ").prospect_id == pid
        assert repo.resolve_contact_by_email(ws["tenant_id"], f"nobody.{RUN}@acme-a1b.com") is None
    # Changing back makes the address ACTIVE again and retires the second one
    assert api.patch(f"/contacts/{pid}", headers=h, json={"email": f"old.{RUN}@acme-a1b.com"}).status_code == 200
    rows = {i["email"]: i["status"] for i in api.get(f"/contacts/{pid}/emails", headers=h).json()["items"]}
    assert rows == {f"new.{RUN}@acme-a1b.com": "RETIRED"}
    assert sql("SELECT status FROM contact_identifiers WHERE email = :e", e=f"old.{RUN}@acme-a1b.com") == "ACTIVE"


def test_scan_dismiss_reopen_and_grouped_get(api, ws, other):
    h = ws["headers"]
    a = new_contact(api, ws, f"bob.{RUN}@dupe-a1b.com", first_name="Bob", last_name="Smith")
    b = new_contact(api, ws, f"rsmith.{RUN}@dupe-a1b.com", first_name="Robert", last_name="Smith")
    c = new_contact(api, ws, f"Jane.Doe.{RUN}+news@gmail.com", first_name="Jane", last_name="Doe")
    d = new_contact(api, ws, f"janedoe{RUN}@googlemail.com", first_name="J", last_name="D")
    stats = api.post("/contacts/duplicates/scan", headers=h).json()
    assert stats["pairs_found"] >= 2 and stats["new"] >= 2

    groups = api.get("/contacts/duplicates", headers=h).json()
    by_ids = {frozenset(x["prospect_id"] for x in g["contacts"]): g for g in groups["groups"]}
    smith = by_ids[frozenset({a, b})]
    assert smith["confidence"] == "HIGH" and smith["reasons"][0].startswith("Similar name at the same")
    assert by_ids[frozenset({c, d})]["score"] == 100 and "mergeable_properties" in groups

    pair_id = smith["pair_id"]
    assert api.post(f"/contacts/duplicates/{pair_id}/dismiss", headers=h).json()["status"] == "DISMISSED"
    api.post("/contacts/duplicates/scan", headers=h)  # a rescan never brings a dismissed pair back
    open_ids = {g["pair_id"] for g in api.get("/contacts/duplicates/pairs", headers=h).json()["items"]}
    dismissed = api.get("/contacts/duplicates/pairs", headers=h, params={"status": "DISMISSED"}).json()["items"]
    assert pair_id not in open_ids and [g["pair_id"] for g in dismissed] == [pair_id]
    assert dismissed[0]["resolved_by_name"]
    assert api.post(f"/contacts/duplicates/{pair_id}/reopen", headers=h).json()["status"] == "OPEN"
    # Other workspaces see nothing of it
    assert api.post(f"/contacts/duplicates/{pair_id}/dismiss", headers=other["headers"]).status_code == 404
    assert api.get("/contacts/duplicates/pairs", headers=other["headers"]).json()["total"] == 0


def _assert_preview(api, h, keep, dup):
    preview = api.post("/contacts/merge/preview", headers=h, json={"primary_id": keep, "duplicate_ids": [dup]})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    props = {p["field"]: p for p in body["properties"]}
    assert props["first_name"]["differs"] and props["first_name"]["default"] == keep
    assert props["designation"]["default"] == dup and not props["designation"]["differs"]
    assert props["owner_id"]["values"][0]["display"]  # the owner's name, not the id
    assert body["moves"]["activities"] >= 0 and body["tags"] == ["vip"] and body["warnings"]
    assert body["survivor"]["prospect_id"] == keep and body["merging"][0]["prospect_id"] == dup


def _assert_merge_log(api, h, keep, dup):
    history = api.get(f"/contacts/{keep}/merges", headers=h).json()["items"]
    assert len(history) == 1
    log = history[0]
    assert log["merged_ids"] == [dup] and log["choices"] == {"first_name": dup} and log["merged_by_name"]
    assert log["snapshot"]["merged"][0]["designation"] == "CTO" and log["snapshot"]["survivor"]["first_name"] == "Ann"
    assert log["merged"][0]["email"] == f"a.lee.{RUN}@merge-a1b.com"


def _assert_identities(api, h, other, keep, dup):
    emails = api.get(f"/contacts/{keep}/emails", headers=h).json()["items"]
    assert [(e["email"], e["status"], e["original_prospect_id"]) for e in emails] == [
        (f"a.lee.{RUN}@merge-a1b.com", "MERGED", dup)]
    r = api.post("/contacts", headers=h, json={"email": f"a.lee.{RUN}@merge-a1b.com"})
    assert r.status_code == 409 and r.json()["detail"]["prospect_id"] == keep
    assert sql("SELECT COUNT(*) FROM duplicate_pairs WHERE status = 'OPEN' AND (contact_a_id = :d OR contact_b_id = :d)",
               d=dup) == 0
    assert api.get(f"/contacts/{keep}/merges", headers=other["headers"]).status_code == 404
    assert api.get(f"/contacts/{keep}/emails", headers=other["headers"]).status_code == 404


def test_merge_preview_merge_log_identities_and_history(api, ws, other):
    h = ws["headers"]
    keep = new_contact(api, ws, f"ann.lee.{RUN}@merge-a1b.com", first_name="Ann", last_name="Lee", phone="111")
    dup = new_contact(api, ws, f"a.lee.{RUN}@merge-a1b.com", first_name="Annie", last_name="Lee",
                      designation="CTO", tags=["vip"])
    gone = new_contact(api, ws, f"gone.{RUN}@merge-a1b.com", first_name="Ann", last_name="Lee")
    api.post("/contacts/duplicates/scan", headers=h)

    _assert_preview(api, h, keep, dup)

    assert api.delete(f"/contacts/{gone}", headers=h).status_code == 200
    r = api.post("/contacts/merge/preview", headers=h, json={"primary_id": keep, "duplicate_ids": [gone]})
    assert r.status_code == 409 and "Recently deleted" in r.json()["detail"]
    r = api.post("/contacts/merge/preview", headers=other["headers"], json={"primary_id": keep, "duplicate_ids": [dup]})
    assert r.status_code == 404

    r = api.post("/contacts/merge", headers=h, json={"primary_id": keep, "duplicate_ids": [dup],
                                                     "choices": {"first_name": dup}})
    assert r.status_code == 200, r.text
    _assert_merge_log(api, h, keep, dup)
    _assert_identities(api, h, other, keep, dup)


def test_merge_takes_the_duplicates_email_and_retires_its_own(api, ws):
    h = ws["headers"]
    keep = new_contact(api, ws, f"k.{RUN}@swap-a1b.com", first_name="Kay")
    dup = new_contact(api, ws, f"d.{RUN}@swap-a1b.com", first_name="Kay")
    r = api.post("/contacts/merge", headers=h, json={"primary_id": keep, "duplicate_ids": [dup],
                                                     "choices": {"email": dup}})
    assert r.status_code == 200, r.text
    assert r.json()["contact"]["email"] == f"d.{RUN}@swap-a1b.com"
    rows = {e["email"]: e["status"] for e in api.get(f"/contacts/{keep}/emails", headers=h).json()["items"]}
    assert rows == {f"k.{RUN}@swap-a1b.com": "RETIRED"}


def test_merge_locks_and_refuses_a_contact_deleted_meanwhile(api, ws):
    keep_id = new_contact(api, ws, f"lock.k.{RUN}@lock-a1b.com", first_name="L")
    dup_id = new_contact(api, ws, f"lock.d.{RUN}@lock-a1b.com", first_name="L")
    db = SessionLocal()
    try:
        keep, dup = db.get(Prospect, keep_id), db.get(Prospect, dup_id)
        assert keep is not None and dup is not None
        db.commit()  # end the read transaction; the objects stay loaded
        sql("UPDATE prospects SET deleted_at = NOW(6) WHERE prospect_id = :p", p=dup_id)  # another request
        with SqlAlchemyContactsUnitOfWork.for_session(db) as uow:
            with pytest.raises(ConflictError, match="deleted or merged while you were merging"):
                merge_into(uow.contacts, keep, [dup], None)
    finally:
        db.rollback()
        db.close()
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=dup_id) == 1
    assert sql("SELECT COUNT(*) FROM merge_logs WHERE survivor_id = :p", p=keep_id) == 0


def test_erasing_the_survivor_removes_its_merge_logs_and_identifiers(api, ws):
    h = ws["headers"]
    keep = new_contact(api, ws, f"erase.k.{RUN}@erase-a1b.com", first_name="E")
    dup = new_contact(api, ws, f"erase.d.{RUN}@erase-a1b.com", first_name="E")
    assert api.post("/contacts/merge", headers=h, json={"primary_id": keep, "duplicate_ids": [dup]}).status_code == 200
    assert sql("SELECT COUNT(*) FROM merge_logs WHERE survivor_id = :p", p=keep) == 1
    assert api.delete(f"/contacts/{keep}", headers=h).status_code == 200
    assert api.post("/contacts/purge", headers=h, json={"prospect_ids": [keep]}).status_code == 200
    assert sql("SELECT COUNT(*) FROM merge_logs WHERE survivor_id = :p", p=keep) == 0
    assert sql("SELECT COUNT(*) FROM contact_identifiers WHERE current_prospect_id = :p", p=keep) == 0


def test_scan_job_and_lazy_first_scan(api, client, ws):
    fresh = register_tenant(client, "a1bj")
    h = fresh["headers"]
    for email in (f"x.{RUN}@gmail.com", f"X{RUN}+tag@gmail.com"):
        new_contact(api, fresh, email, first_name="X")
    assert sql("SELECT COUNT(*) FROM duplicate_pairs WHERE tenant_id = :t", t=fresh["tenant_id"]) == 0
    groups = api.get("/contacts/duplicates", headers=h).json()  # never scanned: scanned now
    assert groups["total_groups"] == 1 and groups["groups"][0]["confidence"] == "HIGH"
    totals = duplicate_scan.run_scan_job(SessionLocal, SqlAlchemyDuplicateRepository, [fresh["tenant_id"], ws["tenant_id"]])
    assert totals["tenants"] == 2 and totals["failed"] == 0


def test_export_log_and_no_silent_cut(api, ws, other, monkeypatch):
    h = ws["headers"]
    r = api.get("/contacts/export", headers=h, params={"format": "csv", "q": "merge-a1b", "columns": "name,email"})
    assert r.status_code == 200, r.text
    logs = api.get("/exports/logs", headers=h).json()
    latest = logs["items"][0]
    assert latest["object_type"] == "CONTACT" and latest["format"] == "csv" and latest["row_count"] >= 1
    assert latest["filter_summary"].startswith("q=merge-a1b") and latest["user_name"]
    assert api.get("/exports/logs", headers=other["headers"]).json()["total"] == 0

    monkeypatch.setattr(contact_query_service, "EXPORT_ROW_LIMIT", 1)
    r = api.get("/contacts/export", headers=h, params={"format": "csv"})
    assert r.status_code == 400 and "more than the 1 an export can hold" in r.json()["detail"]
    assert api.get("/exports/logs", headers=h).json()["total"] == logs["total"]  # refused: not logged
