"""
Forecast snapshots for forecast accuracy (BRD v2.0 section 8).

At the start of every month and fiscal quarter the sales_jobs worker loop stores, per owner, the
forecast for the period, counting the deals whose expected close date falls in it:

  committed  closed won to date + open deals in the Commit forecast category (BR-SF-08)
  forecast   closed won + probability-weighted open pipeline (the Forecast page's measure)

The BRD's forecast accuracy is actual revenue / committed forecast x 100, where actual revenue is
what was won with a close date in the period: 100% means the team won exactly what it committed,
above 100% more, below less. It is None when nothing was committed (or for snapshots taken before
the committed figure was stored). The older variance measure, 1 - |actual - forecast| / forecast on
the weighted forecast, is kept as a secondary figure (weighted_accuracy).
"""
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.sales import Opportunity, SalesStage
from app.models.sales_extra import ForecastSnapshot
from app.models.tenant import Tenant
from app.models.user import User
from app.services import sales as svc
from app.services.crm_kpis import forecast_accuracy_pct, variance_accuracy_pct

logger = logging.getLogger(__name__)

PERIOD_TYPES = ("MONTH", "QUARTER")
# The worker only takes a scheduled snapshot this many days into the period; later than that
# it would no longer be a forecast made at the start (an admin can still take one by hand)
SCHEDULED_GRACE_DAYS = 5
SALES_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_bounds(period_type: str, day: date) -> Tuple[date, date]:
    """[start, end) of the calendar month or fiscal quarter containing day."""
    if period_type == "MONTH":
        start = date(day.year, day.month, 1)
        return start, add_months(start, 1)
    fy_start, _ = svc.fiscal_year_bounds(svc.fiscal_year_of(day))
    start = add_months(fy_start, 3 * (svc.fiscal_quarter_of(day) - 1))
    return start, add_months(start, 3)


def period_label(period_type: str, start: date) -> str:
    if period_type == "MONTH":
        return start.strftime("%b %Y")
    return f"Q{svc.fiscal_quarter_of(start)} {svc.fiscal_label(svc.fiscal_year_of(start))}"


def _empty_forecast() -> dict:
    return {"won": 0.0, "weighted": 0.0, "open_count": 0, "commit_open": 0.0}


def forecast_by_owner(db: Session, tenant_id: str, start: date, end: date) -> Dict[str, dict]:
    """{owner_id: {won, weighted, open_count, commit_open}} for deals with a close date in [start, end).
    commit_open: open deals whose forecast category is Commit (the stored category, else the stage's default)."""
    stages = {s.stage_id: s for s in db.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)}
    rows = db.query(Opportunity.owner_id, Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category,
                    func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).filter(
        Opportunity.tenant_id == tenant_id, Opportunity.close_date >= start, Opportunity.close_date < end,
        Opportunity.status.in_(("WON", "OPEN"))).group_by(
        Opportunity.owner_id, Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category).all()
    out: Dict[str, dict] = {}
    for owner, status, stage_id, category, n, amount in rows:
        o = out.setdefault(owner, _empty_forecast())
        amount = svc.as_float(amount)
        if status == "WON":
            o["won"] += amount
            continue
        stage = stages.get(stage_id)
        o["weighted"] += amount * (stage.probability if stage else 0) / 100
        o["open_count"] += int(n)
        if (category or (svc.category_for(stage) if stage else "PIPELINE")) == "COMMIT":
            o["commit_open"] += amount
    return out


def actual_won(db: Session, tenant_id: str, start: date, end: date, owners: Optional[set]) -> float:
    q = db.query(func.coalesce(func.sum(Opportunity.amount), 0)).filter(
        Opportunity.tenant_id == tenant_id, Opportunity.status == "WON",
        Opportunity.close_date >= start, Opportunity.close_date < end)
    if owners is not None:
        q = q.filter(Opportunity.owner_id.in_(owners or {"-"}))
    return svc.as_float(q.scalar())


def take_snapshot(db: Session, tenant_id: str, period_type: str, day: Optional[date] = None,
                  source: str = "SCHEDULED", taken_by: Optional[str] = None, replace: bool = False) -> dict:
    """Store the forecast of the period containing `day` for every sales user (and any other deal
    owner). An existing snapshot is kept unless replace. Caller commits."""
    start, end = period_bounds(period_type, day or date.today())
    existing = db.query(ForecastSnapshot).filter(
        ForecastSnapshot.tenant_id == tenant_id, ForecastSnapshot.period_type == period_type,
        ForecastSnapshot.period_start == start)
    result = {"period_type": period_type, "period_start": start, "period_end": end - timedelta(days=1),
              "label": period_label(period_type, start)}
    if existing.first():
        if not replace:
            return {**result, "status": "exists", "rows": existing.count()}
        existing.delete(synchronize_session=False)
    by_owner = forecast_by_owner(db, tenant_id, start, end)
    users = {u for (u,) in db.query(User.user_id).filter(User.tenant_id == tenant_id, User.status == "ACTIVE",
                                                          User.role.in_(SALES_ROLES))}
    now = utcnow()
    for owner in sorted(users | set(by_owner)):
        f = by_owner.get(owner, _empty_forecast())
        db.add(ForecastSnapshot(tenant_id=tenant_id, period_type=period_type, period_start=start, period_end=end,
                                owner_id=owner, won=round(f["won"], 2), weighted=round(f["weighted"], 2),
                                forecast=round(f["won"] + f["weighted"], 2), open_count=f["open_count"],
                                commit_open=round(f["commit_open"], 2),
                                committed=round(f["won"] + f["commit_open"], 2),
                                source=source, taken_by=taken_by, taken_at=now))
    db.flush()
    return {**result, "status": "replaced" if replace else "taken", "rows": len(users | set(by_owner))}


