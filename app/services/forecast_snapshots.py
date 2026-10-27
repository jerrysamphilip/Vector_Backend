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

SQL lives in app/repositories/sales_forecast.py. The ``db``-taking functions keep the signatures the
worker, the KPI router and the tests call; the work is done over a ``ForecastRepository``.
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.clock import utcnow
from app.domain.sales import SALES_ROLES
from app.models.sales_extra import ForecastSnapshot
from app.repositories.sales import SqlSession
from app.repositories.sales_forecast import ForecastRepository, SqlAlchemyForecastRepository
from app.services import sales as svc
from app.services.crm_kpis import forecast_accuracy_pct, variance_accuracy_pct

logger = logging.getLogger(__name__)

PERIOD_TYPES = ("MONTH", "QUARTER")
# The worker only takes a scheduled snapshot this many days into the period; later than that
# it would no longer be a forecast made at the start (an admin can still take one by hand)
SCHEDULED_GRACE_DAYS = 5
__all__ = ["PERIOD_TYPES", "SALES_ROLES", "SCHEDULED_GRACE_DAYS", "actual_won", "add_months", "forecast_by_owner",
           "history", "period_bounds", "period_figures", "period_label", "run_due", "take_snapshot"]


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


def _repo(db: SqlSession) -> ForecastRepository:
    return SqlAlchemyForecastRepository(db)


def owner_forecasts(repo: ForecastRepository, tenant_id: str, start: date, end: date) -> Dict[str, dict]:
    """{owner_id: {won, weighted, open_count, commit_open}} for deals with a close date in [start, end).
    commit_open: open deals whose forecast category is Commit (the stored category, else the stage's default)."""
    stages = repo.stages(tenant_id)
    out: Dict[str, dict] = {}
    for owner, status, stage_id, category, n, amount in repo.grouped_deals(tenant_id, start, end):
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


def forecast_by_owner(db: SqlSession, tenant_id: str, start: date, end: date) -> Dict[str, dict]:
    return owner_forecasts(_repo(db), tenant_id, start, end)


def actual_won(db: SqlSession, tenant_id: str, start: date, end: date, owners: Optional[set]) -> float:
    return svc.as_float(_repo(db).won_amount(tenant_id, start, end, owners))


def snapshot_period(repo: ForecastRepository, tenant_id: str, period_type: str, day: Optional[date] = None,
                    source: str = "SCHEDULED", taken_by: Optional[str] = None, replace: bool = False) -> dict:
    """Store the forecast of the period containing `day` for every sales user (and any other deal
    owner). An existing snapshot is kept unless replace. Caller commits."""
    start, end = period_bounds(period_type, day or date.today())
    result: Dict[str, Any] = {"period_type": period_type, "period_start": start, "period_end": end - timedelta(days=1),
                              "label": period_label(period_type, start)}
    existing = repo.snapshot_count(tenant_id, period_type, start)
    if existing:
        if not replace:
            return {**result, "status": "exists", "rows": existing}
        repo.delete_snapshots(tenant_id, period_type, start)
    by_owner = owner_forecasts(repo, tenant_id, start, end)
    users = repo.active_sales_users(tenant_id, SALES_ROLES)
    now = utcnow()
    for owner in sorted(users | set(by_owner)):
        f = by_owner.get(owner, _empty_forecast())
        repo.add(ForecastSnapshot(tenant_id=tenant_id, period_type=period_type, period_start=start, period_end=end,
                                  owner_id=owner, won=round(f["won"], 2), weighted=round(f["weighted"], 2),
                                  forecast=round(f["won"] + f["weighted"], 2), open_count=f["open_count"],
                                  commit_open=round(f["commit_open"], 2),
                                  committed=round(f["won"] + f["commit_open"], 2),
                                  source=source, taken_by=taken_by, taken_at=now))
    repo.flush()
    return {**result, "status": "replaced" if replace else "taken", "rows": len(users | set(by_owner))}


def take_snapshot(db: SqlSession, tenant_id: str, period_type: str, day: Optional[date] = None,
                  source: str = "SCHEDULED", taken_by: Optional[str] = None, replace: bool = False) -> dict:
    """Store the forecast of the period containing `day` (see snapshot_period). Caller commits."""
    return snapshot_period(_repo(db), tenant_id, period_type, day, source, taken_by, replace)


