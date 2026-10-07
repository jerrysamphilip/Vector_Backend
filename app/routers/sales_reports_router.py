# app/routers/sales_reports_router.py
"""
Phase 2 reporting (BRD v2.0 5.8 - 5.10): role dashboards, funnel, leads / SQL /
pipeline reports, quarter and financial-year forecast, team performance.

Every figure is limited to the viewer's records and their team's (BR-SH-02); a
`member` filter narrows to one person and everyone below them.
"""
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.sales import LEAD_STAGES, Lead, Opportunity, SalesStage
from app.models.sales_extra import SalesTarget
from app.models.user import User
from app.services import sales as svc
from app.services.contact_service import (SALES_LEVELS, can_see_owner, sees_everything, team_user_ids,
                                          visible_user_ids)
from app.services.sales_settings import masked_for

router = APIRouter(prefix="/sales-reports", tags=["Sales reports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


# ── Scope and period ─────────────────────────────────────────

def _owners(db: Session, user: User, member: Optional[str]) -> Optional[Set[str]]:
    """Owner ids to report on; None = the whole workspace."""
    visible = visible_user_ids(db, user)
    if not member:
        return visible
    target = db.query(User).filter(User.user_id == member, User.tenant_id == user.tenant_id).first()
    if not target or not can_see_owner(db, user, member):
        raise HTTPException(status_code=404, detail="Team member not found")
    team = team_user_ids(db, target)
    return team if visible is None else team & visible


def _in(column, owners: Optional[Set[str]]):
    return True if owners is None else column.in_(owners or {"-"})


def _period(date_from: Optional[date], date_to: Optional[date]):
    """Inclusive dates -> [start, end) datetimes. Default: the last 90 days."""
    end_d = date_to or date.today()
    start_d = date_from or end_d - timedelta(days=89)
    if start_d > end_d:
        raise HTTPException(status_code=400, detail="date_from is after date_to")
    return datetime.combine(start_d, time.min), datetime.combine(end_d + timedelta(days=1), time.min), start_d, end_d


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def _rate_rows(rows) -> Dict[str, int]:
    return {k: int(v or 0) for k, v in rows}


# ── Core measures (shared by dashboard, team and reports) ─────

def _measures(db: Session, tenant_id: str, owners: Optional[Set[str]], start: datetime, end: datetime) -> dict:
    leads = db.query(Lead).filter(Lead.tenant_id == tenant_id, _in(Lead.owner_id, owners))
    opps = db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, _in(Opportunity.owner_id, owners))
    new_leads = leads.filter(Lead.created_at >= start, Lead.created_at < end).count()
    sqls = leads.filter(Lead.qualified_at >= start, Lead.qualified_at < end).count()
    open_sqls = leads.filter(Lead.stage == "SQL").count()
    opps_created = opps.filter(Opportunity.created_at >= start, Opportunity.created_at < end).count()
    closed = opps.filter(Opportunity.closed_at >= start, Opportunity.closed_at < end)
    won = closed.filter(Opportunity.status == "WON").with_entities(
        func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).one()
    lost = closed.filter(Opportunity.status == "LOST").count()
    stage_prob = {s.stage_id: s.probability for s in db.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)}
    open_rows = opps.filter(Opportunity.status == "OPEN").with_entities(
        Opportunity.stage_id, func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
    ).group_by(Opportunity.stage_id).all()
    open_amount = sum(svc.as_float(a) for _, _, a in open_rows)
    weighted = sum(svc.as_float(a) * stage_prob.get(sid, 0) / 100 for sid, _, a in open_rows)
    won_n, won_amt = int(won[0] or 0), svc.as_float(won[1])
    return {
        "new_leads": new_leads, "sqls": sqls, "open_sqls": open_sqls, "opportunities_created": opps_created,
        "open_opportunities": sum(n for _, n, _ in open_rows), "open_pipeline": round(open_amount, 2),
        "weighted_pipeline": round(weighted, 2), "won_count": won_n, "won_amount": round(won_amt, 2),
        "lost_count": lost, "win_rate": _pct(won_n, won_n + lost),
        "average_deal": round(won_amt / won_n, 2) if won_n else None,
    }


def _email_measures(db: Session, tenant_id: str, owners, start, end) -> dict:
    base = db.query(EmailMessage).join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id).filter(
        Prospect.tenant_id == tenant_id, _in(Prospect.owner_id, owners),
        EmailMessage.sent_at >= start, EmailMessage.sent_at < end)
    sent = base.filter(EmailMessage.direction == "OUTBOUND").with_entities(
        func.count(func.distinct(EmailMessage.prospect_id))).scalar() or 0
    replied = base.filter(EmailMessage.direction == "INBOUND").with_entities(
        func.count(func.distinct(EmailMessage.prospect_id))).scalar() or 0
    return {"contacts_emailed": sent, "contacts_replied": replied}


