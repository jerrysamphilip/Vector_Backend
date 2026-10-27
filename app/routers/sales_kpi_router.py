# app/routers/sales_kpi_router.py
"""
KPI reports (BRD v2.0 section 8): the six CRM KPIs in one call (GET /sales-reports/kpis), and the
detailed SQL-to-win and forecast-accuracy reports.

Same scoping as the other sales reports: every figure covers the viewer's records and their team's
(BR-SH-02), `member` narrows to one person and everyone below them, and amounts are blanked for roles
that may not see them (BR-SF-12). Thin routes (ADR 0009): app/services/reports_kpis_service.py and
app/services/reports_sales_service.py build the reports.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.reports import ReportViewer
from app.schemas.reports_sales_schema import SnapshotRequest
from app.services.reports_kpis_service import CrmKpis
from app.services.reports_sales_service import SalesReports

router = APIRouter(prefix="/sales-reports", tags=["Sales reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def sales_reports(db=Depends(get_db)) -> SalesReports:
    return SalesReports.for_session(db)


def crm_kpi_reports(db=Depends(get_db)) -> CrmKpis:
    return CrmKpis.for_session(db)


@router.get("/sql-to-win")
def sql_to_win(date_from: Optional[date] = None, date_to: Optional[date] = None,
               fy: Optional[int] = Query(None, ge=1970, le=2100), quarter: Optional[int] = Query(None, ge=1, le=4),
               member: Optional[str] = Query(None, max_length=36),
               service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """Of the leads that reached SQL in the period, the share that became a won deal.
    A lead counts as won when any deal created from it is won; the lead owner gets the credit."""
    return service.sql_to_win(current_user, date_from, date_to, fy, quarter, member)


@router.get("/forecast-accuracy")
def forecast_accuracy(period_type: str = Query("QUARTER", pattern="^(MONTH|QUARTER)$"),
                      periods: int = Query(8, ge=1, le=36), member: Optional[str] = Query(None, max_length=36),
                      service: SalesReports = Depends(sales_reports), current_user: ReportViewer = Depends(tenant_user)):
    """The committed forecast stored at the start of each month / fiscal quarter against what was
    actually won by its end (BRD section 8): accuracy = actual / committed x 100. weighted_accuracy
    keeps the older closeness-to-weighted-forecast measure. Newest period first."""
    return service.forecast_accuracy(current_user, period_type, periods, member)


@router.post("/forecast-snapshots", status_code=201)
def take_forecast_snapshot(payload: SnapshotRequest, service: SalesReports = Depends(sales_reports),
                           current_user: ReportViewer = Depends(tenant_user)):
    """Admins: store the current period's forecast now (normally the worker does it at period start)."""
    return service.take_forecast_snapshot(current_user, payload.period_type, payload.replace)


@router.get("/kpis")
def crm_kpis(date_from: Optional[date] = None, date_to: Optional[date] = None,
             fy: Optional[int] = Query(None, ge=1970, le=2100), quarter: Optional[int] = Query(None, ge=1, le=4),
             member: Optional[str] = Query(None, max_length=36),
             service: CrmKpis = Depends(crm_kpi_reports), current_user: ReportViewer = Depends(tenant_user)):
    """The six BRD section 8 KPIs: value (%), numerator, denominator, BRD target, period and a note.
    Period: fy (+ quarter) or date_from / date_to (default: the last 90 days)."""
    return service.kpis(current_user, date_from, date_to, fy, quarter, member)