def run_due(db: SqlSession, today: Optional[date] = None) -> int:
    """Worker: take this period's snapshot for each workspace that has none yet, early in the period."""
    today = today or date.today()
    repo = _repo(db)
    taken = 0
    for tenant_id in repo.active_tenant_ids():
        for period_type in PERIOD_TYPES:
            start, _ = period_bounds(period_type, today)
            if today >= start + timedelta(days=SCHEDULED_GRACE_DAYS):
                continue
            try:
                if snapshot_period(repo, tenant_id, period_type, today)["status"] == "taken":
                    taken += 1
                db.commit()
            except Exception as exc:  # one workspace never blocks the others
                db.rollback()
                logger.warning("[Forecast] Snapshot for %s %s failed: %s", tenant_id, period_type, exc)
    return taken


def _sum(values: List[Any]) -> float:
    return round(sum(svc.as_float(v) for v in values), 2)


def _snapshot_figures(rows: List[Any], committed: Optional[float]) -> dict:
    taken = [r.taken_at for r in rows if r.taken_at]
    return {
        "committed": committed,
        "commit_open_at_snapshot": _sum([r.commit_open for r in rows if r.commit_open is not None])
        if committed is not None else None,
        "forecast": _sum([r.forecast for r in rows]), "won_at_snapshot": _sum([r.won for r in rows]),
        "weighted_at_snapshot": _sum([r.weighted for r in rows]),
        "taken_at": min(taken) if taken else None,
        "source": rows[0].source if rows else None,
    }


def period_figures(db: SqlSession, tenant_id: str, period_type: str, start: date, owners: Optional[set],
                   today: Optional[date] = None) -> dict:
    """Snapshot (if any) and actual figures for one period, limited to the owners."""
    today = today or date.today()
    repo = _repo(db)
    has_snapshot = repo.snapshot_count(tenant_id, period_type, start) > 0
    # A snapshot taken before 0014 has no committed figure on any row: its accuracy is unknown
    legacy = has_snapshot and not repo.has_committed(tenant_id, period_type, start)
    rows = repo.snapshot_rows(tenant_id, period_type, start, owners)
    end = rows[0].period_end if rows else period_bounds(period_type, start)[1]
    committed = None
    if has_snapshot and not legacy:
        committed = _sum([r.committed for r in rows if r.committed is not None])
    snap = _snapshot_figures(rows, committed)
    forecast = snap["forecast"]
    actual = round(svc.as_float(repo.won_amount(tenant_id, start, end, owners)), 2)
    closed = end <= today
    return {
        "period_type": period_type, "period_start": start, "period_end": end - timedelta(days=1),
        "label": period_label(period_type, start), "closed": closed, "has_snapshot": has_snapshot,
        "legacy_snapshot": legacy,
        "committed": committed, "commit_open_at_snapshot": snap["commit_open_at_snapshot"],
        "forecast": forecast, "won_at_snapshot": snap["won_at_snapshot"],
        "weighted_at_snapshot": snap["weighted_at_snapshot"],
        "actual": actual,
        "variance": round(actual - committed, 2) if committed is not None else None,
        # BRD: actual revenue / committed forecast x 100. Final once the period has ended;
        # accuracy_to_date shows progress for the current one.
        "accuracy": forecast_accuracy_pct(actual, committed) if closed else None,
        "accuracy_to_date": forecast_accuracy_pct(actual, committed),
        # Secondary: closeness to the probability-weighted forecast (the previous measure)
        "weighted_accuracy": variance_accuracy_pct(actual, forecast) if closed else None,
        "weighted_variance": round(actual - forecast, 2),
        "taken_at": snap["taken_at"],
        "source": snap["source"],
    }


def history(db: SqlSession, tenant_id: str, period_type: str, owners: Optional[set], periods: int,
            today: Optional[date] = None) -> List[dict]:
    """Snapshotted periods, newest first, with committed and weighted forecast, actual won and accuracy."""
    starts = _repo(db).period_starts(tenant_id, period_type, periods)
    return [period_figures(db, tenant_id, period_type, start, owners, today) for start in starts]
