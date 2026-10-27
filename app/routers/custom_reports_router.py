# app/routers/custom_reports_router.py
"""
Custom reports (BRD v2.0 BR-SF-14): managers pick an object, filters, a
grouping (optionally a second one for a matrix), a measure and a date range,
save it, and share it. Every run is limited to the viewer's records and their
team's, so a shared report shows each person only what they may see.

Thin routes (ADR 0009): the rules are in app/services/reports_custom_service.py.
"""
from fastapi import APIRouter, Depends

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.reports import ReportViewer
from app.schemas.reports_custom_schema import Definition, ReportWrite
from app.services.reports_custom_service import CustomReports

router = APIRouter(prefix="/reports/custom", tags=["Custom reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def custom_reports(db=Depends(get_db)) -> CustomReports:
    return CustomReports.for_session(db)


@router.get("/meta")
def meta(current_user: ReportViewer = Depends(tenant_user)):
    return CustomReports.meta(current_user)


@router.post("/run")
def run(d: Definition, service: CustomReports = Depends(custom_reports),
        current_user: ReportViewer = Depends(tenant_user)):
    """Ad-hoc run while building: same permission as saving a report (shared reports run via GET /{id})."""
    return service.run_adhoc(current_user, d)


@router.get("")
def list_reports(service: CustomReports = Depends(custom_reports), current_user: ReportViewer = Depends(tenant_user)):
    return service.list_reports(current_user)


@router.post("", status_code=201)
def create_report(payload: ReportWrite, service: CustomReports = Depends(custom_reports),
                  current_user: ReportViewer = Depends(tenant_user)):
    return service.create(current_user, payload)


@router.get("/{report_id}")
def run_saved(report_id: str, service: CustomReports = Depends(custom_reports),
              current_user: ReportViewer = Depends(tenant_user)):
    return service.run_saved(current_user, report_id)


@router.patch("/{report_id}")
def update_report(report_id: str, payload: ReportWrite, service: CustomReports = Depends(custom_reports),
                  current_user: ReportViewer = Depends(tenant_user)):
    return service.update(current_user, report_id, payload)


@router.delete("/{report_id}")
def delete_report(report_id: str, service: CustomReports = Depends(custom_reports),
                  current_user: ReportViewer = Depends(tenant_user)):
    return service.delete(current_user, report_id)
