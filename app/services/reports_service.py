# app/services/reports_service.py
"""
Reports Service — Global Analytics, Campaign Comparison, Provider Performance, Mailbox Health,
optimisation metrics, team breakdown and the CSV / Word exports (GET /reports/*).

The rules live here: whose campaigns a report covers (``report_scope``), the prospect-status fallbacks
for opened / replied, rates, series and the export layouts. The SQL is in app/repositories/reports.py
(``CampaignReportRepository``); the Word layout in app/services/reports_docx.py. CSV cells pass
through ``csv_safe`` so spreadsheet apps never evaluate a campaign name or mailbox as a formula.
"""

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, List, Optional

from app.core.errors import NotFoundError, PermissionDenied
from app.domain.paging import Paging
from app.domain.reports import (
    ComparisonSort,
    DateWindow,
    OptionalWindow,
    Owners,
    ReportInputError,
    ReportViewer,
    build_timeseries,
    median_hours,
    rate,
    report_owner_scope,
    sees_owner,
)
from app.models.email_message import EmailEvent
from app.repositories.reports import (
    CampaignReportRepository,
    ComparisonRow,
    Session,
    SqlAlchemyCampaignReportRepository,
)
from app.schemas.reports_schema import (
    CampaignComparisonItem,
    CampaignComparisonResponse,
    CampaignOptimizationMetrics,
    CampaignStats,
    DashboardSummaryResponse,
    GlobalAnalyticsResponse,
    GlobalKPIs,
    LeadStats,
    MailboxHealthItem,
    MailboxStats,
    ProviderPerformanceItem,
    ProviderPerformanceResponse,
    TimeSeriesPoint,
    UserPerformanceItem,
)
from app.services import reports_docx
from app.services.crm import csv_safe_row
from app.services.reports_access import ReportAccess, SessionReportAccess
from app.core.ids import short_id

logger = logging.getLogger(__name__)

EXPORT_PAGE_SIZE = 10000   # exports list every campaign
CAMPAIGN_COLUMNS = ["Campaign Name", "Status", "Sent", "Opened", "Replied", "Bounced",
                    "Open Rate %", "Reply Rate %", "Bounce Rate %"]
MAILBOX_COLUMNS = ["Mailbox", "Leads Contacted", "Emails Sent", "Opened", "Open Rate %",
                   "Replied", "Reply Rate %", "Bounced", "Bounce Rate %"]


class _ReportPaging(Paging):
    """The comparison pages through at most 100 campaigns per API call (the router's limit); exports
    and the dashboard ask for everything at once."""

    MAX_PAGE_SIZE = EXPORT_PAGE_SIZE


def report_scope(access: ReportAccess, viewer: ReportViewer, user_id: Optional[str]) -> Owners:
    """Whose campaigns a report may cover: agents themselves, admins anyone, managers their own team
    (a single member on request; another team's member is refused)."""
    visible = None if viewer.role == "AGENT" else access.visible_user_ids(viewer)
    try:
        return report_owner_scope(viewer.role, viewer.user_id, visible, user_id)
    except ReportInputError as exc:
        raise PermissionDenied(exc.message) from None


class _Fixed(list):
    """A row of the export's own text (section markers, headers): written as is. Every other row holds
    data and goes through ``csv_safe_row``."""


def _csv(rows: Iterable[list]) -> io.StringIO:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row if isinstance(row, _Fixed) else csv_safe_row(row))
    buf.seek(0)
    return buf


def _iso(value: Any) -> str:
    return value.isoformat() if value else ""


def _user_label(row: Optional[tuple]) -> Optional[str]:
    if not row:
        return None
    first, last, email = row
    return f"{first or ''} {last or ''}".strip() or email


