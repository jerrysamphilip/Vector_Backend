"""A6 service layer: the report services over in-memory fake repositories and a fake ReportAccess.
Checks the rules the services own (scope, fallbacks, rates, CSV safety, permissions, sorting, the
duplicate-count cache, transactions); the SQL itself is covered by tests/test_a6_report_outputs.py."""
import csv
import io
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, NotFoundError, PermissionDenied, ValidationFailed
from app.models.sales import LEAD_STAGES
from app.repositories.reports import ComparisonRow, GlobalCounts, MailboxCounts, ProviderCounts
from app.repositories.reports_sales import OpportunityCounts
from app.schemas.reports_custom_schema import Definition, ReportWrite
from app.services import crm_kpis as kp
from app.services.campaign_analytics_service import CampaignAnalyticsService
from app.services.reports_custom_service import CustomReports
from app.services.reports_kpis_service import CrmKpis
from app.services.reports_sales_service import SalesReports, kpi_period
from app.services.reports_service import ReportsService, ViewerReports

ADMIN = SimpleNamespace(user_id="adm", tenant_id="t1", role="ADMIN", sales_level=None)
MANAGER = SimpleNamespace(user_id="mgr", tenant_id="t1", role="MANAGER", sales_level=2)
AGENT = SimpleNamespace(user_id="ag", tenant_id="t1", role="AGENT", sales_level=None)


class FakeAccess:
    def __init__(self, visible=None, amounts=True, everything=False):
        self.visible = visible
        self.amounts = amounts
        self.everything = everything
        self.masked_calls = []

    def visible_user_ids(self, user):
        return None if user.role in ("ADMIN", "SUPER_ADMIN") else self.visible

    def can_see_owner(self, user, owner_id):
        v = self.visible_user_ids(user)
        return v is None or owner_id in v

    def team_user_ids(self, user):
        return {user.user_id, "rep"} if user.user_id == "mgr" else {user.user_id}

    def can_manage_contacts(self, user):
        return user.role != "AGENT"

    def sees_everything(self, user):
        return self.everything or user.role in ("ADMIN", "SUPER_ADMIN")

    def can_see_amounts(self, user):
        return self.amounts

    def masked(self, user, data, keys=None):
        self.masked_calls.append(keys)
        return data

    def mask_money(self, data, keys):
        return data

    def user_names(self, ids):
        return {i: i.upper() for i in ids}

    def stage_probabilities(self, tenant_id):
        return {"s50": 50}


# ── ReportsService ──

class FakeCampaignRepo:
    def __init__(self):
        self.calls = []

    def global_counts(self, tenant_id, owners, window):
        self.calls.append(("global", tenant_id, owners, window))
        return GlobalCounts(sent=10, replied=1, bounced=1, opened=2, clicked=1, opened_prospects=4,
                            replied_prospects=3, leads_contacted=7, active_campaigns=2)

    def daily_counts(self, tenant_id, owners, window):
        return {window.start: 10}, {}, {}, {}

    def comparison_page(self, tenant_id, owners, window, sort, paging):
        self.calls.append(("comparison", owners, sort, paging))
        return [ComparisonRow("c1", "=SUM(A1)", "ACTIVE", datetime(2025, 5, 1), 4, 1, 0, 1),
                ComparisonRow("c2", "Plain", "DRAFT", None, 0, 0, 0, 0)], 2

    def prospect_status_counts(self, ids):
        return {"c1": {"OPENED": 2, "REPLIED": 1}}

    def provider_counts(self, tenant_id, owners, window):
        return ProviderCounts({"gmail": 4}, {"gmail": {4: 1, 5: 5}, "ses": {4: 1}}, {"gmail": 2})

    def mailbox_counts(self, tenant_id, owners):
        return MailboxCounts([("a@x", 1, 1), ("+b@x", 5, None)], {"+b@x": {4: 2}}, {"+b@x": 3})

    def user_name(self, tenant_id, user_id):
        return ("Rep", None, "rep@x") if user_id == "rep" else None

    def campaign_owner(self, tenant_id, campaign_id):
        return (campaign_id == "c1", "rep")

    def prospect_export(self, campaign_id):
        return SimpleNamespace(rows=[SimpleNamespace(email="@evil", first_name=None, last_name="L", company_name=None,
                                                     designation=None, status=None, current_step=None,
                                                     prospect_id="p1")],
                               engagement={"p1": {"sent": 2}}, events={"p1": {2: 3, 3: 1}})


