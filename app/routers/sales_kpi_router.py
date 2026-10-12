# app/routers/sales_kpi_router.py
"""
KPI reports (BRD v2.0 section 8): SQL-to-win rate and forecast accuracy.

Same scoping as the other sales reports (app/routers/sales_reports_router.py): every figure
covers the viewer's records and their team's (BR-SH-02), `member` narrows to one person and
everyone below them, and amounts are blanked for roles that may not see them (BR-SF-12).
"""
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.sales import Lead, Opportunity
from app.models.user import User
from app.routers.sales_reports_router import _in, _owners, _pct, _period
from app.services import forecast_snapshots as fs
from app.services import sales as svc
from app.services.sales_settings import MONEY_KEYS, can_see_amounts, masked_for

router = APIRouter(prefix="/sales-reports", tags=["Sales reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

ACCURACY_MONEY_KEYS = MONEY_KEYS | {"actual", "variance", "won_at_snapshot", "weighted_at_snapshot",
                                    "average_forecast", "average_actual"}
OUTCOME_RANK = {"WON": 3, "OPEN": 2, "LOST": 1}


def _kpi_period(date_from: Optional[date], date_to: Optional[date], fy: Optional[int], quarter: Optional[int]):
    """A fiscal year / quarter when fy is given, else the date range (default: the last 90 days)."""
    if quarter and not fy:
        raise HTTPException(status_code=400, detail="quarter needs fy")
    if fy:
        fy_start, fy_end = svc.fiscal_year_bounds(fy)
        if quarter:
            fy_start = fs.add_months(fy_start, 3 * (quarter - 1))
            fy_end = fs.add_months(fy_start, 3)
        return (datetime.combine(fy_start, time.min), datetime.combine(fy_end, time.min),
                fy_start, fy_end - timedelta(days=1))
    return _period(date_from, date_to)


@router.get("/sql-to-win")
def sql_to_win(date_from: Optional[date] = None, date_to: Optional[date] = None,
               fy: Optional[int] = Query(None, ge=1970, le=2100), quarter: Optional[int] = Query(None, ge=1, le=4),
               member: Optional[str] = Query(None, max_length=36),
               db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Of the leads that reached SQL in the period, the share that became a won deal.
    A lead counts as won when any deal created from it is won; the lead owner gets the credit."""
    start, end, d0, d1 = _kpi_period(date_from, date_to, fy, quarter)
    owners = _owners(db, current_user, member)
    leads = db.query(Lead.lead_id, Lead.owner_id, Lead.opportunity_id, Lead.qualified_at).filter(
        Lead.tenant_id == current_user.tenant_id, _in(Lead.owner_id, owners),
        Lead.qualified_at >= start, Lead.qualified_at < end).all()
    lead_ids = [lead.lead_id for lead in leads]
    linked_opp_ids = [lead.opportunity_id for lead in leads if lead.opportunity_id]
    deals_by_lead: Dict[str, list] = {}
    if lead_ids:
        opp_to_lead = {lead.opportunity_id: lead.lead_id for lead in leads if lead.opportunity_id}
        lead_set = set(lead_ids)
        conditions = [Opportunity.lead_id.in_(lead_ids)]
        if linked_opp_ids:
            conditions.append(Opportunity.opportunity_id.in_(linked_opp_ids))
        for o in db.query(Opportunity.opportunity_id, Opportunity.lead_id, Opportunity.status, Opportunity.amount,
                          Opportunity.closed_at).filter(Opportunity.tenant_id == current_user.tenant_id,
                                                        or_(*conditions)):
            lid = o.lead_id if o.lead_id in lead_set else opp_to_lead.get(o.opportunity_id)
            if lid:
                bucket = deals_by_lead.setdefault(lid, [])
                if all(d.opportunity_id != o.opportunity_id for d in bucket):
                    bucket.append(o)

    totals = {"sqls": 0, "with_deal": 0, "won": 0, "lost": 0, "open": 0, "no_deal": 0}
    won_amount, days = 0.0, []
    per_owner: Dict[str, dict] = {}
    for lead in leads:
        deals = deals_by_lead.get(lead.lead_id, [])
        best = max(deals, key=lambda d: OUTCOME_RANK.get(d.status, 0)) if deals else None
        outcome = {"WON": "won", "LOST": "lost", "OPEN": "open"}.get(best.status, "open") if best else "no_deal"
        o = per_owner.setdefault(lead.owner_id, {"owner_id": lead.owner_id, "sqls": 0, "won": 0, "lost": 0,
                                                 "open": 0, "no_deal": 0, "won_amount": 0.0})
        for agg in (totals, o):
            agg["sqls"] += 1
            agg[outcome] += 1
        if deals:
            totals["with_deal"] += 1
        if outcome == "won":
            amount = sum(svc.as_float(d.amount) for d in deals if d.status == "WON")
            won_amount += amount
            o["won_amount"] += amount
            closed = min((d.closed_at for d in deals if d.status == "WON" and d.closed_at), default=None)
            if closed and lead.qualified_at:
                days.append(max((closed - lead.qualified_at).total_seconds() / 86400, 0))
    names = svc.user_names(db, per_owner)
    rows = []
    for o in per_owner.values():
        o["owner_name"] = names.get(o["owner_id"])
        o["won_amount"] = round(o["won_amount"], 2)
        o["sql_to_win_rate"] = _pct(o["won"], o["sqls"])
        rows.append(o)
    rows.sort(key=lambda r: (-r["sqls"], -r["won"], r["owner_name"] or ""))
    return masked_for(db, current_user, {
        "period": {"from": d0, "to": d1}, **totals,
        "sql_to_win_rate": _pct(totals["won"], totals["sqls"]),
        "sql_to_deal_rate": _pct(totals["with_deal"], totals["sqls"]),
        # Of the SQLs whose deal has closed, the share won (the rest are still being worked)
        "closed_win_rate": _pct(totals["won"], totals["won"] + totals["lost"]),
        "won_amount": round(won_amount, 2),
        "average_days_sql_to_win": round(sum(days) / len(days), 1) if days else None,
        "by_owner": rows,
    })


@router.get("/forecast-accuracy")
def forecast_accuracy(period_type: str = Query("QUARTER", pattern="^(MONTH|QUARTER)$"),
                      periods: int = Query(8, ge=1, le=36), member: Optional[str] = Query(None, max_length=36),
                      db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """The forecast stored at the start of each month / fiscal quarter against what was actually
    won by its end: accuracy = 1 - |actual - forecast| / forecast. Newest period first."""
    owners = _owners(db, current_user, member)
    rows = fs.history(db, current_user.tenant_id, period_type, owners, periods)
    closed = [r for r in rows if r["accuracy"] is not None]
    current_start, _ = fs.period_bounds(period_type, date.today())
    return masked_for(db, current_user, {
        "period_type": period_type, "periods": rows,
        "average_accuracy": round(sum(r["accuracy"] for r in closed) / len(closed), 1) if closed else None,
        "last_closed": closed[0] if closed else None,
        "current_period": {"period_start": current_start, "label": fs.period_label(period_type, current_start),
                           "has_snapshot": any(r["period_start"] == current_start for r in rows)},
        "can_take_snapshot": current_user.role in ("SUPER_ADMIN", "ADMIN"),
        "amounts_hidden": not can_see_amounts(db, current_user),
    }, keys=ACCURACY_MONEY_KEYS)


class SnapshotRequest(BaseModel):
    period_type: Optional[str] = Field(None, pattern="^(MONTH|QUARTER)$")  # none = both
    replace: bool = False              # retake the current period's snapshot


@router.post("/forecast-snapshots", status_code=201)
def take_forecast_snapshot(payload: SnapshotRequest, db: Session = Depends(get_db),
                           current_user: User = Depends(tenant_user)):
    """Admins: store the current period's forecast now (normally the worker does it at period start)."""
    if current_user.role not in ("SUPER_ADMIN", "ADMIN"):
        raise HTTPException(status_code=403, detail="Only admins can take forecast snapshots")
    types = [payload.period_type] if payload.period_type else list(fs.PERIOD_TYPES)
    results = [fs.take_snapshot(db, current_user.tenant_id, t, source="MANUAL", taken_by=current_user.user_id,
                                replace=payload.replace) for t in types]
    db.commit()
    return {"snapshots": results}
