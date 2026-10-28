"""
a1b route layer: lists, prospect lists, duplicate review and export-log routers with fake services injected
through ``app.dependency_overrides``. Asserts status codes, bodies and the domain objects each handler
passes down; no database, no real service.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import ConflictError, NotFoundError, register_domain_error_handler
from app.domain.lists import ListFilter
from app.domain.paging import Paging
from app.routers import duplicates_router, exports_router, lists_router, prospect_list_router
from app.services.contact_query_service import ExportFile

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="ADMIN")


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"ok": True} if result is None else result
        self.error = error

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return self.result
        return call


@pytest.fixture
def make_client():
    def build(fake):
        app = FastAPI()
        register_domain_error_handler(app)
        app.include_router(lists_router.router)
        app.include_router(prospect_list_router.router)
        app.include_router(prospect_list_router.prospect_upload_router)
        app.include_router(duplicates_router.router)
        app.include_router(exports_router.router)
        for dep in (lists_router.tenant_user, prospect_list_router.tenant_user, prospect_list_router.any_user,
                    duplicates_router.tenant_user, exports_router.tenant_user,
                    prospect_list_router.manage_prospects[0].dependency):
            app.dependency_overrides[dep] = lambda: ME
        for dep in (lists_router.list_service, prospect_list_router.prospect_list_service,
                    duplicates_router.duplicate_service, exports_router.export_log_service):
            app.dependency_overrides[dep] = lambda: fake
        return TestClient(app)
    return build


def test_get_lists_unpaged_and_paged(make_client):
    fake = Recorder({"items": []})
    client = make_client(fake)
    assert client.get("/lists", params={"list_type": "STATIC", "q": "vip"}).json() == {"items": []}
    assert fake.calls[-1] == ("list", (ME, ListFilter(list_type="STATIC", q="vip"), None), {})
    client.get("/lists", params={"page": 2})
    assert fake.calls[-1][1][2] == Paging(2, 50)
    client.get("/lists", params={"page_size": 10})
    assert fake.calls[-1][1][2] == Paging(1, 10)
    assert client.get("/lists", params={"page_size": 0}).status_code == 422


def test_list_writes_pass_payloads(make_client):
    fake = Recorder({"list_id": "L1"})
    client = make_client(fake)
    r = client.post("/lists", json={"list_name": "A", "list_type": "ACTIVE", "filters": {"op": "AND"}})
    assert r.status_code == 201
    assert fake.calls[-1] == ("create", (ME, "A", "ACTIVE", {"op": "AND"}, None), {})
    client.patch("/lists/L1", json={"description": "d"})
    assert fake.calls[-1] == ("update", (ME, "L1", {"description": "d"}), {})
    client.post("/lists/L1/members", json={"prospect_ids": ["p1"]})
    assert fake.calls[-1] == ("add_members", (ME, "L1", ["p1"]), {})
    client.post("/lists/L1/members/remove", json={"prospect_ids": ["p1"]})
    assert fake.calls[-1][0] == "remove_members"
    assert client.post("/lists/L1/members", json={"prospect_ids": []}).status_code == 422


def test_list_name_conflict_is_409_with_detail(make_client):
    detail = {"message": "A list with this name already exists. Choose another name.", "field": "list_name"}
    client = make_client(Recorder(error=ConflictError(detail["message"], detail=detail)))
    r = client.post("/lists", json={"list_name": "Dup"})
    assert r.status_code == 409 and r.json() == {"detail": detail}


def test_list_export_streams_the_file(make_client):
    fake = Recorder(ExportFile(b"a,b\n", "text/csv", "contacts-x.csv"))
    client = make_client(fake)
    r = client.get("/lists/L1/export", params={"format": "csv", "columns": "email"})
    assert r.status_code == 200 and r.content == b"a,b\n" and "contacts-x.csv" in r.headers["content-disposition"]
    assert fake.calls[-1] == ("export", (ME, "L1", "csv", "email"), {})


def test_prospect_list_routes(make_client):
    fake = Recorder({"items": [], "total": 0, "page": 1, "page_size": 20})
    client = make_client(fake)
    assert client.get("/prospect-lists", params={"sort_by": "list_name", "sort_order": "asc"}).status_code == 200
    name, (who, f, paging), _ = fake.calls[-1]
    assert name == "page" and f.sort.field == "list_name" and not f.sort.descending and paging == Paging(1, 20)
    nf = make_client(Recorder(error=NotFoundError("List not found")))
    assert nf.delete("/prospect-lists/x").json() == {"detail": "List not found"}
    assert nf.post("/campaigns/c/enrollments/bulk", json={"campaign_id": "c", "list_ids": ["x"]}).status_code == 404


def test_prospect_update_drops_blank_email(make_client):
    fake = Recorder({"status": "updated", "prospect_id": "p", "row_version": 2})
    client = make_client(fake)
    r = client.patch("/prospects/p", json={"email": "  ", "first_name": "Ann"}, headers={"If-Match": "3"})
    assert r.status_code == 200
    assert fake.calls[-1] == ("update_prospect", (ME, "p", {"first_name": "Ann"}, 3), {})


def test_prospect_stats_response_model(make_client):
    client = make_client(Recorder({"total": 3, "available": 1, "active": 2, "opted_out": 0}))
    assert client.get("/prospects/stats").json() == {"total": 3, "available": 1, "active": 2, "opted_out": 0}


def test_duplicate_routes(make_client):
    fake = Recorder({"items": []})
    client = make_client(fake)
    client.get("/contacts/duplicates/pairs", params={"status": "DISMISSED", "confidence": "HIGH", "page_size": 5})
    assert fake.calls[-1] == ("pairs", (ME, "DISMISSED", "HIGH", Paging(1, 5)), {})
    assert client.get("/contacts/duplicates/pairs", params={"status": "nope"}).status_code == 422
    client.post("/contacts/duplicates/scan")
    assert fake.calls[-1][0] == "scan"
    client.post("/contacts/duplicates/p1/dismiss")
    assert fake.calls[-1] == ("dismiss", (ME, "p1"), {})
    client.post("/contacts/duplicates/p1/reopen")
    assert fake.calls[-1] == ("reopen", (ME, "p1"), {})
    client.post("/contacts/merge/preview", json={"primary_id": "a", "duplicate_ids": ["b"]})
    assert fake.calls[-1] == ("merge_preview", (ME, "a", ["b"]), {})
    client.get("/contacts/a/merges", params={"limit": 5})
    assert fake.calls[-1] == ("merge_history", (ME, "a", 5), {})
    client.get("/contacts/a/emails")
    assert fake.calls[-1] == ("email_history", (ME, "a"), {})


def test_export_logs_route(make_client):
    fake = Recorder({"items": [], "total": 0})
    client = make_client(fake)
    client.get("/exports/logs", params={"object_type": "LIST", "page": 2, "page_size": 10})
    assert fake.calls[-1] == ("page", (ME, "LIST", None, Paging(2, 10)), {})
    assert client.get("/exports/logs", params={"object_type": "X"}).status_code == 422


class _Db:
    def __init__(self):
        self.rolled_back = 0

    def rollback(self):
        self.rolled_back += 1


@pytest.fixture
def upload_client(make_client, monkeypatch):
    """The upload routes call module functions with the request's db: record them instead."""
    calls, db = [], _Db()

    def record(name, result=None, error=None):
        def call(**kwargs):
            calls.append((name, kwargs))
            if error:
                raise error
            return {"accepted": 2} if result is None else result
        return call

    def build(**patches):
        for name, fn in patches.items():
            monkeypatch.setattr(prospect_list_router, name, fn)
        client = make_client(Recorder())
        client.app.dependency_overrides[prospect_list_router.get_db] = lambda: db
        return client
    return build, record, calls, db