def test_global_kpis_apply_status_fallbacks_and_rates():
    svc = ReportsService(FakeCampaignRepo())
    k = svc.get_global_analytics("t1", date(2025, 5, 1), date(2025, 5, 2), user_id="u1").kpis
    # open / reply rates over delivered: 10 sent - 1 bounced = 9 (UAT 3-5; was 40.0 / 30.0 over sent)
    assert (k.total_opened, k.total_replied, k.overall_open_rate, k.overall_reply_rate) == (4, 3, 44.44, 33.33)
    assert k.overall_bounce_rate == 10.0


def test_comparison_fallback_rates_and_whitelisted_sort():
    repo = FakeCampaignRepo()
    page = ReportsService(repo).get_campaign_comparison("t1", sort_by="nope", sort_dir="asc", page=2, page_size=5)
    first = page.items[0]
    # open rate over delivered (4 sent - 1 bounced = 3; was 75.0 over sent), bounce rate over sent
    assert (first.opened_count, first.replied_count, first.open_rate, first.bounce_rate) == (3, 1, 100.0, 25.0)
    _, owners, sort, paging = repo.calls[-1]
    assert sort.field == "created_at" and not sort.descending and (paging.offset, paging.limit) == (5, 5)


def test_provider_performance_lists_every_provider_sorted():
    providers = ReportsService(FakeCampaignRepo()).get_provider_performance("t1", date(2025, 5, 1),
                                                                            date(2025, 5, 2)).providers
    assert [(p.provider, p.sent_count, p.delivered_count, p.opened_count) for p in providers] == [
        ("gmail", 4, 0, 2), ("ses", 0, 0, 0)]


def test_mailbox_health_sorted_by_sent():
    items = ReportsService(FakeCampaignRepo()).get_mailbox_health("t1")
    assert [(m.mailbox, m.lead_contacted, m.replied) for m in items] == [("+b@x", 0, 2), ("a@x", 1, 0)]


def _csv_rows(buf):
    return list(csv.reader(io.StringIO(buf.getvalue())))


def test_csv_exports_neutralise_formulas_but_keep_headers():
    svc = ReportsService(FakeCampaignRepo())
    rows = _csv_rows(svc.export_campaign_comparison_csv("t1"))
    assert rows[0][0] == "Campaign Name" and rows[1][0] == "'=SUM(A1)" and rows[2][0] == "Plain"
    assert _csv_rows(svc.export_deliverability_csv("t1"))[1][0] == "'+b@x"
    prospects = _csv_rows(svc.export_prospects_csv("c1"))
    assert prospects[1] == ["'@evil", "", "L", "", "", "", "0", "2", "3", "1", "0", "0"]
    analytics = _csv_rows(svc.export_global_analytics_csv("t1", date(2025, 5, 1), date(2025, 5, 1), "rep"))
    assert analytics[0] == ["=== OVERALL SUMMARY ==="] and analytics[2] == ["Filtered By", "Rep"]


def test_team_scope_labels_all_users():
    svc = ReportsService(FakeCampaignRepo())
    rows = _csv_rows(svc.export_global_analytics_csv("t1", date(2025, 5, 1), date(2025, 5, 1), ["a", "b"]))
    assert rows[2] == ["Filtered By", "All Users"]