def _role_view(user: User) -> str:
    if sees_everything(user):
        return "SALES_HEAD"
    if user.sales_level == 2:
        return "BUSINESS_DEVELOPMENT"
    return "BUSINESS_EXECUTIVE"


def _team_groups(db: Session, user: User) -> List[User]:
    """Whose rollups a dashboard breaks down by: the viewer's direct reports, or for
    workspace-wide viewers the level 2 (Business Development) heads."""
    users = db.query(User).filter(User.tenant_id == user.tenant_id, User.status != "INACTIVE")
    direct = users.filter(User.manager_id == user.user_id).all()
    if direct:
        return direct
    if sees_everything(user):
        return users.filter(User.sales_level == 2).all() or users.filter(User.sales_level.isnot(None)).all()
    return []


# ── Endpoints ────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
              db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Role dashboard (BR-DR-01): Sales Head sees every team, Business Development their team,
    Business Executives their own numbers and work queue."""
    start, end, d0, d1 = _period(date_from, date_to)
    owners = _owners(db, current_user, member)
    tenant = current_user.tenant_id
    kpis = {**_measures(db, tenant, owners, start, end), **_email_measures(db, tenant, owners, start, end)}
    teams = []
    for head in _team_groups(db, current_user):
        team = team_user_ids(db, head)
        if owners is not None:
            team &= owners
        teams.append({"user_id": head.user_id, "name": f"{head.first_name} {head.last_name}".strip(),
                      "sales_level": head.sales_level, "level_label": SALES_LEVELS.get(head.sales_level),
                      "members": len(team), **_measures(db, tenant, team, start, end)})
    # The viewer's own work queue
    mine = {current_user.user_id}
    today = date.today()
    queue = {
        "sql_leads": svc.lead_dicts(db, db.query(Lead).filter(
            Lead.tenant_id == tenant, Lead.owner_id == current_user.user_id, Lead.stage == "SQL")
            .order_by(Lead.qualified_at).limit(10).all()),
        "next_steps_due": svc.lead_dicts(db, db.query(Lead).filter(
            Lead.tenant_id == tenant, Lead.owner_id == current_user.user_id, Lead.stage.in_(("NEW", "CONTACTED", "ENGAGED", "SQL")),
            Lead.next_step_at <= datetime.combine(today + timedelta(days=1), time.min)).order_by(Lead.next_step_at).limit(10).all()),
        "deals_closing": svc.opp_dicts(db, db.query(Opportunity).filter(
            Opportunity.tenant_id == tenant, Opportunity.owner_id == current_user.user_id, Opportunity.status == "OPEN",
            Opportunity.close_date <= today + timedelta(days=30)).order_by(Opportunity.close_date).limit(10).all()),
        "my": _measures(db, tenant, mine, start, end),
    }
    return masked_for(db, current_user, {
        "role_view": _role_view(current_user), "period": {"from": d0, "to": d1}, "kpis": kpis,
        "teams": teams, "queue": queue, "funnel": funnel(d0, d1, member, db, current_user)["stages"],
        "leaderboard": leaderboard(d0, d1, member, db, current_user)["rows"][:5]})


@router.get("/funnel")
def funnel(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
           db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Sent -> replied -> lead -> SQL -> opportunity -> won for the period (BR-DR-03)."""
    start, end, d0, d1 = _period(date_from, date_to)
    owners = _owners(db, current_user, member)
    tenant = current_user.tenant_id
    e = _email_measures(db, tenant, owners, start, end)
    m = _measures(db, tenant, owners, start, end)
    steps = [("sent", "Contacts emailed", e["contacts_emailed"]), ("replied", "Replied", e["contacts_replied"]),
             ("lead", "Leads", m["new_leads"]), ("sql", "SQLs", m["sqls"]),
             ("opportunity", "Opportunities", m["opportunities_created"]), ("won", "Won", m["won_count"])]
    out, prev = [], None
    for key, label, n in steps:
        # Step counts are period volumes, not one cohort, so a step can exceed the one before
        # (e.g. deals created directly); a ratio over 100% would mislead, so none is shown.
        ratio = _pct(n, prev) if prev is not None else None
        out.append({"key": key, "label": label, "count": n,
                    "from_previous": ratio if ratio is not None and ratio <= 100 else None,
                    "from_first": _pct(n, steps[0][2]) if steps[0][2] else None})
        prev = n
    return masked_for(db, current_user, {"period": {"from": d0, "to": d1}, "stages": out, "won_amount": m["won_amount"]})


