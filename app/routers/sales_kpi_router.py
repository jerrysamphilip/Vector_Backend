# app/routers/sales_kpi_router.py
"""
KPI reports (BRD v2.0 section 8): the six CRM KPIs in one call (GET /sales-reports/kpis), and the
detailed SQL-to-win and forecast-accuracy reports.

Same scoping as the other sales reports (app/routers/sales_reports_router.py): every figure
covers the viewer's records and their team's (BR-SH-02), `member` narrows to one person and
everyone below them, and amounts are blanked for roles that may not see them (BR-SF-12).
Duplicate contact rate and CRM adoption measure the workspace itself (data quality, seat usage),
so only workspace-wide viewers (SUPER_ADMIN / ADMIN and sales level 1, the Sales Head) get them;
for everyone else they come back with available = false.
"""
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.config import settings
from app.core.database import get_db
from app.models.sales import Lead, Opportunity
from app.models.user import User
from app.routers.sales_reports_router import _in, _owners, _pct, _period
from app.services import crm_kpis as kp
from app.services import forecast_snapshots as fs
from app.services import sales as svc
from app.services.contact_service import sees_everything
from app.services.sales_settings import MONEY_KEYS, can_see_amounts, mask_money, masked_for

router = APIRouter(prefix="/sales-reports", tags=["Sales reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

ACCURACY_MONEY_KEYS = MONEY_KEYS | {"actual", "variance", "won_at_snapshot", "weighted_at_snapshot",
                                    "average_forecast", "average_actual", "committed", "commit_open_at_snapshot",
                                    "weighted_variance"}
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
    return masked_for(db, current_user, _sql_to_win_data(db, current_user, owners, start, end, d0, d1))


def _sql_to_win_data(db: Session, current_user: User, owners, start, end, d0, d1) -> dict:
    """The SQL-to-win report before amounts are masked ("won" is a count here, but also a money key)."""
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
    return {
        "period": {"from": d0, "to": d1}, **totals,
        "sql_to_win_rate": _pct(totals["won"], totals["sqls"]),
        "sql_to_deal_rate": _pct(totals["with_deal"], totals["sqls"]),
        # Of the SQLs whose deal has closed, the share won (the rest are still being worked)
        "closed_win_rate": _pct(totals["won"], totals["won"] + totals["lost"]),
        "won_amount": round(won_amount, 2),
        "average_days_sql_to_win": round(sum(days) / len(days), 1) if days else None,
        "by_owner": rows,
    }


@router.get("/forecast-accuracy")
def forecast_accuracy(period_type: str = Query("QUARTER", pattern="^(MONTH|QUARTER)$"),
                      periods: int = Query(8, ge=1, le=36), member: Optional[str] = Query(None, max_length=36),
                      db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """The committed forecast stored at the start of each month / fiscal quarter against what was
    actually won by its end (BRD section 8): accuracy = actual / committed x 100. weighted_accuracy
    keeps the older closeness-to-weighted-forecast measure. Newest period first."""
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


# ── All six CRM KPIs (BRD v2.0 section 8) ─────────────────────

def _forecast_kpi(db: Session, user: User, owners, fy, quarter, d1: date, show_amounts: bool) -> dict:
    starts = kp.forecast_quarters(fy, quarter, d1)
    figures = [fs.period_figures(db, user.tenant_id, "QUARTER", s, owners) for s in starts]
    single = len(figures) == 1
    # A whole year counts its finished quarters; a single quarter may still be in progress
    usable = [f for f in figures if f["committed"] is not None and (f["closed"] or single)]
    actual = round(sum(f["actual"] for f in usable), 2)
    committed = round(sum(f["committed"] for f in usable), 2)
    value = kp.forecast_accuracy_pct(actual, committed) if usable else None
    label = figures[0]["label"] if single else f"{svc.fiscal_label(fy)} (finished quarters)"
    note = None
    if not any(f["has_snapshot"] for f in figures):
        note = (f"No forecast was stored at the start of {label}. It is stored automatically in the first days "
                "of every quarter (admins can store it by hand on the Forecast tab).")
    elif all(f["legacy_snapshot"] for f in figures if f["has_snapshot"]):
        note = f"The forecast for {label} was stored before committed forecasts were recorded."
    elif not usable:
        note = f"No quarter of {label} has finished yet."
    elif not committed:
        note = f"Nothing was committed for {label}."
    elif single and not figures[0]["closed"]:
        note = f"{label} is in progress: revenue won so far against the committed forecast."
    quarters = [{k: f[k] for k in ("label", "period_start", "period_end", "closed", "has_snapshot", "committed",
                                   "actual", "accuracy_to_date")} for f in figures]
    if not show_amounts:
        quarters = mask_money(quarters, ACCURACY_MONEY_KEYS)
    return kp.kpi("forecast_accuracy", value, actual if usable and show_amounts else None,
                  committed if usable and show_amounts else None,
                  period={"from": starts[0], "to": fs.add_months(starts[-1], 3) - timedelta(days=1), "label": label},
                  scope="team", note=note, numerator_label="Revenue won", denominator_label="Committed forecast",
                  in_progress=bool(single and not figures[0]["closed"]), quarters=quarters,
                  amounts_hidden=not show_amounts)


@router.get("/kpis")
def crm_kpis(date_from: Optional[date] = None, date_to: Optional[date] = None,
             fy: Optional[int] = Query(None, ge=1970, le=2100), quarter: Optional[int] = Query(None, ge=1, le=4),
             member: Optional[str] = Query(None, max_length=36),
             db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """The six BRD section 8 KPIs: value (%), numerator, denominator, BRD target, period and a note.
    Period: fy (+ quarter) or date_from / date_to (default: the last 90 days)."""
    start, end, d0, d1 = _kpi_period(date_from, date_to, fy, quarter)
    owners = _owners(db, current_user, member)
    tenant = current_user.tenant_id
    period = {"from": d0, "to": d1}
    workspace_view = sees_everything(current_user)
    show_amounts = can_see_amounts(db, current_user)
    restricted = "Workspace-level KPI: shown to admins and the Sales Head."
    out = []

    # 1. Duplicate contact rate (workspace data quality; member= narrows to that team's contacts)
    if workspace_view:
        counts = kp.duplicate_counts(db, tenant, owners)
        dups, total = counts if counts else (None, None)
        note = "Too many contacts to check for duplicates right now; try again later." if counts is None else \
            None if total else "No contacts yet."
        out.append(kp.kpi("duplicate_contact_rate", kp.pct(dups, total) if counts else None, dups, total,
                          period={"as_of": date.today()},
                          scope="team" if member else "workspace", numerator_label="Duplicate contacts",
                          denominator_label="Live contacts",
                          target={"operator": "<", "value": kp.DUPLICATE_RATE_TARGET, "label": "< 1%"}, note=note))
    else:
        out.append(kp.kpi("duplicate_contact_rate", None, period={"as_of": date.today()}, scope="workspace",
                          available=False, note=restricted,
                          target={"operator": "<", "value": kp.DUPLICATE_RATE_TARGET, "label": "< 1%"}))

    # 2. Reply-to-lead conversion
    leads_from_replies, positive = kp.reply_to_lead_counts(db, tenant, owners, start, end)
    out.append(kp.kpi("reply_to_lead", kp.pct(leads_from_replies, positive), leads_from_replies, positive,
                      period=period, scope="team", numerator_label="Leads from replies",
                      denominator_label="Contacts who replied positively",
                      note=kp.reply_to_lead_note(positive, bool(settings.OPENAI_API_KEY)),
                      classifier_available=bool(settings.OPENAI_API_KEY)))

    # 3. Lead-to-SQL conversion
    sqls, leads = kp.lead_to_sql_counts(db, tenant, owners, start, end)
    out.append(kp.kpi("lead_to_sql", kp.pct(sqls, leads), sqls, leads, period=period, scope="team",
                      numerator_label="Reached SQL", denominator_label="Leads created",
                      note=None if leads else "No leads created in the period."))

    # 4. SQL-to-win rate (the same count as GET /sales-reports/sql-to-win)
    s2w = _sql_to_win_data(db, current_user, owners, start, end, d0, d1)
    out.append(kp.kpi("sql_to_win", kp.pct(s2w["won"], s2w["sqls"]), s2w["won"], s2w["sqls"], period=period,
                      scope="team", numerator_label="SQLs won", denominator_label="SQLs",
                      note=None if s2w["sqls"] else "No leads reached SQL in the period."))

    # 5. Forecast accuracy (per fiscal quarter)
    out.append(_forecast_kpi(db, current_user, owners, fy, quarter, d1, show_amounts))

    # 6. CRM adoption (week containing the period end, and the 8 weeks before)
    if workspace_view:
        # member= narrows seats and activity to that person's team (owners is exactly that team here)
        weeks = kp.adoption(db, tenant, min(d1, date.today()), owners if member else None)
        current = weeks[-1]
        out.append(kp.kpi("crm_adoption", current["value"], current["active_users"], current["licensed_users"],
                          period={"from": current["week_start"], "to": current["week_end"],
                                  "label": "This week" if current["partial"] else f"Week of {current['week_start']}"},
                          scope="team" if member else "workspace", numerator_label="Active users this week",
                          denominator_label="Licensed users", trend=weeks,
                          last_full_week=weeks[-2] if current["partial"] else current,
                          note="Week to date." if current["partial"] else None))
    else:
        out.append(kp.kpi("crm_adoption", None, period=period, scope="workspace", available=False, note=restricted))

    return {"period": {**period, "fy": fy, "quarter": quarter}, "member": member,
            "workspace_view": workspace_view, "amounts_hidden": not show_amounts, "kpis": out}