def test_scope_and_prospect_export_visibility():
    svc = ReportsService(FakeCampaignRepo(), FakeAccess(visible={"mgr", "rep"}))
    assert svc.scope(AGENT, "x") == "ag"
    assert svc.scope(MANAGER, None) == ["mgr", "rep"]
    with pytest.raises(PermissionDenied, match="outside your team"):
        svc.scope(MANAGER, "zed")
    assert svc.export_prospects_csv_for(MANAGER, "c1")
    with pytest.raises(NotFoundError):
        svc.export_prospects_csv_for(MANAGER, "other-tenant")
    hidden = ReportsService(FakeCampaignRepo(), FakeAccess(visible={"mgr"}))
    with pytest.raises(NotFoundError):
        hidden.export_prospects_csv_for(MANAGER, "c1")


def test_viewer_reports_default_window_and_file_names():
    repo = FakeCampaignRepo()
    viewer = ViewerReports(ReportsService(repo, FakeAccess()), today=lambda: date(2025, 5, 31))
    viewer.global_analytics(ADMIN, None, None, "daily", "rep")
    _, tenant, owners, window = repo.calls[-1]
    assert (tenant, owners, window.start, window.end) == ("t1", "rep", date(2025, 5, 2), date(2025, 5, 31))
    f = viewer.global_analytics_csv(ADMIN, None, None, "rep")
    assert f.filename == "analytics_2025-05-02_2025-05-31_rep.csv"
    assert viewer.campaign_comparison_csv(ADMIN, "a/b..c").filename == "campaign_comparison_abc.csv"
    assert viewer.prospects_csv(ADMIN, "c1").filename == "prospects_c1.csv"


# ── CampaignAnalyticsService ──

class FakeMetricsRepo:
    def sent_count(self, cid):
        return 5

    def unique_openers(self, cid):
        return 1

    def event_counts(self, cid):
        return {4: 1, 5: 2, 8: 1}

    def prospect_status_count(self, cid, statuses):
        return 3 if "OPENED" in statuses else 2

    def bulk_sent(self, ids):
        return {"a": 5}

    def bulk_openers(self, ids):
        return {"a": 1}

    def bulk_event_counts(self, ids):
        return [("a", 4, 1), ("a", 6, 2), ("zz", 4, 9)]

    def bulk_prospect_status_counts(self, ids, statuses):
        return [("a", {("OPENED", "REPLIED"): 3, ("REPLIED",): 2, ("ACTIVE", "OPENED"): 4}[tuple(statuses)])]


def test_campaign_metrics_fallbacks():
    svc = CampaignAnalyticsService(repository=FakeMetricsRepo())
    m = svc.get_dynamic_metrics("a")
    assert list(m) == ["sent_count", "opened_count", "replied_count", "bounced_count", "sender_bounced_count",
                       "positive_replied_count", "ooo_count", "unsubscribed_count"]
    assert (m["opened_count"], m["replied_count"], m["bounced_count"], m["positive_replied_count"]) == (3, 2, 2, 1)
    bulk = svc.get_bulk_metrics(["a", "b"])
    assert bulk["a"]["opened_count"] == 3 and bulk["a"]["replied_count"] == 2
    assert bulk["a"]["unsubscribed_count"] == 2 and bulk["a"]["in_progress_count"] == 4
    assert bulk["b"]["sent_count"] == 0 and "zz" not in bulk
    assert svc.get_bulk_metrics([]) == {}
    with pytest.raises(ValueError):
        CampaignAnalyticsService()


# ── SalesReports ──

