# app/routers/reports_router.py
"""
Reports API — Global Analytics, Campaign Comparison, Provider Performance, CSV Exports.

Thin routes (ADR 0009): each parses the request and calls one ``ViewerReports`` method
(app/services/reports_service.py), which applies the default period and the viewer's scope.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.domain.reports import ReportViewer
from app.schemas.reports_schema import (
    CampaignComparisonResponse,
    DashboardSummaryResponse,
    GlobalAnalyticsResponse,
    ProviderPerformanceResponse,
)
from app.services.reports_service import ReportFile, ViewerReports
from app.utils.http_files import content_disposition

router = APIRouter(prefix="/reports", tags=["Reports"])

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
any_viewer = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")
exporter = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def get_reports_service(db=Depends(get_db)) -> ViewerReports:
    return ViewerReports.for_session(db)


def _download(file: ReportFile, media_type: str = "text/csv") -> StreamingResponse:
    return StreamingResponse(file.content, media_type=media_type,
                             headers={"Content-Disposition": content_disposition(file.filename)})


# =============================
# DASHBOARD SUMMARY (single call)
# =============================

@router.get("/dashboard-summary", response_model=DashboardSummaryResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_dashboard_summary(
    start_date: date = Query(None, description="Start date (defaults to 30 days ago)"),
    end_date: date = Query(None, description="End date (defaults to today)"),
    granularity: str = Query("daily", description="daily or weekly"),
    user_id: str = Query(None, description="Filter by specific user (SUPER_ADMIN/ADMIN/MANAGER only)"),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(any_viewer),
):
    """
    Full dashboard summary — KPIs, timeseries, lead stats, campaign stats,
    mailbox stats, provider performance, mailbox health, top campaigns, optimization metrics.
    Single API call for the entire Reports page.
    """
    return service.dashboard_summary(current_user, start_date, end_date, granularity, user_id)


# =============================
# GLOBAL ANALYTICS
# =============================

@router.get("/global-analytics", response_model=GlobalAnalyticsResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_global_analytics(
    start_date: date = Query(None, description="Start date (defaults to 30 days ago)"),
    end_date: date = Query(None, description="End date (defaults to today)"),
    granularity: str = Query("daily", description="daily or weekly"),
    user_id: str = Query(None, description="Filter by specific user"),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(any_viewer),
):
    """
    Cross-campaign portfolio analytics.
    Returns aggregate KPIs and time-series data for all campaigns.
    """
    return service.global_analytics(current_user, start_date, end_date, granularity, user_id)


# =============================
# CAMPAIGN COMPARISON
# =============================

@router.get("/campaign-comparison", response_model=CampaignComparisonResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_campaign_comparison(
    start_date: date = Query(None, description="Optional start date filter"),
    end_date: date = Query(None, description="Optional end date filter"),
    sort_by: str = Query("open_rate", description="Sort field: open_rate, reply_rate, sent_count, campaign_name, created_at"),
    sort_dir: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: str = Query(None, description="Filter by specific user"),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(any_viewer),
):
    """
    All campaigns side-by-side with metrics, sortable and paginated.
    """
    return service.campaign_comparison(current_user, start_date, end_date, sort_by, sort_dir, page, page_size,
                                       user_id)


# =============================
# PROVIDER PERFORMANCE
# =============================

@router.get("/provider-performance", response_model=ProviderPerformanceResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_provider_performance(
    start_date: date = Query(None),
    end_date: date = Query(None),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(any_viewer),
):
    """
    Per-provider (Gmail/Outlook/SES) aggregate sending metrics.
    """
    return service.provider_performance(current_user, start_date, end_date)


# =============================
# CSV EXPORTS
# =============================

@router.get("/export/global-analytics", dependencies=[Depends(require_permission("export_data"))])
async def export_global_analytics_csv(
    start_date: date = Query(None),
    end_date: date = Query(None),
    user_id: str = Query(None),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(exporter),
):
    """Download global analytics as CSV (scoped to user if user_id provided)."""
    return _download(service.global_analytics_csv(current_user, start_date, end_date, user_id))


@router.get("/export/global-analytics/docx", dependencies=[Depends(require_permission("export_data"))])
async def export_global_analytics_docx(
    start_date: date = Query(None),
    end_date: date = Query(None),
    user_id: str = Query(None),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(exporter),
):
    """Download full analytics report as Word doc (scoped to user if user_id provided)."""
    return _download(service.global_analytics_docx(current_user, start_date, end_date, user_id), DOCX)


@router.get("/export/campaign-comparison", dependencies=[Depends(require_permission("export_data"))])
async def export_campaign_comparison_csv(
    user_id: str = Query(None, description="Filter by specific user"),
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(exporter),
):
    """Download campaign comparison table as CSV (scoped to user if user_id provided)."""
    return _download(service.campaign_comparison_csv(current_user, user_id))


@router.get("/export/prospects/{campaign_id}", dependencies=[Depends(require_permission("export_data"))])
async def export_prospects_csv(
    campaign_id: str,
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(exporter),
):
    """Download prospects with engagement data for a campaign as CSV."""
    return _download(service.prospects_csv(current_user, campaign_id))


@router.get("/export/deliverability", dependencies=[Depends(require_permission("export_data"))])
async def export_deliverability_csv(
    service: ViewerReports = Depends(get_reports_service),
    current_user: ReportViewer = Depends(exporter),
):
    """Download domain health report as CSV."""
    return _download(service.deliverability_csv(current_user))
