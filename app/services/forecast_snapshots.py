"""
Forecast snapshots for forecast accuracy (BRD v2.0 section 8).

At the start of every month and fiscal quarter the sales_jobs worker loop stores, per owner,
the forecast for the period: closed won plus the probability-weighted open pipeline of the
deals whose expected close date falls in the period (the same measure as the Forecast page).
At the end of the period it is compared with what was actually won (deals won with a close
date in the period): accuracy = 1 - |actual - forecast| / forecast.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.sales import Opportunity, SalesStage
from app.models.sales_extra import ForecastSnapshot
from app.models.tenant import Tenant
from app.models.user import User
from app.services import sales as svc

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


def forecast_by_owner(db: Session, tenant_id: str, start: date, end: date) -> Dict[str, dict]:
    """{owner_id: {won, weighted, open_count}} for deals with a close date in [start, end)."""
    prob = {s.stage_id: s.probability for s in db.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)}
    rows = db.query(Opportunity.owner_id, Opportunity.status, Opportunity.stage_id,
                    func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).filter(
        Opportunity.tenant_id == tenant_id, Opportunity.close_date >= start, Opportunity.close_date < end,
        Opportunity.status.in_(("WON", "OPEN"))).group_by(
        Opportunity.owner_id, Opportunity.status, Opportunity.stage_id).all()
    out: Dict[str, dict] = {}
    for owner, status, stage_id, n, amount in rows:
        o = out.setdefault(owner, {"won": 0.0, "weighted": 0.0, "open_count": 0})
        if status == "WON":
            o["won"] += svc.as_float(amount)
        else:
            o["weighted"] += svc.as_float(amount) * prob.get(stage_id, 0) / 100
            o["open_count"] += int(n)
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
    now = datetime.utcnow()
    for owner in sorted(users | set(by_owner)):
        f = by_owner.get(owner, {"won": 0.0, "weighted": 0.0, "open_count": 0})
        db.add(ForecastSnapshot(tenant_id=tenant_id, period_type=period_type, period_start=start, period_end=end,
                                owner_id=owner, won=round(f["won"], 2), weighted=round(f["weighted"], 2),
                                forecast=round(f["won"] + f["weighted"], 2), open_count=f["open_count"],
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


def accuracy_pct(actual: float, forecast: float) -> Optional[float]:
    if not forecast:
        return None
    return round(100.0 * (1 - abs(actual - forecast) / forecast), 1)


def history(db: Session, tenant_id: str, period_type: str, owners: Optional[set], periods: int,
            today: Optional[date] = None) -> List[dict]:
    """Snapshotted periods, newest first, with forecast, actual won and accuracy for the owners."""
    today = today or date.today()
    starts = [s for (s,) in db.query(ForecastSnapshot.period_start).filter(
        ForecastSnapshot.tenant_id == tenant_id, ForecastSnapshot.period_type == period_type).distinct()
        .order_by(ForecastSnapshot.period_start.desc()).limit(periods)]
    out = []
    for start in starts:
        q = db.query(ForecastSnapshot).filter(ForecastSnapshot.tenant_id == tenant_id,
                                              ForecastSnapshot.period_type == period_type,
                                              ForecastSnapshot.period_start == start)
        if owners is not None:
            q = q.filter(ForecastSnapshot.owner_id.in_(owners or {"-"}))
        rows = q.all()
        end = rows[0].period_end if rows else period_bounds(period_type, start)[1]
        forecast = round(sum(svc.as_float(r.forecast) for r in rows), 2)
        actual = round(actual_won(db, tenant_id, start, end, owners), 2)
        closed = end <= today
        taken = [r.taken_at for r in rows if r.taken_at]
        out.append({
            "period_type": period_type, "period_start": start, "period_end": end - timedelta(days=1),
            "label": period_label(period_type, start), "closed": closed,
            "forecast": forecast, "won_at_snapshot": round(sum(svc.as_float(r.won) for r in rows), 2),
            "weighted_at_snapshot": round(sum(svc.as_float(r.weighted) for r in rows), 2),
            "actual": actual, "variance": round(actual - forecast, 2),
            # Only a finished period has a final accuracy; the current one shows progress so far
            "accuracy": accuracy_pct(actual, forecast) if closed else None,
            "taken_at": min(taken) if taken else None,
            "source": rows[0].source if rows else None,
        })
    return out