class FakeSalesRepo:
    def __init__(self):
        self.calls = []

    def tenant_user(self, tenant_id, user_id):
        return SimpleNamespace(user_id=user_id) if user_id in ("rep", "zed") else None

    def stage_probabilities(self, tenant_id):
        return {"s50": 50}

    def opportunity_counts(self, tenant_id, owners, start, end):
        self.calls.append(("counts", owners))
        return OpportunityCounts(new_leads=4, sqls=2, open_sqls=1, opportunities_created=3, won_count=2,
                                 won_amount=300, lost_count=2, open_by_stage=[("s50", 2, 1000), ("s0", 1, 10)])

    def email_counts(self, tenant_id, owners, start, end):
        return 10, 2

    def roi_campaigns(self, tenant_id, limit):
        return [SimpleNamespace(campaign_id="c1", campaign_name="C1", status="ACTIVE"),
                SimpleNamespace(campaign_id="c2", campaign_name="C2", status="ACTIVE"),
                SimpleNamespace(campaign_id="c3", campaign_name="Empty", status="ACTIVE")]

    def roi_messages(self, ids, owners, start, end):
        return [("c1", "OUTBOUND", 10), ("c1", "INBOUND", 2), ("c2", "OUTBOUND", 5)]

    def roi_leads(self, ids, owners, start, end):
        return [("c1", 2, 1)]

    def roi_deals(self, ids, owners, start, end):
        return [("c1", "WON", "s0", 2, 100), ("c2", "WON", "s0", 1, 900), ("c2", "OPEN", "s50", 1, 100)]

    def qualified_leads(self, tenant_id, owners, start, end):
        q = datetime(2025, 5, 1)
        return [SimpleNamespace(lead_id="l1", owner_id="rep", opportunity_id="o9", qualified_at=q),
                SimpleNamespace(lead_id="l2", owner_id="rep", opportunity_id=None, qualified_at=q),
                SimpleNamespace(lead_id="l3", owner_id="mgr", opportunity_id=None, qualified_at=q)]

    def deals_of_leads(self, tenant_id, lead_ids, opp_ids):
        self.calls.append(("deals", list(lead_ids), list(opp_ids)))
        return [SimpleNamespace(opportunity_id="o9", lead_id=None, status="WON", amount=50,
                                closed_at=datetime(2025, 5, 3)),
                SimpleNamespace(opportunity_id="o2", lead_id="l2", status="LOST", amount=10, closed_at=None)]


def _sales(**access):
    return SalesReports(FakeSalesRepo(), FakeAccess(**access), today=lambda: date(2025, 6, 30))


def test_member_filter_scopes_to_visible_team_or_404():
    s = _sales(visible={"mgr", "rep"})
    assert s.owners(MANAGER, None) == {"mgr", "rep"}
    assert s.owners(MANAGER, "rep") == {"rep"}
    with pytest.raises(NotFoundError, match="Team member not found"):
        s.owners(MANAGER, "zed")
    with pytest.raises(NotFoundError):
        s.owners(MANAGER, "ghost")


def test_funnel_caps_ratios_at_100_percent():
    out = _sales().funnel(ADMIN, date(2025, 5, 1), date(2025, 5, 31), None)
    steps = {st["key"]: st for st in out["stages"]}
    assert steps["replied"]["from_previous"] == 20.0 and steps["lead"]["from_previous"] is None
    assert out["won_amount"] == 300.0


def test_reversed_period_is_400_and_quarter_needs_fy():
    with pytest.raises(BadRequestError, match="date_from is after date_to"):
        _sales().funnel(ADMIN, date(2025, 6, 1), date(2025, 5, 1), None)
    with pytest.raises(BadRequestError, match="quarter needs fy"):
        kpi_period(None, None, None, 2, date(2025, 1, 1))


def test_measures_weight_open_pipeline():
    m = _sales().measures("t1", None, kpi_period(None, None, None, None, date(2025, 6, 30)))
    assert (m["open_pipeline"], m["weighted_pipeline"], m["win_rate"], m["average_deal"]) == (1010.0, 500.0, 50.0, 150.0)


