"""
A1 router layer (ADR 0009): accounts_router parses the request into domain objects and makes one service call.
``FastAPI()`` + the domain error handler + the router; services and the user are swapped through
``dependency_overrides``. No database.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import DomainError, NotFoundError, domain_error_handler
from app.routers import accounts_router as r
from app.services.contact_query_service import ExportFile

USER = SimpleNamespace(user_id="u1", tenant_id="t1", role="ADMIN", custom_permissions=None, sales_level=None)


class Recorder:
    """Stands in for every service: records (method, args) and returns a canned value."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        return call


@pytest.fixture
def services():
    return {"accounts": Recorder(), "backfill": Recorder(), "fields": Recorder()}


@pytest.fixture
def client(services):
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(r.router)
    app.dependency_overrides[r.tenant_user] = lambda: USER
    app.dependency_overrides[r.account_service] = lambda: services["accounts"]
    app.dependency_overrides[r.backfill_service] = lambda: services["backfill"]
    app.dependency_overrides[r.company_field_service] = lambda: services["fields"]
    return TestClient(app)


def test_list_passes_filter_sort_and_paging(client, services):
    res = client.get("/accounts", params={"q": "acme", "owner": "me", "sort_by": "contacts", "sort_order": "desc",
                                          "page": 2, "page_size": 10, "status": "archived",
                                          "filters": '{"field": "custom.tier", "operator": "is", "value": "Gold"}'})
    assert res.status_code == 200
    name, (user, f, sort, paging), _ = services["accounts"].calls[0]
    assert name == "list_page" and user is USER
    assert (f.q, f.owner, f.status, f.filters) == ("acme", "me", "archived", '{"field": "custom.tier", "operator": "is", "value": "Gold"}')
    assert (sort.field, sort.descending, paging.page, paging.page_size) == ("contacts", True, 2, 10)


def test_list_rejects_unknown_sort_and_status(client):
    assert client.get("/accounts", params={"sort_by": "email"}).status_code == 422
    assert client.get("/accounts", params={"status": "gone"}).status_code == 422


def test_static_paths_win_over_account_id(client, services):
    assert client.get("/accounts/meta").status_code == 200
    assert client.get("/accounts/fields").status_code == 200
    assert client.get("/accounts/deleted").status_code == 200
    assert [c[0] for c in services["accounts"].calls] == ["meta", "deleted"]
    assert services["fields"].calls[0][0] == "list"


def test_record_routes(client, services):
    assert client.get("/accounts/a1").status_code == 200
    assert client.post("/accounts", json={"name": "Acme", "custom_fields": {"tier": "Gold"}}).status_code == 201
    assert client.patch("/accounts/a1", json={"name": "Acme 2"}, headers={"If-Match": "3"}).status_code == 200
    assert client.delete("/accounts/a1").status_code == 200
    assert client.post("/accounts/a1/restore").status_code == 200
    calls = services["accounts"].calls
    assert [c[0] for c in calls] == ["get", "create", "update", "delete", "restore"]
    assert calls[1][1][1].custom_fields == {"tier": "Gold"}
    _, (user, account_id, payload, if_match), _ = calls[2]
    assert (account_id, payload.name, if_match) == ("a1", "Acme 2", 3)


def test_domain_errors_become_http(client, services):
    services["accounts"].result = NotFoundError("Company not found")
    res = client.get("/accounts/zzz")
    assert res.status_code == 404 and res.json() == {"detail": "Company not found"}


def test_company_field_routes(client, services):
    assert client.post("/accounts/fields", json={"label": "Tier", "field_type": "SELECT",
                                                 "options": ["Gold"]}).status_code == 201
    assert client.patch("/accounts/fields/f1", json={"label": "Tier 2"}).status_code == 200
    assert client.delete("/accounts/fields/f1").status_code == 200
    assert [c[0] for c in services["fields"].calls] == ["create", "update", "delete"]


def test_backfill_routes(client, services):
    assert client.post("/accounts/backfill").status_code == 200
    assert client.post("/accounts/backfill/previews").status_code == 200
    assert client.post("/accounts/backfill/runs").status_code == 201
    assert client.get("/accounts/backfill/runs", params={"limit": 5}).status_code == 200
    assert client.post("/accounts/backfill/runs/r1/undo").status_code == 200
    calls = services["backfill"].calls
    assert [c[0] for c in calls] == ["legacy", "preview", "run", "list_runs", "undo"]
    assert calls[3][1] == (USER, 5) and calls[4][1] == (USER, "r1")


def test_export_streams_the_file(client, services):
    services["accounts"].result = ExportFile(b"Company\r\nAcme\r\n", "text/csv", "companies-x.csv")
    res = client.get("/accounts/export", params={"format": "csv", "columns": "name", "industry": "SaaS"})
    assert res.status_code == 200 and res.content == b"Company\r\nAcme\r\n"
    assert "companies-x.csv" in res.headers["content-disposition"]
    _, (user, fmt, columns, f), _ = services["accounts"].calls[0]
    assert (fmt, columns, f.industry, f.status) == ("csv", "name", "SaaS", "active")
    assert client.get("/accounts/export", params={"format": "pdf"}).status_code == 422
