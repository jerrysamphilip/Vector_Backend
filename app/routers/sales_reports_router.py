# app/routers/sales_reports_router.py
"""
Phase 2 reporting (BRD v2.0 5.8 - 5.10): role dashboards, funnel, leads / SQL /
pipeline reports, quarter and financial-year forecast, team performance.

Every figure is limited to the viewer's records and their team's (BR-SH-02); a
`member` filter narrows to one person and everyone below them.

Thin routes (ADR 0009): the reports are built by app/services/reports_sales_service.py.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.reports import ReportViewer
from app.services.reports_sales_service import SalesReports

router = APIRouter(prefix="/sales-reports", tags=["Sales reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def sales_reports(db=Depends(get_db)) -> SalesReports:
    return SalesReports.for_session(db)


@router.get("/dashboard")
def dashboard(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
              service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Role dashboard (BR-DR-01): Sales Head sees every team, Business Development their team,
    Business Executives their own numbers and work queue."""
    return service.dashboard(current_user, date_from, date_to, member)


@router.get("/funnel")
def funnel(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
           service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Sent -> replied -> lead -> SQL -> opportunity -> won for the period (BR-DR-03)."""
    return service.funnel(current_user, date_from, date_to, member)


@router.get("/leads")
def leads_report(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                 service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Leads and SQLs created in the period: by stage, source and owner (BR-DR-02)."""
    return service.leads_report(current_user, date_from, date_to, member)


@router.get("/pipeline")
def pipeline_report(client_type: Optional[str] = None, member: Optional[str] = None,
                    service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Open pipeline by owner and by expected close month; won / lost to date (BR-DR-02)."""
    return service.pipeline(current_user, client_type, member)


@router.get("/forecast")
def forecast(fy: Optional[int] = Query(None, ge=1970, le=2100), member: Optional[str] = None, client_type: Optional[str] = None,
             service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Revenue forecast by quarter for one financial year, and by financial year (BR-FC-01/02).
    Each bucket counts opportunities by expected close date; totals reconcile with
    GET /opportunities?close_from=&close_to= for the same bucket and filters."""
    return service.forecast(current_user, fy, member, client_type)


@router.get("/team-performance")
def team_performance(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                     service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Scorecard per rep: activities, leads, SQLs, deals won and stage conversion (BR-TP-01).
    Managers see their own team; level 1 and admins see everyone."""
    return service.team_performance(current_user, date_from, date_to, member)


@router.get("/daily-limit")
def daily_limit(service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Today's new-contact count against the per-user limit (BR-OV-01)."""
    return service.daily_limit(current_user)


# ── Targets, leaderboard, campaign ROI (BR-SF-08, 09, 11) ─────

@router.get("/targets")
def targets_vs_actual(fy: Optional[int] = None, quarter: Optional[int] = Query(None, ge=1, le=4),
                      member: Optional[str] = None, service: SalesReports = Depends(sales_reports),
                      current_user: ReportViewer = Depends(tenant_user)):
    """Target against won, commit and best case per rep for a fiscal quarter, or the whole year (BR-SF-09)."""
    return service.targets(current_user, fy, quarter, member)


@router.get("/leaderboard")
def leaderboard(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Reps ranked by revenue won in the period, with deals, SQLs and activity (BR-SF-09)."""
    return service.leaderboard(current_user, date_from, date_to, member)


@router.get("/campaign-roi")
def campaign_roi(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                 service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Per campaign: contacts emailed, replies, leads, SQLs, opportunities, pipeline and revenue won (BR-SF-11).
    A deal belongs to the campaign its lead came from, else the last campaign that emailed the contact."""
    return service.campaign_roi(current_user, date_from, date_to, member)