def test_campaign_roi_never_orders_by_hidden_revenue():
    shown = _sales().campaign_roi(ADMIN, None, None, None)
    assert [r["campaign_id"] for r in shown["rows"]] == ["c2", "c1"]
    assert shown["totals"]["revenue"] == 1000.0 and shown["period"] == {"from": None, "to": None}
    hidden = _sales(amounts=False).campaign_roi(ADMIN, None, None, None)
    assert [r["campaign_id"] for r in hidden["rows"]] == ["c1", "c2"]   # by won count (2 vs 1) instead


def test_sql_to_win_credits_linked_and_created_deals():
    s = _sales()
    data = s.sql_to_win_data(ADMIN, None, kpi_period(None, None, None, None, date(2025, 6, 30)))
    assert (data["sqls"], data["won"], data["lost"], data["no_deal"], data["with_deal"]) == (3, 1, 1, 1, 2)
    assert data["won_amount"] == 50.0 and data["average_days_sql_to_win"] == 2.0
    assert [r["owner_id"] for r in data["by_owner"]] == ["rep", "mgr"]
    assert s.repo.calls[-1] == ("deals", ["l1", "l2", "l3"], ["o9"])


class FakeUow:
    def __init__(self):
        self.commits = 0
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_forecast_snapshot_admin_only_and_committed():
    access = FakeAccess()
    access.take_snapshot = lambda tenant, period_type, by, replace: {"type": period_type, "by": by}
    uow = FakeUow()
    s = SalesReports(FakeSalesRepo(), access, uow)
    with pytest.raises(PermissionDenied):
        s.take_forecast_snapshot(MANAGER, None, False)
    out = s.take_forecast_snapshot(ADMIN, None, False)
    assert [r["type"] for r in out["snapshots"]] == ["MONTH", "QUARTER"] and uow.commits == 1


# ── CRM KPIs ──

class FakeKpiRepo:
    def __init__(self):
        self.dup_runs = 0
        self.fingerprint = (5, "x")

    def contacts_fingerprint(self, tenant_id, owners):
        return self.fingerprint

    def duplicate_counts(self, tenant_id, owners, labels, limit_ms):
        self.dup_runs += 1
        assert "gmail" in labels and limit_ms == kp.DUP_QUERY_LIMIT_MS
        return (1, 5)


def test_duplicate_count_is_cached_while_contacts_are_unchanged():
    kp._DUP_CACHE.clear()
    repo = FakeKpiRepo()
    assert kp.duplicate_counts(repo, "t-cache", None) == (1, 5)
    assert kp.duplicate_counts(repo, "t-cache", None) == (1, 5) and repo.dup_runs == 1
    repo.fingerprint = (6, "y")
    kp.duplicate_counts(repo, "t-cache", None)
    assert repo.dup_runs == 2


def test_workspace_kpis_are_withheld_from_team_viewers():
    sales = SalesReports(FakeSalesRepo(), FakeAccess(visible={"mgr"}), today=lambda: date(2025, 6, 30))
    kpis = CrmKpis(sales, FakeKpiRepo(), today=lambda: date(2025, 6, 30))
    dup = kpis._duplicates(MANAGER, {"mgr"}, None, workspace_view=False)
    assert dup["available"] is False and dup["value"] is None
    period = kpi_period(None, None, None, None, date(2025, 6, 30))
    adoption = kpis._adoption(MANAGER, {"mgr"}, None, period, workspace_view=False)
    assert adoption["available"] is False and adoption["note"].startswith("Workspace-level KPI")


# ── Custom reports ──