class ReportsService:
    def __init__(self, repository: CampaignReportRepository, access: Optional[ReportAccess] = None) -> None:
        self.repo = repository
        self.access = access

    @classmethod
    def for_session(cls, db: Session) -> "ReportsService":
        return cls(SqlAlchemyCampaignReportRepository(db), SessionReportAccess(db))

    def _access(self) -> ReportAccess:
        if self.access is None:
            raise RuntimeError("ReportsService needs a ReportAccess for viewer scoping")
        return self.access

    def scope(self, viewer: ReportViewer, user_id: Optional[str]) -> Owners:
        return report_scope(self._access(), viewer, user_id)

    # ------------------------------------------------------------------
    # GLOBAL ANALYTICS
    # ------------------------------------------------------------------

    def get_global_analytics(self, tenant_id: str, start_date: date, end_date: date, granularity: str = "daily",
                             user_id: Owners = None) -> GlobalAnalyticsResponse:
        window = DateWindow(start_date, end_date)
        return GlobalAnalyticsResponse(kpis=self._global_kpis(tenant_id, window, user_id),
                                       timeseries=self._timeseries(tenant_id, window, granularity, user_id))

    def _global_kpis(self, tenant_id: str, window: DateWindow, owners: Owners) -> GlobalKPIs:
        c = self.repo.global_counts(tenant_id, owners, window)
        # Without open events, prospects with OPENED / REPLIED status count (a reply implies an open)
        opened = max(c.opened, c.opened_prospects)
        replied = max(c.replied, c.replied_prospects)
        return GlobalKPIs(
            total_leads_contacted=c.leads_contacted, total_emails_sent=c.sent, total_opened=opened,
            total_replied=replied, total_bounced=c.bounced, total_clicked=c.clicked,
            overall_open_rate=rate(opened, c.sent), overall_reply_rate=rate(replied, c.sent),
            overall_bounce_rate=rate(c.bounced, c.sent), active_campaigns=c.active_campaigns)

    def _timeseries(self, tenant_id: str, window: DateWindow, granularity: str, owners: Owners) -> list:
        sent, opened, replied, bounced = self.repo.daily_counts(tenant_id, owners, window)
        return [TimeSeriesPoint(date=p.date, sent=p.sent, opened=p.opened, replied=p.replied, bounced=p.bounced)
                for p in build_timeseries(window, sent, opened, replied, bounced, granularity)]

    # ------------------------------------------------------------------
    # CAMPAIGN COMPARISON
    # ------------------------------------------------------------------

    def get_campaign_comparison(self, tenant_id: str, sort_by: str = "open_rate", sort_dir: str = "desc",
                                page: int = 1, page_size: int = 20, start_date: Optional[date] = None,
                                end_date: Optional[date] = None, user_id: Owners = None) -> CampaignComparisonResponse:
        """All campaigns with metrics, sortable and paginated."""
        paging = _ReportPaging(page, page_size)
        rows, total = self.repo.comparison_page(tenant_id, user_id, OptionalWindow(start_date, end_date),
                                                ComparisonSort.parse(sort_by, sort_dir), paging)
        fallbacks = self.repo.prospect_status_counts([r.campaign_id for r in rows])
        items = [self._comparison_item(r, fallbacks.get(r.campaign_id, {})) for r in rows]
        return CampaignComparisonResponse(items=items, total=total, page=page, page_size=page_size)

    @staticmethod
    def _comparison_item(r: ComparisonRow, fb: dict) -> CampaignComparisonItem:
        sent = r.sent_count
        opened = max(r.opened_count, fb.get("OPENED", 0) + fb.get("REPLIED", 0))
        replied = max(r.replied_count, fb.get("REPLIED", 0))
        return CampaignComparisonItem(
            campaign_id=r.campaign_id, campaign_name=r.campaign_name, status=r.status, sent_count=sent,
            opened_count=opened, replied_count=replied, bounced_count=r.bounced_count,
            open_rate=rate(opened, sent), reply_rate=rate(replied, sent), bounce_rate=rate(r.bounced_count, sent),
            created_at=r.created_at)

    # ------------------------------------------------------------------
    # PROVIDER PERFORMANCE
    # ------------------------------------------------------------------

    def get_provider_performance(self, tenant_id: str, start_date: date, end_date: date,
                                 user_id: Owners = None) -> ProviderPerformanceResponse:
        """Per-provider (Gmail/Outlook/SES) aggregate metrics."""
        c = self.repo.provider_counts(tenant_id, user_id, DateWindow(start_date, end_date))
        items = []
        for prov in sorted(set(c.sent) | set(c.events)):
            sent, events, opened = c.sent.get(prov, 0), c.events.get(prov, {}), c.openers.get(prov, 0)
            replied = events.get(EmailEvent.EVENT_REPLY, 0)
            bounced = events.get(EmailEvent.EVENT_BOUNCE, 0)
            items.append(ProviderPerformanceItem(
                provider=prov, sent_count=sent, delivered_count=max(sent - bounced, 0), opened_count=opened,
                replied_count=replied, bounced_count=bounced, open_rate=rate(opened, sent),
                reply_rate=rate(replied, sent), bounce_rate=rate(bounced, sent)))
        return ProviderPerformanceResponse(providers=items)

    # ------------------------------------------------------------------
    # LEADS, MAILBOXES, CAMPAIGNS
    # ------------------------------------------------------------------

    def get_lead_stats(self, tenant_id: str, user_id: Owners = None) -> LeadStats:
        """New leads (step 1) vs follow-up leads (step > 1)."""
        total, new_leads = self.repo.lead_counts(tenant_id, user_id)
        return LeadStats(total_leads_contacted=total, new_leads_reached=new_leads, follow_up_leads=total - new_leads)

    def get_mailbox_stats(self, tenant_id: str) -> MailboxStats:
        """Mailbox connection statistics."""
        inboxes = self.repo.mailbox_states(tenant_id)
        return MailboxStats(total_connected=len(inboxes),
                            mailbox_in_use=sum(1 for status, _ in inboxes if status == "ACTIVE"),
                            disconnected=sum(1 for status, _ in inboxes if status == "PAUSED"),
                            without_warmup=sum(1 for _, warmup in inboxes if not warmup))

    def get_campaign_stats(self, tenant_id: str, user_id: Owners = None) -> CampaignStats:
        """Campaign status breakdown."""
        counts = self.repo.campaign_status_counts(tenant_id, user_id)
        return CampaignStats(total_campaigns=sum(counts.values()), active=counts.get("ACTIVE", 0),
                             paused=counts.get("PAUSED", 0), drafted=counts.get("DRAFT", 0),
                             completed=counts.get("COMPLETED", 0))

    def get_mailbox_health(self, tenant_id: str, user_id: Owners = None) -> list:
        """Per-mailbox email metrics (optionally scoped to a specific user's campaigns)."""
        c = self.repo.mailbox_counts(tenant_id, user_id)
        items = []
        for mailbox, sent, leads in c.sent:
            sent = sent or 0
            events, opened = c.events.get(mailbox, {}), c.openers.get(mailbox, 0)
            replied = events.get(EmailEvent.EVENT_REPLY, 0)
            bounced = events.get(EmailEvent.EVENT_BOUNCE, 0)
            items.append(MailboxHealthItem(
                mailbox=mailbox, lead_contacted=leads or 0, email_sent=sent, opened=opened,
                opened_rate=rate(opened, sent), replied=replied, replied_rate=rate(replied, sent), bounced=bounced,
                bounce_rate=rate(bounced, sent)))
        return sorted(items, key=lambda x: x.email_sent, reverse=True)

    def get_optimization_metrics(self, tenant_id: str, user_id: Owners = None) -> CampaignOptimizationMetrics:
        """Avg prospects per campaign with a reply, follow-up reply rate, median time to first reply."""
        c = self.repo.optimization_counts(tenant_id, user_id)
        avg_leads = round(c.total_prospects / c.campaigns_with_replies, 1) if c.campaigns_with_replies > 0 else 0.0
        follow_up_rate = rate(c.follow_up_replies, c.follow_up_sent)
        try:
            median = median_hours(self.repo.first_reply_times(tenant_id, user_id))
        except Exception:  # optional metric: report 0 rather than failing the whole report
            logger.warning("Median time-to-reply query failed", exc_info=True)
            median = 0.0
        return CampaignOptimizationMetrics(avg_leads_before_first_reply=avg_leads,
                                           follow_up_reply_rate=follow_up_rate,
                                           median_time_to_first_reply_hours=round(median, 1))

    def get_user_breakdown(self, tenant_id: str, start_date: date, end_date: date, user_ids: Owners = None) -> list:
        """Per-user campaign performance summary for team analytics (user_ids: limit to a team)."""
        window = DateWindow(start_date, end_date)
        results = []
        for u in self.repo.breakdown_users(tenant_id, user_ids):
            c = self.repo.user_counts(tenant_id, u.user_id, window)
            results.append(UserPerformanceItem(
                user_id=u.user_id, first_name=u.first_name, last_name=u.last_name, email=u.email, role=u.role,
                campaigns_created=c.campaigns, total_sent=c.sent, total_opened=c.opened, total_replied=c.replied,
                total_bounced=c.bounced, open_rate=rate(c.opened, c.sent), reply_rate=rate(c.replied, c.sent),
                bounce_rate=rate(c.bounced, c.sent)))
        return results

    # ------------------------------------------------------------------
    # FULL DASHBOARD SUMMARY
    # ------------------------------------------------------------------

    def get_dashboard_summary(self, tenant_id: str, start_date: date, end_date: date, granularity: str = "daily",
                              user_id: Owners = None) -> DashboardSummaryResponse:
        """One call for the full Smartlead-style dashboard."""
        analytics = self.get_global_analytics(tenant_id, start_date, end_date, granularity, user_id)
        top_campaigns = self.get_campaign_comparison(tenant_id, sort_by="sent_count", sort_dir="desc", page_size=10,
                                                     start_date=start_date, end_date=end_date, user_id=user_id)
        # User breakdown only for org-wide view (not when filtering to one user)
        user_breakdown = [] if isinstance(user_id, str) else self.get_user_breakdown(tenant_id, start_date, end_date,
                                                                                   user_id)
        return DashboardSummaryResponse(
            kpis=analytics.kpis, timeseries=analytics.timeseries,
            lead_stats=self.get_lead_stats(tenant_id, user_id),
            campaign_stats=self.get_campaign_stats(tenant_id, user_id),
            mailbox_stats=self.get_mailbox_stats(tenant_id),
            providers=self.get_provider_performance(tenant_id, start_date, end_date, user_id).providers,
            mailbox_health=self.get_mailbox_health(tenant_id, user_id),
            top_campaigns=top_campaigns.items,
            optimization=self.get_optimization_metrics(tenant_id, user_id),
            user_breakdown=user_breakdown,
        )

    # ------------------------------------------------------------------
    # EXPORTS
    # ------------------------------------------------------------------

    def _all_campaigns(self, tenant_id: str, user_id: Owners, start_date: Optional[date] = None,
                       end_date: Optional[date] = None, sort_by: str = "open_rate") -> List[CampaignComparisonItem]:
        return self.get_campaign_comparison(tenant_id, sort_by=sort_by, sort_dir="desc", page_size=EXPORT_PAGE_SIZE,
                                            start_date=start_date, end_date=end_date, user_id=user_id).items

    def _user_label(self, tenant_id: str, user_id: Owners) -> str:
        label = _user_label(self.repo.user_name(tenant_id, user_id)) if isinstance(user_id, str) and user_id else None
        return label or "All Users"

    def export_global_analytics_csv(self, tenant_id: str, start_date: date, end_date: date,
                                    user_id: Owners = None) -> io.StringIO:
        """CSV with three sections: overall KPI summary, daily time-series, and per-campaign breakdown."""
        analytics = self.get_global_analytics(tenant_id, start_date, end_date, user_id=user_id)
        campaigns = self._all_campaigns(tenant_id, user_id, start_date, end_date, sort_by="sent_count")
        k = analytics.kpis
        rows: List[list] = [
            _Fixed(["=== OVERALL SUMMARY ==="]),
            ["Period", f"{start_date} to {end_date}"],
            ["Filtered By", self._user_label(tenant_id, user_id)],
            *[[label, value] for label, value in _kpi_rows(k)],
            [],
            _Fixed(["=== DAILY TIME-SERIES ==="]),
            _Fixed(["Date", "Sent", "Opened", "Replied", "Bounced"]),
            *[[pt.date, pt.sent, pt.opened, pt.replied, pt.bounced] for pt in analytics.timeseries],
            [],
            _Fixed(["=== PER-CAMPAIGN BREAKDOWN ==="]),
            _Fixed([*CAMPAIGN_COLUMNS, "Created At"]),
            *[_campaign_row(c, str, _iso) for c in campaigns],
        ]
        return _csv(rows)

    def export_campaign_comparison_csv(self, tenant_id: str, user_id: Owners = None) -> io.StringIO:
        """CSV of all campaigns with metrics (optionally scoped to a specific user)."""
        rows: List[list] = [_Fixed([*CAMPAIGN_COLUMNS, "Created At"])]
        rows += [_campaign_row(c, str, _iso) for c in self._all_campaigns(tenant_id, user_id)]
        return _csv(rows)

    def export_prospects_csv_for(self, viewer: ReportViewer, campaign_id: str) -> io.StringIO:
        """The campaign's prospects, if the campaign is the viewer's tenant's and its creator is visible."""
        exists, owner = self.repo.campaign_owner(viewer.tenant_id, campaign_id)
        if not exists or not sees_owner(self._access().visible_user_ids(viewer), owner):
            raise NotFoundError("Campaign not found")
        return self.export_prospects_csv(campaign_id)

    def export_prospects_csv(self, campaign_id: str) -> io.StringIO:
        """CSV of prospects enrolled in a campaign with engagement data."""
        data = self.repo.prospect_export(campaign_id)
        rows: List[list] = [_Fixed(["Email", "First Name", "Last Name", "Company", "Designation", "Status",
                                    "Current Step", "Emails Sent", "Opens", "Clicks", "Replies", "Bounces"])]
        for r in data.rows:
            events = data.events.get(r.prospect_id, {})
            rows.append([
                r.email, r.first_name or "", r.last_name or "", r.company_name or "", r.designation or "",
                r.status or "", r.current_step or 0, data.engagement.get(r.prospect_id, {}).get("sent", 0),
                events.get(EmailEvent.EVENT_OPEN, 0), events.get(EmailEvent.EVENT_CLICK, 0),
                events.get(EmailEvent.EVENT_REPLY, 0), events.get(EmailEvent.EVENT_BOUNCE, 0),
            ])
        return _csv(rows)

    def export_deliverability_csv(self, tenant_id: str) -> io.StringIO:
        """CSV of per-mailbox email health metrics for the tenant."""
        rows: List[list] = [_Fixed(MAILBOX_COLUMNS)]
        rows += [_mailbox_row(m, str) for m in self.get_mailbox_health(tenant_id)]
        return _csv(rows)

    def export_global_analytics_docx(self, tenant_id: str, start_date: date, end_date: date,
                                     user_id: Owners = None) -> io.BytesIO:
        """Full analytics report as a Word document (layout: app/services/reports_docx.py)."""
        analytics = self.get_global_analytics(tenant_id, start_date, end_date, user_id=user_id)
        campaigns = self._all_campaigns(tenant_id, user_id, start_date, end_date, sort_by="sent_count")
        return reports_docx.analytics_report(
            start_date=start_date, end_date=end_date, user_label=self._user_label(tenant_id, user_id),
            kpi_rows=_kpi_rows(analytics.kpis),
            timeseries=[[pt.date, pt.sent, pt.opened, pt.replied, pt.bounced] for pt in analytics.timeseries],
            campaigns=[_campaign_row(c, _percent, lambda d: d.strftime("%Y-%m-%d") if d else "") for c in campaigns],
            mailboxes=[_mailbox_row(m, _percent) for m in self.get_mailbox_health(tenant_id, user_id)],
            providers=[[p.provider, p.sent_count, p.opened_count, p.replied_count, p.bounced_count,
                        f"{p.open_rate}%", f"{p.reply_rate}%", f"{p.bounce_rate}%"]
                       for p in self.get_provider_performance(tenant_id, start_date, end_date, user_id=user_id).providers],
        )


