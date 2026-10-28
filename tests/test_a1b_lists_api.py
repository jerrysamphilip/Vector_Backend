"""
a1b functional (MySQL): the lists aggregate after the layered refactor (chunk A1) -- /lists (unique names,
paging, members, active lists, export with the export log), /prospect-lists (paging, members, notes,
re-validation, delete with orphan cleanup), bulk enrollment checks and the repository's tenant isolation.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.errors import register_domain_error_handler
from app.domain.contacts import ListScope, VisibilityScope
from app.domain.lists import ListFilter, ProspectListFilter
from app.domain.paging import Paging
from app.main import app as main_app
from app.repositories.lists import SqlAlchemyListRepository
from app.routers import exports_router
from tests.conftest import RUN, register_tenant, sql


@pytest.fixture(scope="module")
def api():
    test_app = FastAPI()
    register_domain_error_handler(test_app)
    test_app.include_router(main_app.router)
    test_app.include_router(exports_router.router)
    return TestClient(test_app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "a1bl")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "a1bm")


def contact(api, ws, email, **kw):
    r = api.post("/contacts", headers=ws["headers"], json={"email": email, **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def test_list_names_are_unique_per_workspace(api, ws, other):
    h = ws["headers"]
    r = api.post("/lists", headers=h, json={"list_name": f"VIP {RUN}"})
    assert r.status_code == 201, r.text
    first = r.json()["list_id"]
    r = api.post("/lists", headers=h, json={"list_name": f"  vip {RUN} "})
    assert r.status_code == 409
    assert r.json()["detail"] == {"message": "A list with this name already exists. Choose another name.",
                                  "field": "list_name"}
    assert api.post("/lists", headers=other["headers"], json={"list_name": f"VIP {RUN}"}).status_code == 201
    second = api.post("/lists", headers=h, json={"list_name": f"Other {RUN}"}).json()["list_id"]
    assert api.patch(f"/lists/{second}", headers=h, json={"list_name": f"VIP {RUN}"}).status_code == 409
    assert api.patch(f"/lists/{first}", headers=h, json={"list_name": f"VIP {RUN}", "description": "x"}).status_code == 200
    # The database refuses a duplicate too (unique index from migration 0025)
    assert sql("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
               "AND table_name = 'prospect_lists' AND index_name = 'uq_prospect_lists_tenant_name'") == 2


def test_get_lists_old_shape_and_paged(api, ws):
    h = ws["headers"]
    for i in range(3):
        api.post("/lists", headers=h, json={"list_name": f"Paged {i} {RUN}"})
    old = api.get("/lists", headers=h).json()
    assert set(old) == {"items"} and len(old["items"]) >= 3
    page = api.get("/lists", headers=h, params={"page": 2, "page_size": 2, "q": f"Paged"}).json()
    assert page["total"] == 3 and page["page"] == 2 and page["page_size"] == 2 and len(page["items"]) == 1
    assert {"list_id", "list_name", "member_count", "created_by_name"} <= set(page["items"][0])


def test_members_active_lists_and_export_log(api, ws):
    h = ws["headers"]
    a = contact(api, ws, f"m1.{RUN}@lists-a1b.com", first_name="Mem", lifecycle_stage="CUSTOMER")
    b = contact(api, ws, f"m2.{RUN}@lists-a1b.com", first_name="Mem")
    lst = api.post("/lists", headers=h, json={"list_name": f"Members {RUN}"}).json()["list_id"]
    out = api.post(f"/lists/{lst}/members", headers=h, json={"prospect_ids": [a, b, a, "missing"]}).json()
    assert out == {"added": 2, "already_in_list": 0, "not_found": 1}
    assert api.post(f"/lists/{lst}/members", headers=h, json={"prospect_ids": [a]}).json()["already_in_list"] == 1
    assert api.get(f"/lists/{lst}", headers=h).json()["member_count"] == 2

    active = api.post("/lists", headers=h, json={
        "list_name": f"Customers {RUN}", "list_type": "ACTIVE",
        "filters": {"op": "AND", "conditions": [{"field": "lifecycle_stage", "operator": "is", "value": "CUSTOMER"}]}})
    assert active.status_code == 201, active.text
    assert api.post(f"/lists/{active.json()['list_id']}/members", headers=h,
                    json={"prospect_ids": [a]}).status_code == 400
    preview = api.post("/lists/preview", headers=h, json={"filters": {"op": "AND", "conditions": [
        {"field": "email", "operator": "contains", "value": f"{RUN}@lists-a1b.com"}]}}).json()
    assert preview["count"] == 2 and len(preview["sample"]) == 2

    r = api.get(f"/lists/{lst}/export", headers=h, params={"format": "csv", "columns": "name,email"})
    assert r.status_code == 200 and f"m1.{RUN}@lists-a1b.com" in r.text
    log = api.get("/exports/logs", headers=h, params={"object_type": "LIST"}).json()["items"][0]
    assert log["row_count"] == 2 and log["filter_summary"].startswith(f"list=Members {RUN}")
    assert api.post(f"/lists/{lst}/members/remove", headers=h, json={"prospect_ids": [a]}).json() == {"removed": 1}
    assert api.delete(f"/lists/{lst}", headers=h).json() == {"status": "deleted", "list_id": lst}
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=b) == 1  # contacts are kept
    assert api.get(f"/lists/{lst}", headers=h).status_code == 404


def test_prospect_lists_routes(api, ws, other):
    h = ws["headers"]
    keep = contact(api, ws, f"pl1.{RUN}@plists-a1b.com", first_name="Pl", poc_state="Texas")
    only = contact(api, ws, f"pl2.{RUN}@gmail.com", first_name="Pl")
    upload = api.post("/lists", headers=h, json={"list_name": f"Upload {RUN}"}).json()["list_id"]
    api.post(f"/lists/{upload}/members", headers=h, json={"prospect_ids": [keep, only]})
    other_list = api.post("/lists", headers=h, json={"list_name": f"Keep {RUN}"}).json()["list_id"]
    api.post(f"/lists/{other_list}/members", headers=h, json={"prospect_ids": [keep]})

    page = api.get("/prospect-lists", headers=h, params={"search": f"Upload {RUN}"}).json()
    assert page["total"] == 1 and page["items"][0]["prospect_count"] == 2
    detail = api.get(f"/prospect-lists/{upload}/prospects", headers=h, params={"search": "pl2"}).json()
    assert detail["total"] == 1 and detail["prospects"][0]["prospect_id"] == only
    r = api.patch(f"/prospect-lists/{upload}/prospects/{keep}/notes", headers=h, json={"notes": "call"})
    assert r.json() == {"status": "updated", "prospect_id": keep, "list_id": upload}
    reval = api.post(f"/prospect-lists/{upload}/revalidations", headers=h).json()
    assert reval["total_checked"] == 2 and reval["warning_count"] == 1
    personal = next(x for x in reval["results"] if x["prospect_id"] == only)
    assert "Personal email domain detected" in personal["issues"] and "Missing POC State" in personal["issues"]
    assert api.get(f"/prospect-lists/{upload}/prospects", headers=other["headers"]).status_code == 404
    stats = api.get("/prospects/stats", headers=h).json()
    assert stats["total"] >= 2 and stats["active"] + stats["available"] == stats["total"]

    gone = api.delete(f"/prospect-lists/{upload}", headers=h).json()
    assert gone == {"status": "deleted", "list_id": upload, "deleted_prospects": 1}
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=only) == 0
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=keep) == 1


def test_bulk_enrollment_checks(api, ws, other):
    h = ws["headers"]
    r = api.post("/campaigns/nope/enrollments/bulk", headers=h, json={"campaign_id": "nope", "list_ids": []})
    assert r.status_code == 404 and r.json() == {"detail": "Campaign not found"}


def test_repository_tenant_isolation_and_paging(ws, other):
    with SessionLocal() as db:
        repo = SqlAlchemyListRepository(db)
        mine = repo.search(ws["tenant_id"], ListFilter(q=f"{RUN}"), 500)
        assert mine and all(lst.tenant_id == ws["tenant_id"] for lst in mine)
        first = mine[0]
        assert repo.get(other["tenant_id"], first.list_id) is None
        assert repo.name_taken(ws["tenant_id"], first.list_name.upper())
        assert not repo.name_taken(ws["tenant_id"], first.list_name, except_id=first.list_id)
        page = repo.page(ws["tenant_id"], ListFilter(list_type="ACTIVE"), Paging(1, 10))
        assert page.total == 1 and page.items[0].list_type == "ACTIVE"
        uploads = repo.upload_page(other["tenant_id"], ProspectListFilter(), Paging(1, 10))
        assert all(lst.tenant_id == other["tenant_id"] for lst in uploads.items)
        scope = ListScope(first.list_id, ws["tenant_id"])
        assert repo.visible_member_count(scope, VisibilityScope(frozenset({"nobody"}))) == 0
        assert repo.live_prospect_ids(other["tenant_id"], [m for m in repo.all_member_ids(first.list_id)]) == set()


def test_free_list_name_for_flows_that_name_lists_themselves(api, ws):
    from app.repositories.lists import free_list_name

    h = ws["headers"]
    api.post("/lists", headers=h, json={"list_name": f"Auto 100% {RUN}"})
    with SessionLocal() as db:
        assert free_list_name(db, ws["tenant_id"], f"auto 100% {RUN}") == f"auto 100% {RUN} (2)"
        assert free_list_name(db, ws["tenant_id"], f"Fresh {RUN}") == f"Fresh {RUN}"