class FakeCustomRepo:
    def __init__(self, rows=None, report=None):
        self.rows = rows or []
        self.report = report
        self.runs = []
        self.added = []
        self.deleted = []

    def field_kinds(self, obj):
        return {}

    def run(self, tenant_id, obj, scope, filters, date_field, lower, upper, groups, measure):
        self.runs.append(dict(tenant=tenant_id, obj=obj, scope=scope, filters=filters, date_field=date_field,
                              lower=lower, upper=upper, groups=groups, measure=measure))
        return self.rows

    def stage_names(self, ids):
        return {i: "Stage " + i[-1] for i in ids}

    def campaign_names(self, ids):
        return {}

    def company_names(self, ids):
        return {}

    def get(self, tenant_id, report_id):
        return self.report if self.report and report_id == self.report.report_id else None

    def new_report(self, **values):
        return SimpleNamespace(report_id="r-new", updated_at=None, **values)

    def add(self, report):
        self.added.append(report)
        return report

    def delete(self, report):
        self.deleted.append(report)

    def visible_reports(self, tenant_id, user_id):
        return [self.report] if self.report else []


def test_custom_run_scopes_validates_and_shapes():
    repo = FakeCustomRepo(rows=[("SQL", 2), ("NEW", 5)])
    out = CustomReports(repo, FakeAccess(visible={"mgr", "rep"})).run_adhoc(MANAGER, Definition(
        object="leads", group_by="stage", filters=[{"field": "owner", "value": "me"}], date_field="created_at",
        date_from=date(2025, 5, 1)))
    assert [(r["key"], r["label"]) for r in out["rows"]] == [("NEW", LEAD_STAGES["NEW"][0]),
                                                             ("SQL", LEAD_STAGES["SQL"][0])]
    run = repo.runs[0]
    assert run["scope"].owner_ids == frozenset({"mgr", "rep"}) and run["scope"].include_unassigned
    assert run["filters"][0].values == ("mgr",) and run["lower"] == datetime(2025, 5, 1) and run["upper"] is None
    assert out["total"] == 7.0 and out["money"] is False


def test_custom_errors_map_to_http_statuses():
    svc = CustomReports(FakeCustomRepo(), FakeAccess(amounts=False))
    with pytest.raises(PermissionDenied, match="Only managers"):
        svc.run_adhoc(AGENT, Definition(object="leads", group_by="stage"))
    with pytest.raises(BadRequestError, match="object must be one of"):
        svc.run_adhoc(ADMIN, Definition(object="users", group_by="email"))
    with pytest.raises(PermissionDenied, match="cannot see amounts"):
        svc.run_adhoc(ADMIN, Definition(object="deals", group_by="stage", measure="sum_amount"))
    with pytest.raises(ValidationFailed, match="list of values"):
        svc.run_adhoc(ADMIN, Definition(object="leads", group_by="stage",
                                        filters=[{"field": "stage", "operator": "contains", "value": ["a"]}]))


def test_saved_report_lifecycle_and_edit_rights():
    report = SimpleNamespace(report_id="r1", name="R", description=None, definition={"object": "leads",
                             "group_by": "stage"}, shared=True, owner_id="mgr", updated_at=None)
    repo, uow = FakeCustomRepo(report=report), FakeUow()
    svc = CustomReports(repo, FakeAccess(), uow)
    assert svc.run_saved(AGENT, "r1")["can_edit"] is False
    with pytest.raises(PermissionDenied, match="owner or an admin"):
        svc.update(AGENT, "r1", ReportWrite(name="x"))
    with pytest.raises(NotFoundError):
        svc.run_saved(AGENT, "nope")
    assert svc.update(MANAGER, "r1", ReportWrite(name="  ", shared=False))["name"] == "R" and report.shared is False
    with pytest.raises(BadRequestError, match="name and a definition"):
        svc.create(MANAGER, ReportWrite(name=" "))
    created = svc.create(MANAGER, ReportWrite(name=" New ", definition=Definition(object="tasks", group_by="status")))
    assert created["name"] == "New" and repo.added[0].definition["object"] == "tasks"
    with pytest.raises(NotFoundError):   # private now: not even an admin sees another user's report
        svc.delete(ADMIN, "r1")
    assert svc.delete(MANAGER, "r1") == {"status": "deleted", "report_id": "r1"} and repo.deleted == [report]
    assert uow.commits == 3