def _percent(value: float) -> str:
    return f"{value}%"


def _kpi_rows(k: GlobalKPIs) -> List[tuple]:
    return [
        ("Active Campaigns", k.active_campaigns), ("Total Leads Contacted", k.total_leads_contacted),
        ("Total Emails Sent", k.total_emails_sent), ("Total Opened", k.total_opened),
        ("Total Replied", k.total_replied), ("Total Bounced", k.total_bounced), ("Total Clicked", k.total_clicked),
        ("Overall Open Rate", f"{k.overall_open_rate}%"), ("Overall Reply Rate", f"{k.overall_reply_rate}%"),
        ("Overall Bounce Rate", f"{k.overall_bounce_rate}%"),
    ]


def _campaign_row(c: CampaignComparisonItem, fmt_rate: Callable[[float], Any], fmt_date: Callable[[Any], str]) -> list:
    return [c.campaign_name, c.status, c.sent_count, c.opened_count, c.replied_count, c.bounced_count,
            _keep(fmt_rate, c.open_rate), _keep(fmt_rate, c.reply_rate), _keep(fmt_rate, c.bounce_rate),
            fmt_date(c.created_at)]


def _mailbox_row(m: MailboxHealthItem, fmt_rate: Callable[[float], Any]) -> list:
    return [m.mailbox, m.lead_contacted, m.email_sent, m.opened, _keep(fmt_rate, m.opened_rate), m.replied,
            _keep(fmt_rate, m.replied_rate), m.bounced, _keep(fmt_rate, m.bounce_rate)]


