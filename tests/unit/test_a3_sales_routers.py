"""A3: the sales routers with fake services (dependency_overrides): status codes, bodies, arguments."""
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import ConflictError, PermissionDenied, register_domain_error_handler
from app.domain.sales import LeadFilter, OpportunityFilter
from app.routers import quotes_router, sales_admin_router, sales_router

LEAD = "8a7fe580-5736-42fc-b78f-95175c72df0b"
ME = SimpleNamespace(user_id="me", tenant_id="t1", role="ADMIN")


class Recorder:
    """Fake service: records every call and returns a canned body (or raises)."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"ok": True} if result is None else result
        self.error = error

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if self.error:
                raise self.error
            return self.result
        return call


def _provider(fake):
    def provide():
        return fake
    return provide


@pytest.fixture
def make_client():
    def build(**fakes):
        app = FastAPI()
        register_domain_error_handler(app)
        for r in (sales_router, sales_admin_router, quotes_router):
            app.include_router(r.router)
            app.dependency_overrides[r.tenant_user] = lambda: ME
            app.dependency_overrides[r.admin_user] = lambda: ME
        deps = {"lead": [sales_router.lead_service], "deal": [sales_router.deal_service],
                "admin": [sales_router.admin_service, sales_admin_router.admin_service],
                "quote": [quotes_router.quote_service]}
        for key, fake in fakes.items():
            for dep in deps[key]:
                app.dependency_overrides[dep] = _provider(fake)
        return TestClient(app)
    return build


def test_list_leads_builds_filter_and_query(make_client):
    fake = Recorder({"items": [], "total": 0})
    r = make_client(lead=fake).get("/leads", params={"stage": "NEW,SQL", "owner": "me", "q": " ann ",
                                                     "sort_by": "next_step_at", "sort_order": "asc", "page": 2})
    assert r.status_code == 200 and r.json() == {"items": [], "total": 0}
    name, (user, flt, query) = fake.calls[0]
    assert name == "list_leads" and user is ME
    assert flt == LeadFilter(stages=("NEW", "SQL"), owner_id="me", search="ann")
    assert (query.sort.field, query.sort.descending, query.paging.page) == ("next_step_at", False, 2)


def test_bad_sort_is_still_a_422(make_client):
    assert make_client(lead=Recorder()).get("/leads", params={"sort_by": "password"}).status_code == 422


def test_path_ids_are_uuids_and_garbage_is_404_without_a_service_call(make_client):
    fake = Recorder()
    client = make_client(lead=fake)
    r = client.get("/leads/not-a-uuid")
    assert (r.status_code, r.json()) == (404, {"detail": "Lead not found"}) and fake.calls == []
    assert client.get(f"/leads/{LEAD}").status_code == 200
    assert fake.calls[0] == ("detail", (ME, UUID(LEAD)))


def test_patch_passes_only_set_fields_and_if_match(make_client):
    fake = Recorder()
    r = make_client(lead=fake).patch(f"/leads/{LEAD}", json={"stage": "SQL", "expected_version": 3},
                                     headers={"If-Match": '"3"'})
    assert r.status_code == 200
    assert fake.calls[0] == ("update", (ME, UUID(LEAD), {"stage": "SQL", "expected_version": 3}, 3))


def test_create_lead_is_201_and_conflicts_keep_their_body(make_client):
    ok = make_client(lead=Recorder({"lead_id": "x"})).post("/leads", json={"prospect_id": "p1"})
    assert ok.status_code == 201 and ok.json() == {"lead_id": "x"}
    err = ConflictError(detail={"message": "This contact already has an open lead", "lead_id": "l1"})
    r = make_client(lead=Recorder(error=err)).post("/leads", json={"prospect_id": "p1"})
    assert r.status_code == 409 and r.json()["detail"]["lead_id"] == "l1"


def test_opportunity_routes(make_client):
    fake = Recorder()
    client = make_client(deal=fake)
    assert client.get("/opportunities", params={"status": "OPEN", "stale": "true"}).status_code == 200
    name, (user, flt, stale, query) = fake.calls[0]
    assert (name, stale, flt.statuses, query.sort.field) == ("list_opportunities", True, ("OPEN",), "close_date")
    assert client.get("/pipeline/revenue", params={"owner": "me"}).status_code == 200
    assert fake.calls[1] == ("revenue", (ME, OpportunityFilter(owner_id="me")))
    assert client.delete("/opportunities/nope").status_code == 404
    assert client.post("/sales/stages", json={"name": "New"}).status_code == 201


def test_permission_errors_from_the_service_are_403(make_client):
    r = make_client(deal=Recorder(error=PermissionDenied("Agents cannot delete opportunities"))) \
        .delete(f"/opportunities/{LEAD}")
    assert (r.status_code, r.json()) == (403, {"detail": "Agents cannot delete opportunities"})


def test_set_lines_gets_the_raw_id_so_403_comes_before_404(make_client):
    fake = Recorder()
    r = make_client(quote=fake).put("/proposals/garbage/lines", json={"lines": [{"description": "x"}]})
    assert r.status_code == 200
    name, (user, proposal_id, lines, expected, if_match) = fake.calls[0]
    assert (name, proposal_id, expected, if_match) == ("set_lines", "garbage", None, None)
    assert lines == [{"product_id": None, "description": "x", "quantity": 1, "unit_price": None, "discount_pct": 0}]


def test_pdf_route_streams_the_service_bytes(make_client):
    r = make_client(quote=Recorder(("Quote one.pdf", b"%PDF-1.4"))).get(f"/proposals/{LEAD}/pdf")
    assert r.status_code == 200 and r.content == b"%PDF-1.4"
    assert r.headers["content-type"] == "application/pdf" and "Quote one.pdf" in r.headers["content-disposition"]


def test_approvals_query_and_admin_routes(make_client):
    quotes, admin = Recorder(), Recorder()
    client = make_client(quote=quotes, admin=admin)
    assert client.get("/quotes/approvals", params={"scope": "requested", "status": "ALL"}).status_code == 200
    q = quotes.calls[0][1][1]
    assert (q.scope, q.statuses, q.limit) == ("requested", ("PENDING", "APPROVED", "REJECTED"), 200)
    assert client.put("/sales/settings", json={"stale_deal_days": 7}).status_code == 200
    assert admin.calls[0] == ("write_settings", (ME, {"stale_deal_days": 7}))
    assert client.get("/template-library", params={"q": "  hi "}).status_code == 200
    assert admin.calls[1] == ("templates", (ME, "hi", None))
    assert client.put("/team/hierarchy/bad", json={"sales_level": 2}).json() == {"detail": "User not found"}
