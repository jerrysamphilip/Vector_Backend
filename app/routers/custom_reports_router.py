# app/routers/custom_reports_router.py
"""
Custom reports (BRD v2.0 BR-SF-14): managers pick an object, filters, a
grouping (optionally a second one for a matrix), a measure and a date range,
save it, and share it. Every run is limited to the viewer's records and their
team's, so a shared report shows each person only what they may see.
"""
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.campaign import Campaign
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask
from app.models.prospect import Prospect
from app.models.sales import LEAD_STAGES, Lead, Opportunity, SalesStage
from app.models.sales_extra import SavedReport
from app.models.user import User
from app.services import sales as svc
from app.services.contact_service import clean_str, scope, sees_everything
from app.services.sales_settings import can_see_amounts

router = APIRouter(prefix="/reports/custom", tags=["Custom reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def _month(col):
    return func.date_format(col, "%Y-%m")


# object -> (model, owner column, {field: (label, column expression)}, {date field: (label, column)}, measures)
def _objects():
    return {
        "leads": {
            "label": "Leads", "model": Lead, "owner": Lead.owner_id,
            "fields": {"stage": ("Stage", Lead.stage), "owner": ("Owner", Lead.owner_id), "source": ("Source", Lead.source),
                       "campaign": ("Campaign", Lead.campaign_id), "created_month": ("Created month", _month(Lead.created_at)),
                       "disqualified_reason": ("Disqualification reason", Lead.disqualified_reason)},
            "dates": {"created_at": ("Created", Lead.created_at), "qualified_at": ("Became SQL", Lead.qualified_at),
                      "converted_at": ("Converted", Lead.converted_at)},
            "measures": {"count": ("Number of leads", None)},
        },
        "deals": {
            "label": "Opportunities", "model": Opportunity, "owner": Opportunity.owner_id,
            "fields": {"stage": ("Stage", Opportunity.stage_id), "status": ("Status", Opportunity.status),
                       "owner": ("Owner", Opportunity.owner_id), "client_type": ("Client type", Opportunity.client_type),
                       "forecast_category": ("Forecast category", Opportunity.forecast_category),
                       "campaign": ("Campaign", Opportunity.campaign_id), "company": ("Company", Opportunity.account_id),
                       "close_month": ("Close month", _month(Opportunity.close_date)),
                       "created_month": ("Created month", _month(Opportunity.created_at)),
                       "closed_reason": ("Win / loss reason", Opportunity.closed_reason)},
            "dates": {"created_at": ("Created", Opportunity.created_at), "close_date": ("Close date", Opportunity.close_date),
                      "closed_at": ("Closed", Opportunity.closed_at)},
            "measures": {"count": ("Number of deals", None), "sum_amount": ("Total amount", Opportunity.amount),
                         "avg_amount": ("Average amount", Opportunity.amount)},
        },
        "contacts": {
            "label": "Contacts", "model": Prospect, "owner": Prospect.owner_id,
            "fields": {"lifecycle_stage": ("Lifecycle stage", Prospect.lifecycle_stage),
                       "lead_status": ("Lead status", Prospect.lead_status), "owner": ("Owner", Prospect.owner_id),
                       "country": ("Country", Prospect.poc_country), "industry": ("Industry", Prospect.industry),
                       "lead_source": ("Lead source", Prospect.lead_source),
                       "consent_status": ("Subscription", Prospect.consent_status),
                       "created_month": ("Created month", _month(Prospect.created_at))},
            "dates": {"created_at": ("Created", Prospect.created_at)},
            "measures": {"count": ("Number of contacts", None)},
        },
        "activities": {
            "label": "Activities", "model": ContactActivity, "owner": ContactActivity.created_by,
            "fields": {"activity_type": ("Type", ContactActivity.activity_type), "owner": ("Logged by", ContactActivity.created_by),
                       "source": ("Source", ContactActivity.source), "month": ("Month", _month(ContactActivity.occurred_at))},
            "dates": {"occurred_at": ("Date", ContactActivity.occurred_at)},
            "measures": {"count": ("Number of activities", None)},
        },
        "tasks": {
            "label": "Tasks", "model": CrmTask, "owner": CrmTask.owner_id,
            "fields": {"status": ("Status", CrmTask.status), "owner": ("Owner", CrmTask.owner_id),
                       "priority": ("Priority", CrmTask.priority), "task_type": ("Type", CrmTask.task_type)},
            "dates": {"created_at": ("Created", CrmTask.created_at), "due_at": ("Due", CrmTask.due_at)},
            "measures": {"count": ("Number of tasks", None)},
        },
    }


OPERATORS = {"is": "is", "is_not": "is not", "contains": "contains", "is_empty": "is empty", "is_not_empty": "is known"}


class Definition(BaseModel):
    object: str
    group_by: str
    group_by_2: Optional[str] = None
    measure: str = "count"
    filters: List[Dict[str, Any]] = []
    date_field: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    chart: str = "bar"           # bar / table


class ReportWrite(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[Definition] = None
    shared: Optional[bool] = None


def _can_build(user: User) -> bool:
    return user.role in ("SUPER_ADMIN", "ADMIN", "MANAGER") or (user.sales_level or 9) <= 2


def _labels(db: Session, tenant_id: str, key: str, values) -> Dict[Any, str]:
    values = [v for v in values if v]
    if not values:
        return {}
    if key == "owner":
        return svc.user_names(db, values)
    if key == "stage" and values and len(str(values[0])) == 36:
        return {s.stage_id: s.name for s in db.query(SalesStage).filter(SalesStage.stage_id.in_(values))}
    if key == "stage":
        return {k: v[0] for k, v in LEAD_STAGES.items()}
    if key == "campaign":
        return {c.campaign_id: c.campaign_name for c in db.query(Campaign.campaign_id, Campaign.campaign_name).filter(
            Campaign.campaign_id.in_(values))}
    if key == "company":
        return {a.account_id: a.name for a in db.query(Account.account_id, Account.name).filter(Account.account_id.in_(values))}
    return {}


def run_definition(db: Session, user: User, d: Definition) -> dict:
    objects = _objects()
    spec = objects.get(d.object)
    if not spec:
        raise HTTPException(status_code=400, detail=f"object must be one of {list(objects)}")
    for g in filter(None, (d.group_by, d.group_by_2)):
        if g not in spec["fields"]:
            raise HTTPException(status_code=400, detail=f"Can group {spec['label'].lower()} by {list(spec['fields'])}")
    if d.measure not in spec["measures"]:
        raise HTTPException(status_code=400, detail=f"measure must be one of {list(spec['measures'])}")
    money = d.measure != "count"
    if money and not can_see_amounts(db, user):
        raise HTTPException(status_code=403, detail="Your role cannot see amounts")
    model = spec["model"]
    tenant_col = getattr(model, "tenant_id")
    query = db.query(model).filter(tenant_col == user.tenant_id)
    if model is Prospect:
        query = query.filter(Prospect.deleted_at.is_(None))
    query = scope(query, db, user, spec["owner"])
    for f in d.filters or []:
        field, op, value = f.get("field"), f.get("operator") or "is", f.get("value")
        if field not in spec["fields"] or op not in OPERATORS:
            raise HTTPException(status_code=400, detail=f"Bad filter on {field}")
        col = spec["fields"][field][1]
        if field == "owner" and value == "me":
            value = user.user_id
        if op == "is":
            query = query.filter(col.in_(value) if isinstance(value, list) else col == value)
        elif op == "is_not":
            query = query.filter(or_(col.is_(None), ~(col.in_(value) if isinstance(value, list) else col == value)))
        elif op == "contains":
            query = query.filter(col.ilike(f"%{value}%"))
        elif op == "is_empty":
            query = query.filter(or_(col.is_(None), col == ""))
        else:
            query = query.filter(col.isnot(None), col != "")
    if d.date_field:
        if d.date_field not in spec["dates"]:
            raise HTTPException(status_code=400, detail=f"date_field must be one of {list(spec['dates'])}")
        dcol = spec["dates"][d.date_field][1]
        if d.date_from:
            query = query.filter(dcol >= datetime.combine(d.date_from, time.min))
        if d.date_to:
            query = query.filter(dcol < datetime.combine(d.date_to + timedelta(days=1), time.min))

    g1 = spec["fields"][d.group_by][1]
    g2 = spec["fields"][d.group_by_2][1] if d.group_by_2 else None
    mcol = spec["measures"][d.measure][1]
    measure = {"count": func.count(), "sum_amount": func.coalesce(func.sum(mcol), 0),
               "avg_amount": func.avg(mcol)}[d.measure]
    cols = [g1] + ([g2] if g2 is not None else [])
    rows = query.with_entities(*cols, measure).group_by(*cols).all()
    l1 = _labels(db, user.tenant_id, d.group_by, [r[0] for r in rows])
    l2 = _labels(db, user.tenant_id, d.group_by_2, [r[1] for r in rows]) if g2 is not None else {}

    def num(v):
        return round(float(v), 2) if v is not None else 0

    if g2 is None:
        out = [{"key": r[0], "label": l1.get(r[0], r[0]) if r[0] not in (None, "") else "(none)", "value": num(r[1])}
               for r in rows]
        out.sort(key=lambda r: -r["value"] if not d.group_by.endswith("month") else 0)
        if d.group_by.endswith("month"):
            out.sort(key=lambda r: r["key"] or "9999")
        return {"rows": out, "columns": None, "total": round(sum(r["value"] for r in out), 2) if d.measure != "avg_amount" else None,
                "money": money, "measure_label": spec["measures"][d.measure][0]}
    columns, matrix = [], {}
    for a, b, v in rows:
        ck = b if b not in (None, "") else "(none)"
        if ck not in columns:
            columns.append(ck)
        rk = a if a not in (None, "") else "(none)"
        matrix.setdefault(rk, {"key": rk, "label": l1.get(a, a) if a not in (None, "") else "(none)", "values": {}})
        matrix[rk]["values"][ck] = num(v)
    col_out = [{"key": c, "label": l2.get(c, c)} for c in columns]
    out = list(matrix.values())
    for r in out:
        r["value"] = round(sum(r["values"].values()), 2) if d.measure != "avg_amount" else None
    out.sort(key=lambda r: -(r["value"] or 0))
    return {"rows": out, "columns": col_out, "total": round(sum(r["value"] or 0 for r in out), 2) if d.measure != "avg_amount" else None,
            "money": money, "measure_label": spec["measures"][d.measure][0]}


def _report_dict(r: SavedReport, user: User, names: dict = None) -> dict:
    return {"report_id": r.report_id, "name": r.name, "description": r.description, "definition": r.definition,
            "shared": r.shared, "owner_id": r.owner_id, "owner_name": (names or {}).get(r.owner_id),
            "can_edit": r.owner_id == user.user_id or user.role in ("SUPER_ADMIN", "ADMIN"), "updated_at": r.updated_at}


@router.get("/meta")
def meta(current_user: User = Depends(tenant_user)):
    objects = _objects()
    return {"can_build": _can_build(current_user), "operators": OPERATORS, "objects": {
        k: {"label": v["label"], "fields": {f: lab for f, (lab, _) in v["fields"].items()},
            "dates": {f: lab for f, (lab, _) in v["dates"].items()},
            "measures": {m: lab for m, (lab, _) in v["measures"].items()}} for k, v in objects.items()}}


@router.post("/run")
def run(d: Definition, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    return run_definition(db, current_user, d)


@router.get("")
def list_reports(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    rows = db.query(SavedReport).filter(SavedReport.tenant_id == current_user.tenant_id,
                                        or_(SavedReport.shared.is_(True), SavedReport.owner_id == current_user.user_id)) \
        .order_by(SavedReport.name).all()
    names = svc.user_names(db, [r.owner_id for r in rows])
    return [_report_dict(r, current_user, names) for r in rows]


@router.post("", status_code=201)
def create_report(payload: ReportWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    if not _can_build(current_user):
        raise HTTPException(status_code=403, detail="Only managers can build reports")
    if not clean_str(payload.name) or not payload.definition:
        raise HTTPException(status_code=400, detail="Give the report a name and a definition")
    run_definition(db, current_user, payload.definition)  # validates
    r = SavedReport(tenant_id=current_user.tenant_id, owner_id=current_user.user_id, name=clean_str(payload.name),
                    description=payload.description, definition=payload.definition.model_dump(mode="json"),
                    shared=bool(payload.shared))
    db.add(r)
    db.commit()
    return _report_dict(r, current_user)


def _get(db, user, report_id, edit=False) -> SavedReport:
    r = db.query(SavedReport).filter(SavedReport.report_id == report_id, SavedReport.tenant_id == user.tenant_id).first()
    if not r or not (r.shared or r.owner_id == user.user_id):
        raise HTTPException(status_code=404, detail="Report not found")
    if edit and not _report_dict(r, user)["can_edit"]:
        raise HTTPException(status_code=403, detail="Only the report's owner or an admin can change it")
    return r


@router.get("/{report_id}")
def run_saved(report_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    r = _get(db, current_user, report_id)
    return {**_report_dict(r, current_user), "result": run_definition(db, current_user, Definition(**r.definition))}


@router.patch("/{report_id}")
def update_report(report_id: str, payload: ReportWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    r = _get(db, current_user, report_id, edit=True)
    if payload.definition:
        run_definition(db, current_user, payload.definition)
        r.definition = payload.definition.model_dump(mode="json")
    if payload.name is not None:
        r.name = clean_str(payload.name) or r.name
    if payload.description is not None:
        r.description = payload.description
    if payload.shared is not None:
        r.shared = payload.shared
    db.commit()
    return _report_dict(r, current_user)


@router.delete("/{report_id}")
def delete_report(report_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    r = _get(db, current_user, report_id, edit=True)
    db.delete(r)
    db.commit()
    return {"status": "deleted", "report_id": report_id}