@router.get("/leads")
def leads_report(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                 db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Leads and SQLs created in the period: by stage, source and owner (BR-DR-02)."""
    start, end, d0, d1 = _period(date_from, date_to)
    owners = _owners(db, current_user, member)
    base = db.query(Lead).filter(Lead.tenant_id == current_user.tenant_id, _in(Lead.owner_id, owners),
                                 Lead.created_at >= start, Lead.created_at < end)
    by_stage = _rate_rows(base.with_entities(Lead.stage, func.count(Lead.lead_id)).group_by(Lead.stage))
    by_source = base.with_entities(func.coalesce(Lead.source, "Unknown"), func.count(Lead.lead_id),
                                   func.sum(case((Lead.qualified_at.isnot(None), 1), else_=0))) \
        .group_by(func.coalesce(Lead.source, "Unknown")).all()
    owner_rows = base.with_entities(
        Lead.owner_id, func.count(Lead.lead_id),
        func.sum(case((Lead.qualified_at.isnot(None), 1), else_=0)),
        func.sum(case((Lead.stage == "CONVERTED", 1), else_=0)),
        func.sum(case((Lead.stage == "DISQUALIFIED", 1), else_=0)),
    ).group_by(Lead.owner_id).all()
    names = svc.user_names(db, [r[0] for r in owner_rows])
    to_sql = base.filter(Lead.qualified_at.isnot(None)).with_entities(
        func.avg(func.timestampdiff(text_day(), Lead.created_at, Lead.qualified_at))).scalar()
    total = sum(by_stage.values())
    sql_total = sum(int(r[2] or 0) for r in owner_rows)
    disq = base.filter(Lead.stage == "DISQUALIFIED").with_entities(
        func.coalesce(Lead.disqualified_reason, "No reason"), func.count(Lead.lead_id),
        func.sum(case((Lead.recycle_at.isnot(None), 1), else_=0))).group_by(
        func.coalesce(Lead.disqualified_reason, "No reason")).all()
    return {
        "period": {"from": d0, "to": d1}, "total": total, "sqls": sql_total,
        "lead_to_sql_rate": _pct(sql_total, total),
        "average_days_to_sql": round(float(to_sql), 1) if to_sql is not None else None,
        "by_stage": [{"stage": k, "label": v[0], "count": by_stage.get(k, 0)} for k, v in LEAD_STAGES.items()],
        "by_source": sorted([{"source": s, "leads": int(n), "sqls": int(q or 0), "sql_rate": _pct(int(q or 0), int(n))}
                             for s, n, q in by_source], key=lambda r: -r["leads"]),
        "disqualified_reasons": sorted([{"reason": r, "count": int(n), "recycling": int(c or 0)} for r, n, c in disq],
                                       key=lambda x: -x["count"]),
        "by_owner": sorted([{"owner_id": o, "owner_name": names.get(o), "leads": int(n), "sqls": int(q or 0),
                             "converted": int(c or 0), "disqualified": int(d or 0), "sql_rate": _pct(int(q or 0), int(n))}
                            for o, n, q, c, d in owner_rows], key=lambda r: -r["leads"]),
    }


def text_day():
    from sqlalchemy import literal_column
    return literal_column("DAY")


@router.get("/pipeline")
def pipeline_report(client_type: Optional[str] = None, member: Optional[str] = None,
                    db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Open pipeline by owner and by expected close month; won / lost to date (BR-DR-02)."""
    owners = _owners(db, current_user, member)
    stage_prob = {s.stage_id: s.probability for s in svc.stages(db, current_user.tenant_id, True)}
    base = db.query(Opportunity).filter(Opportunity.tenant_id == current_user.tenant_id,
                                        _in(Opportunity.owner_id, owners))
    if client_type:
        base = base.filter(Opportunity.client_type == client_type)
    rows = base.with_entities(Opportunity.owner_id, Opportunity.status, Opportunity.stage_id,
                              func.date_format(Opportunity.close_date, "%Y-%m"),
                              func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)) \
        .group_by(Opportunity.owner_id, Opportunity.status, Opportunity.stage_id,
                  func.date_format(Opportunity.close_date, "%Y-%m")).all()
    names = svc.user_names(db, [r[0] for r in rows])
    by_owner: Dict[str, dict] = {}
    by_month: Dict[str, dict] = {}
    for owner, status, stage_id, month, n, amount in rows:
        amount = svc.as_float(amount)
        o = by_owner.setdefault(owner, {"owner_id": owner, "owner_name": names.get(owner), "open_count": 0,
                                        "open_amount": 0.0, "weighted": 0.0, "won_count": 0, "won_amount": 0.0,
                                        "lost_count": 0})
        if status == "OPEN":
            w = amount * stage_prob.get(stage_id, 0) / 100
            o["open_count"] += n
            o["open_amount"] += amount
            o["weighted"] += w
            m = by_month.setdefault(month or "none", {"month": month, "count": 0, "amount": 0.0, "weighted": 0.0})
            m["count"] += n
            m["amount"] += amount
            m["weighted"] += w
        elif status == "WON":
            o["won_count"] += n
            o["won_amount"] += amount
        else:
            o["lost_count"] += n
    for o in by_owner.values():
        o["win_rate"] = _pct(o["won_count"], o["won_count"] + o["lost_count"])
        for k in ("open_amount", "weighted", "won_amount"):
            o[k] = round(o[k], 2)
    for m in by_month.values():
        m["amount"], m["weighted"] = round(m["amount"], 2), round(m["weighted"], 2)
    return masked_for(db, current_user, {"by_owner": sorted(by_owner.values(), key=lambda r: -r["open_amount"]),
                                         "by_close_month": sorted(by_month.values(), key=lambda r: r["month"] or "9999")})