def _keep(fmt: Callable[[float], Any], value: float) -> Any:
    """CSV keeps the number itself (``str`` marks that); Word shows it with a % sign."""
    return value if fmt is str else fmt(value)


# ── Per-viewer entry points (one call per route) ──────────────

@dataclass(frozen=True)
class ReportFile:
    content: Any
    filename: str


def _owner_suffix(owners: Owners) -> str:
    return "_" + short_id(owners) if isinstance(owners, str) else ""


class ViewerReports:
    """The /reports routes: default period, the viewer's scope, then the report."""

    def __init__(self, reports: ReportsService, today: Callable[[], date] = date.today) -> None:
        self.reports = reports
        self._today = today

    @classmethod
    def for_session(cls, db: Session) -> "ViewerReports":
        return cls(ReportsService.for_session(db))

    def _window(self, start: Optional[date], end: Optional[date]) -> DateWindow:
        return DateWindow.or_default(start, end, self._today())

    def dashboard_summary(self, viewer: ReportViewer, start_date: Optional[date], end_date: Optional[date],
                          granularity: str, user_id: Optional[str]) -> DashboardSummaryResponse:
        w = self._window(start_date, end_date)
        owners = self.reports.scope(viewer, user_id)   # AGENTs always see only their own data
        return self.reports.get_dashboard_summary(viewer.tenant_id, w.start, w.end, granularity, owners)

    def global_analytics(self, viewer: ReportViewer, start_date: Optional[date], end_date: Optional[date],
                         granularity: str, user_id: Optional[str]) -> GlobalAnalyticsResponse:
        w = self._window(start_date, end_date)
        owners = self.reports.scope(viewer, user_id)
        return self.reports.get_global_analytics(viewer.tenant_id, w.start, w.end, granularity, owners)

    def campaign_comparison(self, viewer: ReportViewer, start_date: Optional[date], end_date: Optional[date],
                            sort_by: str, sort_dir: str, page: int, page_size: int,
                            user_id: Optional[str]) -> CampaignComparisonResponse:
        owners = self.reports.scope(viewer, user_id)
        return self.reports.get_campaign_comparison(viewer.tenant_id, sort_by=sort_by, sort_dir=sort_dir, page=page,
                                                    page_size=page_size, start_date=start_date, end_date=end_date,
                                                    user_id=owners)

    def provider_performance(self, viewer: ReportViewer, start_date: Optional[date],
                             end_date: Optional[date]) -> ProviderPerformanceResponse:
        w = self._window(start_date, end_date)
        return self.reports.get_provider_performance(viewer.tenant_id, w.start, w.end, self.reports.scope(viewer, None))

    def global_analytics_csv(self, viewer: ReportViewer, start_date: Optional[date], end_date: Optional[date],
                             user_id: Optional[str]) -> ReportFile:
        w = self._window(start_date, end_date)
        owners = self.reports.scope(viewer, user_id)
        buf = self.reports.export_global_analytics_csv(viewer.tenant_id, w.start, w.end, owners)
        return ReportFile(buf, f"analytics_{w.start}_{w.end}{_owner_suffix(owners)}.csv")

    def global_analytics_docx(self, viewer: ReportViewer, start_date: Optional[date], end_date: Optional[date],
                              user_id: Optional[str]) -> ReportFile:
        w = self._window(start_date, end_date)
        owners = self.reports.scope(viewer, user_id)
        buf = self.reports.export_global_analytics_docx(viewer.tenant_id, w.start, w.end, owners)
        return ReportFile(buf, f"analytics_report_{w.start}_{w.end}{_owner_suffix(owners)}.docx")

    def campaign_comparison_csv(self, viewer: ReportViewer, user_id: Optional[str]) -> ReportFile:
        owners = self.reports.scope(viewer, user_id)
        buf = self.reports.export_campaign_comparison_csv(viewer.tenant_id, user_id=owners)
        suffix = "".join(c for c in short_id(owners) if c.isalnum() or c in "-_") if isinstance(owners, str) else ""
        return ReportFile(buf, f"campaign_comparison{'_' + suffix if suffix else ''}.csv")

    def prospects_csv(self, viewer: ReportViewer, campaign_id: str) -> ReportFile:
        return ReportFile(self.reports.export_prospects_csv_for(viewer, campaign_id), f"prospects_{short_id(campaign_id)}.csv")

    def deliverability_csv(self, viewer: ReportViewer) -> ReportFile:
        return ReportFile(self.reports.export_deliverability_csv(viewer.tenant_id), "mailbox_health_report.csv")