UID = "0b5c1e3a-9d1f-4f7e-8a2b-111111111111"


def test_upload_confirm_uses_the_path_id(upload_client):
    build, record, calls, _ = upload_client
    client = build(confirm_upload=record("confirm"))
    r = client.post(f"/uploads/{UID}/confirmations", json={"upload_id": UID, "title": "Q4", "records": []})
    assert r.status_code == 200 and r.json() == {"accepted": 2}
    assert calls[0][0] == "confirm" and calls[0][1]["upload_id"] == UID


def test_upload_confirm_refuses_a_body_id_that_differs_from_the_path(upload_client):
    build, record, calls, _ = upload_client
    client = build(confirm_upload=record("confirm"))
    r = client.post(f"/uploads/{UID}/confirmations", json={"upload_id": "other", "title": "Q4", "records": []})
    assert r.status_code == 400 and "does not match" in r.json()["detail"] and calls == []


def test_upload_confirm_and_revalidate_pass_domain_errors_through(upload_client):
    build, record, _, db = upload_client
    missing = NotFoundError("Upload not found")
    client = build(confirm_upload=record("confirm", error=missing),
                   revalidate_prospect_records=record("revalidate", error=missing))
    assert client.post(f"/uploads/{UID}/confirmations",
                       json={"upload_id": UID, "title": "Q4", "records": []}).status_code == 404
    assert client.post(f"/uploads/{UID}/revalidations", json={"records": []}).status_code == 404


def test_upload_revalidate_uses_the_path_id(upload_client):
    build, record, calls, _ = upload_client
    client = build(revalidate_prospect_records=record("revalidate", result={"records": []}))
    r = client.post(f"/uploads/{UID}/revalidations", json={"records": [{"email": "a@b.co"}]})
    assert r.status_code == 200 and calls[0][1]["upload_id"] == UID and calls[0][1]["tenant_id"] == "t1"
