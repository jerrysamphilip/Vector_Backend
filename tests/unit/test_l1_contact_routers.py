"""
L1 route layer (ADR 0009): contacts, CRM (views / search) and tasks routers with fake services
injected through ``app.dependency_overrides``. Asserts status codes, bodies and the domain objects
each handler passes down; no database, no real service.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import NotFoundError, PermissionDenied, register_domain_error_handler
from app.domain.contacts import ContactFilter, TaskFilter
from app.domain.paging import Paging, SortDirection
from app.routers import contacts_router, crm_router, tasks_router
from app.services.contact_query_service import ExportFile
from app.services.task_service import task_dict

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="ADMIN")


class Recorder:
    """Fake service: records every call and returns a canned body (or raises)."""

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
    def build(**fakes):
        app = FastAPI()
        register_domain_error_handler(app)
        for r in (contacts_router, crm_router, tasks_router):
            app.include_router(r.router)
            app.dependency_overrides[r.tenant_user] = lambda: ME
        deps = {"reads": [contacts_router.query_service, crm_router.search_service],
                "writes": [contacts_router.write_service], "fields": [contacts_router.field_service],
                "activities": [contacts_router.activity_service], "views": [crm_router.view_service],
                "tasks": [tasks_router.task_service]}
        for key, fake in fakes.items():
            for dep in deps[key]:
                app.dependency_overrides[dep] = (lambda f: (lambda: f))(fake)
        return TestClient(app)
    return build


def test_list_builds_filter_sort_and_paging(make_client):
    fake = Recorder({"items": [], "total": 0, "page": 3, "page_size": 10})
    client = make_client(reads=fake)
    r = client.get("/contacts", params={"q": "ann", "owner": "me", "email_valid": "true", "grade": "A,B",
                                        "score_min": 5, "sort_by": "score", "sort_order": "asc",
                                        "page": 3, "page_size": 10, "filters": '{"op":"AND","conditions":[]}'})
    assert r.status_code == 200 and r.json()["page"] == 3
    name, (user, f, sort, paging), _ = fake.calls[0]
    assert name == "list_page" and user is ME
    assert f == ContactFilter(q="ann", owner="me", email_valid=True, grade="A,B", score_min=5,
                              filters='{"op":"AND","conditions":[]}')
    assert (sort.field, sort.direction) == ("score", SortDirection.ASC)
    assert paging == Paging(3, 10)


def test_list_defaults_and_validation(make_client):
    fake = Recorder({"items": []})
    client = make_client(reads=fake)
    assert client.get("/contacts").status_code == 200
    _, (_, f, sort, paging), _ = fake.calls[0]
    assert f == ContactFilter() and sort.field == "created_at" and sort.descending and paging == Paging(1, 25)
    assert client.get("/contacts", params={"sort_by": "password"}).status_code == 422
    assert client.get("/contacts", params={"page_size": 201}).status_code == 422
    assert client.get("/contacts", params={"score_max": 101}).status_code == 422


def test_board_export_and_deleted(make_client):
    fake = Recorder()
    client = make_client(reads=fake)
    assert client.get("/contacts/board", params={"group_by": "lead_status", "owner": "team"}).status_code == 200
    assert fake.calls[-1][1][1:] == ("lead_status", 50, ContactFilter(owner="team"))
    fake.result = ExportFile(b"a,b\r\n", "text/csv", "contacts-x.csv")
    r = client.get("/contacts/export", params={"format": "csv", "tag": "vip", "columns": "email"})
    assert r.status_code == 200 and r.content == b"a,b\r\n" and r.headers["content-type"].startswith("text/csv")
    assert "contacts-x.csv" in r.headers["content-disposition"]
    assert fake.calls[-1][1][1:] == ("csv", "email", ContactFilter(tag="vip"))
    fake.result = {"items": []}
    assert client.get("/contacts/deleted", params={"q": "x", "page": 2}).status_code == 200
    assert fake.calls[-1][0] == "deleted_page" and fake.calls[-1][1][1:] == ("x", Paging(2, 25))


def test_domain_errors_keep_their_status(make_client):
    client = make_client(reads=Recorder(error=NotFoundError("Contact not found")),
                         writes=Recorder(error=PermissionDenied("Agents cannot delete contacts")))
    r = client.get("/contacts/not-a-uuid")  # malformed ids are a plain 404, as before
    assert r.status_code == 404 and r.json() == {"detail": "Contact not found"}
    r = client.delete("/contacts/p1")
    assert r.status_code == 403 and r.json() == {"detail": "Agents cannot delete contacts"}


def test_writes_pass_payload_and_if_match(make_client):
    fake = Recorder({"prospect_id": "p1"})
    client = make_client(writes=fake)
    r = client.patch("/contacts/p1", json={"first_name": "Ann"}, headers={"If-Match": '"3"'})
    assert r.status_code == 200
    name, (user, pid, payload, if_match), _ = fake.calls[-1]
    assert (name, pid, payload.first_name, if_match) == ("update", "p1", "Ann", 3)
    assert client.post("/contacts", json={"email": "a@b.io"}).status_code == 201
    assert client.post("/contacts/bulk", json={"prospect_ids": ["p1"], "action": "add_tags",
                                               "tags": ["x"]}).status_code == 200
    assert client.post("/contacts/restore", json={"prospect_ids": ["p1"]}).status_code == 200
    assert fake.calls[-1][:2] == ("restore", (ME, ["p1"]))
    r = client.delete("/contacts/p1/companies/a1", params={"new_primary_id": "a2"})
    assert r.status_code == 200 and fake.calls[-1][1][1:] == ("p1", "a1", "a2")
    assert client.post("/contacts", json={"email": "not-an-email"}).status_code == 422


def test_fields_activities_and_timeline(make_client):
    fields, activities, reads = Recorder(), Recorder(), Recorder()
    client = make_client(fields=fields, activities=activities, reads=reads)
    assert client.post("/contacts/fields", json={"label": "Seats"}).status_code == 201
    assert fields.calls[-1][0] == "create"
    assert client.delete("/contacts/fields/f1").status_code == 200 and fields.calls[-1][1][1:] == ("f1",)
    assert client.post("/contacts/p1/activities", json={"activity_type": "NOTE", "body": "hi"}).status_code == 201
    assert activities.calls[-1][0] == "log" and activities.calls[-1][1][1] == "p1"
    assert client.get("/contacts/p1/timeline", params={"types": "NOTE", "limit": 5}).status_code == 200
    name, args, kwargs = reads.calls[-1]
    assert name == "timeline" and args[1:] == ("p1", "NOTE", None, 5) and kwargs == {"task_serializer": task_dict}


def test_views_search_and_tasks(make_client):
    views, reads, tasks = Recorder([]), Recorder({"contacts": [], "companies": []}), Recorder({"items": []})
    client = make_client(views=views, reads=reads, tasks=tasks)
    assert client.get("/views", params={"object_type": "COMPANY"}).json() == []
    assert views.calls[-1][1][1:] == ("COMPANY",)
    assert client.get("/views", params={"object_type": "DEAL"}).status_code == 422
    assert client.get("/search", params={"q": "ac", "limit": 3}).status_code == 200
    assert reads.calls[-1][:2] == ("global_search", (ME, "ac", 3))
    assert client.get("/search", params={"q": "a"}).status_code == 422
    assert client.get("/tasks", params={"scope": "team", "due": "week", "account_id": "a1"}).status_code == 200
    name, (user, f, paging), _ = tasks.calls[-1]
    assert f == TaskFilter(scope="team", status="OPEN", due="week", account_id="a1") and paging == Paging(1, 50)
    assert client.post("/tasks", json={"title": "Call"}).status_code == 201
    assert client.delete("/tasks/t9").status_code == 200 and tasks.calls[-1][1][1:] == ("t9",)