@router.get("/forecast")
def forecast(fy: Optional[int] = None, member: Optional[str] = None, client_type: Optional[str] = None,
             db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Revenue forecast by quarter for one financial year, and by financial year (BR-FC-01/02).
    Each bucket counts opportunities by expected close date; totals reconcile with
    GET /opportunities?close_from=&close_to= for the same bucket and filters."""
    owners = _owners(db, current_user, member)
    stage_prob = {s.stage_id: s.probability for s in svc.stages(db, current_user.tenant_id, True)}
    fy = fy or svc.fiscal_year_of(date.today())
    base = db.query(Opportunity).filter(Opportunity.tenant_id == current_user.tenant_id,
                                        _in(Opportunity.owner_id, owners), Opportunity.close_date.isnot(None))
    if client_type:
        base = base.filter(Opportunity.client_type == client_type)

    def bucket(start: date, end: date) -> dict:
        rows = base.filter(Opportunity.close_date >= start, Opportunity.close_date < end).with_entities(
            Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category, func.count(Opportunity.opportunity_id),
            func.coalesce(func.sum(Opportunity.amount), 0)).group_by(
            Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category).all()
        b = {"close_from": start, "close_to": end - timedelta(days=1), "won": 0.0, "won_count": 0,
             "pipeline": 0.0, "weighted": 0.0, "open_count": 0, "lost": 0.0, "lost_count": 0,
             "categories": {"COMMIT": 0.0, "BEST_CASE": 0.0, "PIPELINE": 0.0, "OMITTED": 0.0}}
        for status, stage_id, category, n, amount in rows:
            if status == "OPEN":
                cat = category if category in b["categories"] else "PIPELINE"
                b["categories"][cat] += svc.as_float(amount)
            amount = svc.as_float(amount)
            if status == "WON":
                b["won"] += amount
                b["won_count"] += n
            elif status == "OPEN":
                b["pipeline"] += amount
                b["weighted"] += amount * stage_prob.get(stage_id, 0) / 100
                b["open_count"] += n
            else:
                b["lost"] += amount
                b["lost_count"] += n
        b["forecast"] = b["won"] + b["weighted"]     # expected revenue: closed plus probability-weighted open
        b["best_case"] = b["won"] + b["pipeline"]
        # Forecast categories (BR-SF-08): what reps commit to vs best case
        b["commit"] = b["won"] + b["categories"]["COMMIT"]
        b["best_case_category"] = b["commit"] + b["categories"]["BEST_CASE"]
        for k in ("won", "pipeline", "weighted", "lost", "forecast", "best_case", "commit", "best_case_category"):
            b[k] = round(b[k], 2)
        b["categories"] = {k: round(v, 2) for k, v in b["categories"].items()}
        return b

    def target_for(fy_: int, quarters) -> Optional[float]:
        if client_type:
            return None  # targets are per rep, not per client type
        q = db.query(func.coalesce(func.sum(SalesTarget.amount), 0)).filter(
            SalesTarget.tenant_id == current_user.tenant_id, SalesTarget.fy == fy_, SalesTarget.quarter.in_(quarters),
            _in(SalesTarget.user_id, owners))
        value = svc.as_float(q.scalar())
        return round(value, 2) if value else None

    fy_start, fy_end = svc.fiscal_year_bounds(fy)
    quarters = []
    for q in range(4):
        q_start = _add_months(fy_start, 3 * q)
        b = bucket(q_start, _add_months(q_start, 3))
        b["target"] = target_for(fy, [q + 1])
        b["attainment"] = _pct(b["won"], b["target"]) if b["target"] else None
        quarters.append({"quarter": f"Q{q + 1}", "label": f"Q{q + 1} {svc.fiscal_label(fy)}", **b})
    total = bucket(fy_start, fy_end)
    total["target"] = target_for(fy, [1, 2, 3, 4])
    total["attainment"] = _pct(total["won"], total["target"]) if total["target"] else None
    years = []
    for y in range(fy - 1, fy + 3):
        s, e = svc.fiscal_year_bounds(y)
        yb = bucket(s, e)
        yb["target"] = target_for(y, [1, 2, 3, 4])
        years.append({"fy": y, "label": svc.fiscal_label(y), **yb})
    undated = db.query(Opportunity).filter(Opportunity.tenant_id == current_user.tenant_id,
                                           _in(Opportunity.owner_id, owners), Opportunity.close_date.is_(None),
                                           Opportunity.status == "OPEN")
    if client_type:
        undated = undated.filter(Opportunity.client_type == client_type)
    undated_row = undated.with_entities(func.count(Opportunity.opportunity_id),
                                        func.coalesce(func.sum(Opportunity.amount), 0)).one()
    return masked_for(db, current_user, {
        "fy": fy, "label": svc.fiscal_label(fy), "start": fy_start, "end": fy_end - timedelta(days=1),
        "fiscal_year_start_month": svc.settings.FISCAL_YEAR_START_MONTH,
        "quarters": quarters, "total": total, "years": years,
        "open_without_close_date": {"count": undated_row[0], "amount": svc.as_float(undated_row[1])}})


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


@router.get("/team-performance")
def team_performance(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                     db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Scorecard per rep: activities, leads, SQLs, deals won and stage conversion (BR-TP-01).
    Managers see their own team; level 1 and admins see everyone."""
    start, end, d0, d1 = _period(date_from, date_to)
    owners = _owners(db, current_user, member)
    tenant = current_user.tenant_id
    users = db.query(User).filter(User.tenant_id == tenant, User.status != "INACTIVE",
                                  User.role.in_(("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")))
    if owners is not None:
        users = users.filter(User.user_id.in_(owners or {"-"}))
    users = users.order_by(User.sales_level.is_(None), User.sales_level, User.first_name).all()
    ids = [u.user_id for u in users]
    if not ids:
        return {"period": {"from": d0, "to": d1}, "reps": [], "totals": {}}

    def grouped(query, col):
        return _rate_rows(query.with_entities(col, func.count()).group_by(col).all())

    activities = grouped(db.query(ContactActivity).filter(ContactActivity.created_by.in_(ids),
                         ContactActivity.occurred_at >= start, ContactActivity.occurred_at < end),
                         ContactActivity.created_by)
    calls = grouped(db.query(ContactActivity).filter(ContactActivity.created_by.in_(ids),
                    ContactActivity.activity_type == "CALL",
                    ContactActivity.occurred_at >= start, ContactActivity.occurred_at < end), ContactActivity.created_by)
    meetings = grouped(db.query(ContactActivity).filter(ContactActivity.created_by.in_(ids),
                       ContactActivity.activity_type == "MEETING",
                       ContactActivity.occurred_at >= start, ContactActivity.occurred_at < end), ContactActivity.created_by)
    tasks_done = grouped(db.query(CrmTask).filter(CrmTask.owner_id.in_(ids), CrmTask.status == "DONE",
                         CrmTask.completed_at >= start, CrmTask.completed_at < end), CrmTask.owner_id)
    emails = _rate_rows(db.query(Prospect.owner_id, func.count(EmailMessage.message_id))
                        .join(EmailMessage, EmailMessage.prospect_id == Prospect.prospect_id)
                        .filter(Prospect.owner_id.in_(ids), EmailMessage.direction == "OUTBOUND",
                                EmailMessage.sent_at >= start, EmailMessage.sent_at < end)
                        .group_by(Prospect.owner_id).all())
    lead_q = db.query(Lead).filter(Lead.tenant_id == tenant, Lead.owner_id.in_(ids))
    created = lead_q.filter(Lead.created_at >= start, Lead.created_at < end)
    leads = grouped(created, Lead.owner_id)
    qualified = lead_q.filter(Lead.qualified_at >= start, Lead.qualified_at < end)
    sqls = grouped(qualified, Lead.owner_id)
    # Conversion rates follow one cohort so they never exceed 100%
    cohort_sql = grouped(created.filter(Lead.qualified_at.isnot(None)), Lead.owner_id)
    sql_converted = grouped(qualified.filter(Lead.converted_at.isnot(None)), Lead.owner_id)
    opp_q = db.query(Opportunity).filter(Opportunity.tenant_id == tenant, Opportunity.owner_id.in_(ids))
    opps = grouped(opp_q.filter(Opportunity.created_at >= start, Opportunity.created_at < end), Opportunity.owner_id)
    closed = opp_q.filter(Opportunity.closed_at >= start, Opportunity.closed_at < end)
    won_rows = {o: (int(n), svc.as_float(a)) for o, n, a in closed.filter(Opportunity.status == "WON").with_entities(
        Opportunity.owner_id, func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
    ).group_by(Opportunity.owner_id).all()}
    lost = grouped(closed.filter(Opportunity.status == "LOST"), Opportunity.owner_id)
    open_pipe = {o: svc.as_float(a) for o, a in opp_q.filter(Opportunity.status == "OPEN").with_entities(
        Opportunity.owner_id, func.coalesce(func.sum(Opportunity.amount), 0)).group_by(Opportunity.owner_id).all()}

    reps, totals = [], {k: 0 for k in ("activities", "calls", "meetings", "tasks_done", "emails_sent", "leads",
                                       "sqls", "opportunities", "won_count", "lost_count", "leads_reaching_sql",
                                       "sqls_converted")}
    totals.update(won_amount=0.0, open_pipeline=0.0)
    for u in users:
        uid = u.user_id
        r = {"user_id": uid, "name": f"{u.first_name} {u.last_name}".strip(), "sales_level": u.sales_level,
             "level_label": SALES_LEVELS.get(u.sales_level), "manager_id": u.manager_id,
             "activities": activities.get(uid, 0) + tasks_done.get(uid, 0), "calls": calls.get(uid, 0),
             "meetings": meetings.get(uid, 0), "tasks_done": tasks_done.get(uid, 0), "emails_sent": emails.get(uid, 0),
             "leads": leads.get(uid, 0), "sqls": sqls.get(uid, 0), "opportunities": opps.get(uid, 0),
             "won_count": won_rows.get(uid, (0, 0))[0], "won_amount": round(won_rows.get(uid, (0, 0.0))[1], 2),
             "lost_count": lost.get(uid, 0), "open_pipeline": round(open_pipe.get(uid, 0.0), 2)}
        r["leads_reaching_sql"] = cohort_sql.get(uid, 0)
        r["sqls_converted"] = sql_converted.get(uid, 0)
        r["lead_to_sql"] = _pct(r["leads_reaching_sql"], r["leads"])
        r["sql_to_opportunity"] = _pct(r["sqls_converted"], r["sqls"])
        r["win_rate"] = _pct(r["won_count"], r["won_count"] + r["lost_count"])
        for k in totals:
            totals[k] += r[k]
        reps.append(r)
    totals["won_amount"], totals["open_pipeline"] = round(totals["won_amount"], 2), round(totals["open_pipeline"], 2)
    totals["lead_to_sql"] = _pct(totals["leads_reaching_sql"], totals["leads"])
    totals["sql_to_opportunity"] = _pct(totals["sqls_converted"], totals["sqls"])
    totals["win_rate"] = _pct(totals["won_count"], totals["won_count"] + totals["lost_count"])
    return masked_for(db, current_user, {"period": {"from": d0, "to": d1}, "reps": reps, "totals": totals})


@router.get("/daily-limit")
def daily_limit(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Today's new-contact count against the per-user limit (BR-OV-01)."""
    from app.services.daily_limit import status_for
    return status_for(db, current_user)


# ── Targets, leaderboard, campaign ROI (BR-SF-08, 09, 11) ─────

@router.get("/targets")
def targets_vs_actual(fy: Optional[int] = None, quarter: Optional[int] = Query(None, ge=1, le=4),
                      member: Optional[str] = None, db: Session = Depends(get_db),
                      current_user: User = Depends(tenant_user)):
    """Target against won, commit and best case per rep for a fiscal quarter, or the whole year (BR-SF-09)."""
    owners = _owners(db, current_user, member)
    today = date.today()
    fy = fy or svc.fiscal_year_of(today)
    fy_start, fy_end = svc.fiscal_year_bounds(fy)
    if quarter:
        start = _add_months(fy_start, 3 * (quarter - 1))
        end, quarters = _add_months(start, 3), [quarter]
    else:
        start, end, quarters = fy_start, fy_end, [1, 2, 3, 4]
    users = db.query(User).filter(User.tenant_id == current_user.tenant_id, User.status == "ACTIVE",
                                  User.role.in_(("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")))
    if owners is not None:
        users = users.filter(User.user_id.in_(owners or {"-"}))
    users = users.all()
    ids = [u.user_id for u in users]
    targets = {uid: svc.as_float(a) for uid, a in db.query(SalesTarget.user_id, func.sum(SalesTarget.amount)).filter(
        SalesTarget.tenant_id == current_user.tenant_id, SalesTarget.fy == fy, SalesTarget.quarter.in_(quarters),
        SalesTarget.user_id.in_(ids or ["-"])).group_by(SalesTarget.user_id)}
    opps = db.query(Opportunity.owner_id, Opportunity.status, Opportunity.forecast_category,
                    func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).filter(
        Opportunity.tenant_id == current_user.tenant_id, Opportunity.owner_id.in_(ids or ["-"]),
        Opportunity.close_date >= start, Opportunity.close_date < end).group_by(
        Opportunity.owner_id, Opportunity.status, Opportunity.forecast_category).all()
    agg: Dict[str, dict] = {uid: {"won": 0.0, "won_count": 0, "commit": 0.0, "best_case": 0.0, "pipeline": 0.0} for uid in ids}
    for owner, status, cat, n, amount in opps:
        a = agg[owner]
        amount = svc.as_float(amount)
        if status == "WON":
            a["won"] += amount
            a["won_count"] += n
        elif status == "OPEN":
            key = {"COMMIT": "commit", "BEST_CASE": "best_case"}.get(cat, "pipeline" if cat != "OMITTED" else None)
            if key:
                a[key] += amount
    rows = []
    for u in users:
        a = agg[u.user_id]
        target = targets.get(u.user_id) or 0.0
        rows.append({"user_id": u.user_id, "name": f"{u.first_name} {u.last_name}".strip(), "sales_level": u.sales_level,
                     "level_label": SALES_LEVELS.get(u.sales_level), "manager_id": u.manager_id,
                     "target": round(target, 2) if target else None, "won": round(a["won"], 2), "won_count": a["won_count"],
                     "commit": round(a["won"] + a["commit"], 2), "best_case": round(a["won"] + a["commit"] + a["best_case"], 2),
                     "pipeline": round(a["pipeline"], 2),
                     "attainment": _pct(a["won"], target) if target else None,
                     "gap": round(max(target - a["won"], 0), 2) if target else None})
    rows.sort(key=lambda r: (-(r["attainment"] or -1), -r["won"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    tot_target = sum(r["target"] or 0 for r in rows)
    tot_won = sum(r["won"] for r in rows)
    totals = {"target": round(tot_target, 2) or None, "won": round(tot_won, 2),
              "commit": round(sum(r["commit"] for r in rows), 2), "best_case": round(sum(r["best_case"] for r in rows), 2),
              "attainment": _pct(tot_won, tot_target) if tot_target else None}
    return masked_for(db, current_user, {"fy": fy, "fy_label": svc.fiscal_label(fy), "quarter": quarter,
                                         "start": start, "end": end - timedelta(days=1), "rows": rows, "totals": totals})


@router.get("/leaderboard")
def leaderboard(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Reps ranked by revenue won in the period, with deals, SQLs and activity (BR-SF-09)."""
    perf = team_performance(date_from, date_to, member, db, current_user)
    rows = sorted(perf["reps"], key=lambda r: (-(r["won_amount"] or 0), -r["won_count"], -r["sqls"], -r["activities"]))
    out = [{"rank": i, "user_id": r["user_id"], "name": r["name"], "level_label": r["level_label"],
            "won_amount": r["won_amount"], "won_count": r["won_count"], "sqls": r["sqls"],
            "activities": r["activities"], "win_rate": r["win_rate"]} for i, r in enumerate(rows, 1)]
    return {"period": perf["period"], "rows": out}


@router.get("/campaign-roi")
def campaign_roi(date_from: Optional[date] = None, date_to: Optional[date] = None, member: Optional[str] = None,
                 db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Per campaign: contacts emailed, replies, leads, SQLs, opportunities, pipeline and revenue won (BR-SF-11).
    A deal belongs to the campaign its lead came from, else the last campaign that emailed the contact."""
    from app.models.campaign import Campaign
    start, end, d0, d1 = _period(date_from, date_to or None) if date_from else (None, None, None, None)
    owners = _owners(db, current_user, member)
    tenant = current_user.tenant_id
    stage_prob = {s.stage_id: s.probability for s in svc.stages(db, tenant, True)}
    campaigns = db.query(Campaign).filter(Campaign.tenant_id == tenant, Campaign.status != "DRAFT") \
        .order_by(Campaign.created_at.desc()).limit(200).all()
    ids = [c.campaign_id for c in campaigns]
    if not ids:
        return {"rows": [], "totals": {}}

    def period(col):
        return and_(col >= start, col < end) if start else True

    msgs = db.query(EmailMessage.campaign_id, EmailMessage.direction, func.count(func.distinct(EmailMessage.prospect_id))) \
        .join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id).filter(
        EmailMessage.campaign_id.in_(ids), EmailMessage.sent_at.isnot(None), period(EmailMessage.sent_at),
        _in(Prospect.owner_id, owners)).group_by(EmailMessage.campaign_id, EmailMessage.direction).all()
    sent = {c: n for c, d, n in msgs if d == "OUTBOUND"}
    replied = {c: n for c, d, n in msgs if d == "INBOUND"}
    lead_rows = db.query(Lead.campaign_id, func.count(Lead.lead_id),
                         func.sum(case((Lead.qualified_at.isnot(None), 1), else_=0))).filter(
        Lead.campaign_id.in_(ids), _in(Lead.owner_id, owners), period(Lead.created_at)).group_by(Lead.campaign_id).all()
    leads = {c: (int(n), int(q or 0)) for c, n, q in lead_rows}
    opp_rows = db.query(Opportunity.campaign_id, Opportunity.status, Opportunity.stage_id,
                        func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).filter(
        Opportunity.campaign_id.in_(ids), _in(Opportunity.owner_id, owners), period(Opportunity.created_at)).group_by(
        Opportunity.campaign_id, Opportunity.status, Opportunity.stage_id).all()
    deals: Dict[str, dict] = {}
    for cid, status, stage_id, n, amount in opp_rows:
        d = deals.setdefault(cid, {"opportunities": 0, "pipeline": 0.0, "weighted": 0.0, "won_count": 0,
                                   "revenue": 0.0, "lost_count": 0})
        amount = svc.as_float(amount)
        d["opportunities"] += n
        if status == "OPEN":
            d["pipeline"] += amount
            d["weighted"] += amount * stage_prob.get(stage_id, 0) / 100
        elif status == "WON":
            d["won_count"] += n
            d["revenue"] += amount
        else:
            d["lost_count"] += n
    rows = []
    for c in campaigns:
        d = deals.get(c.campaign_id, {})
        emailed = sent.get(c.campaign_id, 0)
        row = {"campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "status": c.status,
               "contacts_emailed": emailed, "replies": replied.get(c.campaign_id, 0),
               "leads": leads.get(c.campaign_id, (0, 0))[0], "sqls": leads.get(c.campaign_id, (0, 0))[1],
               "opportunities": d.get("opportunities", 0), "pipeline": round(d.get("pipeline", 0.0), 2),
               "weighted": round(d.get("weighted", 0.0), 2), "won_count": d.get("won_count", 0),
               "revenue": round(d.get("revenue", 0.0), 2),
               "win_rate": _pct(d.get("won_count", 0), d.get("won_count", 0) + d.get("lost_count", 0))}
        row["reply_rate"] = _pct(row["replies"], emailed)
        row["revenue_per_contact"] = round(row["revenue"] / emailed, 2) if emailed else None
        if emailed or row["leads"] or row["opportunities"]:
            rows.append(row)
    rows.sort(key=lambda r: (-r["revenue"], -r["pipeline"], -r["contacts_emailed"]))
    totals = {k: sum(r[k] for r in rows) for k in ("contacts_emailed", "replies", "leads", "sqls", "opportunities",
                                                   "won_count")}
    totals.update({k: round(sum(r[k] for r in rows), 2) for k in ("pipeline", "weighted", "revenue")})
    return masked_for(db, current_user, {"period": {"from": d0, "to": d1}, "rows": rows, "totals": totals})
