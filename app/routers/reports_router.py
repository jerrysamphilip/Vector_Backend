# app/routers/reports_router.py
"""
Reports API — Global Analytics, Campaign Comparison, Provider Performance, CSV Exports.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from datetime import date, timedelta
from typing import Optional

from app.core.auth import require_role, require_permission
from app.core.database import get_db
from app.models.user import User
from app.models.campaign import Campaign
from app.services.reports_service import ReportsService
from app.schemas.reports_schema import (
    GlobalAnalyticsResponse,
    CampaignComparisonResponse,
    ProviderPerformanceResponse,
    DashboardSummaryResponse,
)

router = APIRouter(prefix="/reports", tags=["Reports"])


def get_reports_service(db: Session = Depends(get_db)) -> ReportsService:
    return ReportsService(db)


# =============================
# DASHBOARD SUMMARY (single call)
# =============================


def _report_scope(db, current_user: User, user_id: Optional[str]):
    """Whose campaigns a report may cover: agents themselves, admins anyone, managers their
    own team (a single member on request; another team's member is refused)."""
    from app.services.contact_service import visible_user_ids
    if current_user.role == "AGENT":
        return current_user.user_id
    visible = visible_user_ids(db, current_user)
    if visible is None:
        return user_id
    if user_id:
        if user_id not in visible:
            raise HTTPException(status_code=403, detail="That user is outside your team")
        return user_id
    return sorted(visible)


def _can_see_owner(db, current_user: User, owner_id: Optional[str]) -> bool:
    from app.services.contact_service import visible_user_ids
    visible = visible_user_ids(db, current_user)
    return visible is None or owner_id in visible

@router.get("/dashboard-summary", response_model=DashboardSummaryResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_dashboard_summary(
    start_date: date = Query(None, description="Start date (defaults to 30 days ago)"),
    end_date: date = Query(None, description="End date (defaults to today)"),
    granularity: str = Query("daily", description="daily or weekly"),
    user_id: str = Query(None, description="Filter by specific user (SUPER_ADMIN/ADMIN/MANAGER only)"),
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Full dashboard summary — KPIs, timeseries, lead stats, campaign stats,
    mailbox stats, provider performance, mailbox health, top campaigns, optimization metrics.
    Single API call for the entire Reports page.
    """
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)

    # AGENTs always see only their own data
    user_id = _report_scope(service.db, current_user, user_id)

    return service.get_dashboard_summary(
        tenant_id=current_user.tenant_id,
        start_date=start_date,
        end_date=end_date,
        granularity=granularity,
        user_id=user_id,
    )


# =============================
# GLOBAL ANALYTICS
# =============================

@router.get("/global-analytics", response_model=GlobalAnalyticsResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_global_analytics(
    start_date: date = Query(None, description="Start date (defaults to 30 days ago)"),
    end_date: date = Query(None, description="End date (defaults to today)"),
    granularity: str = Query("daily", description="daily or weekly"),
    user_id: str = Query(None, description="Filter by specific user"),
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Cross-campaign portfolio analytics.
    Returns aggregate KPIs and time-series data for all campaigns.
    """
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)

    user_id = _report_scope(service.db, current_user, user_id)

    return service.get_global_analytics(
        tenant_id=current_user.tenant_id,
        start_date=start_date,
        end_date=end_date,
        granularity=granularity,
        user_id=user_id,
    )


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
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    All campaigns side-by-side with metrics, sortable and paginated.
    """
    user_id = _report_scope(service.db, current_user, user_id)

    return service.get_campaign_comparison(
        tenant_id=current_user.tenant_id,
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
        user_id=user_id,
    )


# =============================
# PROVIDER PERFORMANCE
# =============================

@router.get("/provider-performance", response_model=ProviderPerformanceResponse, dependencies=[Depends(require_permission("view_analytics"))])
async def get_provider_performance(
    start_date: date = Query(None),
    end_date: date = Query(None),
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Per-provider (Gmail/Outlook/SES) aggregate sending metrics.
    """
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)

    return service.get_provider_performance(
        tenant_id=current_user.tenant_id,
        start_date=start_date,
        end_date=end_date,
        user_id=_report_scope(service.db, current_user, None),
    )


# =============================
# CSV EXPORTS
# =============================

@router.get("/export/global-analytics", dependencies=[Depends(require_permission("export_data"))])
async def export_global_analytics_csv(
    start_date: date = Query(None),
    end_date: date = Query(None),
    user_id: str = Query(None),
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Download global analytics as CSV (scoped to user if user_id provided)."""
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)
    user_id = _report_scope(service.db, current_user, user_id)

    buf = service.export_global_analytics_csv(current_user.tenant_id, start_date, end_date, user_id)
    filename = f"analytics_{start_date}_{end_date}{'_' + user_id[:8] if isinstance(user_id, str) else ''}.csv"
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/global-analytics/docx", dependencies=[Depends(require_permission("export_data"))])
async def export_global_analytics_docx(
    start_date: date = Query(None),
    end_date: date = Query(None),
    user_id: str = Query(None),
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Download full analytics report as Word doc (scoped to user if user_id provided)."""
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)
    user_id = _report_scope(service.db, current_user, user_id)

    buf = service.export_global_analytics_docx(current_user.tenant_id, start_date, end_date, user_id)
    filename = f"analytics_report_{start_date}_{end_date}{'_' + user_id[:8] if isinstance(user_id, str) else ''}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/campaign-comparison", dependencies=[Depends(require_permission("export_data"))])
async def export_campaign_comparison_csv(
    user_id: str = Query(None, description="Filter by specific user"),
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Download campaign comparison table as CSV (scoped to user if user_id provided)."""
    user_id = _report_scope(service.db, current_user, user_id)
    buf = service.export_campaign_comparison_csv(current_user.tenant_id, user_id=user_id)
    filename = f"campaign_comparison{'_' + user_id[:8] if isinstance(user_id, str) else ''}.csv"
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/prospects/{campaign_id}", dependencies=[Depends(require_permission("export_data"))])
async def export_prospects_csv(
    campaign_id: str,
    service: ReportsService = Depends(get_reports_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Download prospects with engagement data for a campaign as CSV."""
    # Verify campaign belongs to tenant
    campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    if not campaign or campaign.tenant_id != current_user.tenant_id or not _can_see_owner(db, current_user, campaign.created_by):
        raise HTTPException(status_code=404, detail="Campaign not found")

    buf = service.export_prospects_csv(campaign_id)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=prospects_{campaign_id[:8]}.csv"},
    )


@router.get("/export/deliverability", dependencies=[Depends(require_permission("export_data"))])
async def export_deliverability_csv(
    service: ReportsService = Depends(get_reports_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Download domain health report as CSV."""
    buf = service.export_deliverability_csv(current_user.tenant_id)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=mailbox_health_report.csv"},
    )
