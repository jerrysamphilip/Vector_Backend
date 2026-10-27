"""A6 route layer: the reports, custom reports, sales reports and KPI routers with fake services injected
through ``app.dependency_overrides``. Asserts status, body and the arguments each handler passes down."""
import io
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import NotFoundError, PermissionDenied, register_domain_error_handler
from app.routers import custom_reports_router, reports_router, sales_kpi_router, sales_reports_router
from app.schemas.reports_custom_schema import Definition, ReportWrite
from app.services.reports_service import ReportFile

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="MANAGER", sales_level=2)


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
    def build(fake):
        app = FastAPI()
        register_domain_error_handler(app)
        for module in (reports_router, custom_reports_router, sales_reports_router, sales_kpi_router):
            app.include_router(module.router)
            for route in module.router.routes:
                for dep in route.dependant.dependencies:
                    if dep.call.__name__ in ("_role_checker", "_checker"):
                        app.dependency_overrides[dep.call] = lambda: ME
        for provider in (reports_router.get_reports_service, custom_reports_router.custom_reports,
                         sales_reports_router.sales_reports, sales_kpi_router.sales_reports,
                         sales_kpi_router.crm_kpi_reports):
            app.dependency_overrides[provider] = lambda: fake
        return TestClient(app)
    return build


def test_dashboard_summary_passes_query_down(make_client):
    fake = Recorder({"kpis": {}, "timeseries": [], "lead_stats": {}, "campaign_stats": {}, "mailbox_stats": {},
                     "providers": [], "mailbox_health": [], "top_campaigns": [], "optimization": {},
                     "user_breakdown": []})
    r = make_client(fake).get("/reports/dashboard-summary", params={"start_date": "2025-05-01", "user_id": "u2",
                                                                    "granularity": "weekly"})
    assert r.status_code == 200, r.text
    name, args, _ = fake.calls[0]
    assert name == "dashboard_summary" and args == (ME, date(2025, 5, 1), None, "weekly", "u2")


def test_comparison_validates_paging_before_the_service(make_client):
    fake = Recorder({"items": [], "total": 0, "page": 1, "page_size": 20})
    client = make_client(fake)
    assert client.get("/reports/campaign-comparison", params={"page_size": 101}).status_code == 422
    assert client.get("/reports/campaign-comparison", params={"sort_by": "reply_rate", "page": 2}).status_code == 200
    assert fake.calls[0][1] == (ME, None, None, "reply_rate", "desc", 2, 20, None)


def test_scope_refusal_is_403(make_client):
    r = make_client(Recorder(error=PermissionDenied("That user is outside your team"))).get(
        "/reports/global-analytics", params={"user_id": "x"})
    assert r.status_code == 403 and r.json() == {"detail": "That user is outside your team"}


def test_exports_stream_the_service_file(make_client):
    fake = Recorder(ReportFile(io.StringIO("a,b\n"), "campaign_comparison.csv"))
    r = make_client(fake).get("/reports/export/campaign-comparison")
    assert r.status_code == 200 and r.text == "a,b\n" and r.headers["content-type"].startswith("text/csv")
    assert "campaign_comparison.csv" in r.headers["content-disposition"]
    fake.result = ReportFile(io.BytesIO(b"PK"), "analytics_report.docx")
    r = make_client(fake).get("/reports/export/global-analytics/docx")
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_prospect_export_404_from_service(make_client):
    r = make_client(Recorder(error=NotFoundError("Campaign not found"))).get("/reports/export/prospects/c9")
    assert r.status_code == 404 and r.json()["detail"] == "Campaign not found"


def test_custom_report_routes(make_client):
    fake = Recorder({"rows": []})
    client = make_client(fake)
    assert client.post("/reports/custom/run", json={"object": "leads", "group_by": "stage"}).status_code == 200
    name, (user, definition), _ = fake.calls[-1]
    assert name == "run_adhoc" and user is ME and definition == Definition(object="leads", group_by="stage")
    assert client.post("/reports/custom", json={"name": "x"}).status_code == 201
    assert fake.calls[-1][0] == "create" and fake.calls[-1][1][1] == ReportWrite(name="x")
    client.patch("/reports/custom/r1", json={"shared": True})
    assert fake.calls[-1][:2] == ("update", (ME, "r1", ReportWrite(shared=True)))
    client.delete("/reports/custom/r1")
    assert fake.calls[-1][:2] == ("delete", (ME, "r1"))
    assert client.post("/reports/custom/run", json={"group_by": "stage"}).status_code == 422


def test_custom_meta_needs_no_service(make_client):
    body = make_client(Recorder()).get("/reports/custom/meta").json()
    assert body["can_build"] is True and "leads" in body["objects"]


@pytest.mark.parametrize("path,method,expected", [
    ("/sales-reports/dashboard?member=u2&date_from=2025-05-01", "dashboard", (ME, date(2025, 5, 1), None, "u2")),
    ("/sales-reports/funnel", "funnel", (ME, None, None, None)),
    ("/sales-reports/pipeline?client_type=NEW", "pipeline", (ME, "NEW", None)),
    ("/sales-reports/forecast?fy=2025", "forecast", (ME, 2025, None, None)),
    ("/sales-reports/targets?quarter=2", "targets", (ME, None, 2, None)),
    ("/sales-reports/daily-limit", "daily_limit", (ME,)),
    ("/sales-reports/sql-to-win?fy=2025&quarter=1", "sql_to_win", (ME, None, None, 2025, 1, None)),
    ("/sales-reports/forecast-accuracy?periods=4", "forecast_accuracy", (ME, "QUARTER", 4, None)),
    ("/sales-reports/kpis?member=u3", "kpis", (ME, None, None, None, None, "u3")),
])
def test_sales_routes_call_one_service_method(make_client, path, method, expected):
    fake = Recorder()
    r = make_client(fake).get(path)
    assert r.status_code == 200, r.text
    assert fake.calls == [(method, expected, {})]


def test_sales_query_validation(make_client):
    client = make_client(Recorder())
    assert client.get("/sales-reports/forecast", params={"fy": 1900}).status_code == 422
    assert client.get("/sales-reports/forecast-accuracy", params={"period_type": "YEAR"}).status_code == 422
    assert client.post("/sales-reports/forecast-snapshots", json={"period_type": "DAY"}).status_code == 422


def test_snapshot_route(make_client):
    fake = Recorder({"snapshots": []})
    r = make_client(fake).post("/sales-reports/forecast-snapshots", json={"replace": True})
    assert r.status_code == 201 and fake.calls == [("take_forecast_snapshot", (ME, None, True), {})]