def run_due(db: Session, today: Optional[date] = None) -> int:
    """Worker: take this period's snapshot for each workspace that has none yet, early in the period."""
    today = today or date.today()
    taken = 0
    for (tenant_id,) in db.query(Tenant.tenant_id).filter(Tenant.status == "ACTIVE"):
        for period_type in PERIOD_TYPES:
            start, _ = period_bounds(period_type, today)
            if today >= start + timedelta(days=SCHEDULED_GRACE_DAYS):
                continue
            try:
                if take_snapshot(db, tenant_id, period_type, today)["status"] == "taken":
                    taken += 1
                db.commit()
            except Exception as exc:  # one workspace never blocks the others
                db.rollback()
                logger.warning("[Forecast] Snapshot for %s %s failed: %s", tenant_id, period_type, exc)
    return taken


def period_figures(db: Session, tenant_id: str, period_type: str, start: date, owners: Optional[set],
                   today: Optional[date] = None) -> dict:
    """Snapshot (if any) and actual figures for one period, limited to the owners."""
    today = today or date.today()
    q = db.query(ForecastSnapshot).filter(ForecastSnapshot.tenant_id == tenant_id,
                                          ForecastSnapshot.period_type == period_type,
                                          ForecastSnapshot.period_start == start)
    has_snapshot = q.first() is not None
    # A snapshot taken before 0014 has no committed figure on any row: its accuracy is unknown
    legacy = has_snapshot and q.filter(ForecastSnapshot.committed.isnot(None)).first() is None
    if owners is not None:
        q = q.filter(ForecastSnapshot.owner_id.in_(owners or {"-"}))
    rows = q.all()
    end = rows[0].period_end if rows else period_bounds(period_type, start)[1]
    forecast = round(sum(svc.as_float(r.forecast) for r in rows), 2)
    committed = None
    if has_snapshot and not legacy:
        committed = round(sum(svc.as_float(r.committed) for r in rows if r.committed is not None), 2)
    actual = round(actual_won(db, tenant_id, start, end, owners), 2)
    closed = end <= today
    taken = [r.taken_at for r in rows if r.taken_at]
    return {
        "period_type": period_type, "period_start": start, "period_end": end - timedelta(days=1),
        "label": period_label(period_type, start), "closed": closed, "has_snapshot": has_snapshot,
        "legacy_snapshot": legacy,
        "committed": committed,
        "commit_open_at_snapshot": round(sum(svc.as_float(r.commit_open) for r in rows if r.commit_open is not None), 2)
        if committed is not None else None,
        "forecast": forecast, "won_at_snapshot": round(sum(svc.as_float(r.won) for r in rows), 2),
        "weighted_at_snapshot": round(sum(svc.as_float(r.weighted) for r in rows), 2),
        "actual": actual,
        "variance": round(actual - committed, 2) if committed is not None else None,
        # BRD: actual revenue / committed forecast x 100. Final once the period has ended;
        # accuracy_to_date shows progress for the current one.
        "accuracy": forecast_accuracy_pct(actual, committed) if closed else None,
        "accuracy_to_date": forecast_accuracy_pct(actual, committed),
        # Secondary: closeness to the probability-weighted forecast (the previous measure)
        "weighted_accuracy": variance_accuracy_pct(actual, forecast) if closed else None,
        "weighted_variance": round(actual - forecast, 2),
        "taken_at": min(taken) if taken else None,
        "source": rows[0].source if rows else None,
    }


def history(db: Session, tenant_id: str, period_type: str, owners: Optional[set], periods: int,
            today: Optional[date] = None) -> List[dict]:
    """Snapshotted periods, newest first, with committed and weighted forecast, actual won and accuracy."""
    starts = [s for (s,) in db.query(ForecastSnapshot.period_start).filter(
        ForecastSnapshot.tenant_id == tenant_id, ForecastSnapshot.period_type == period_type).distinct()
        .order_by(ForecastSnapshot.period_start.desc()).limit(periods)]
    return [period_figures(db, tenant_id, period_type, start, owners, today) for start in starts]
