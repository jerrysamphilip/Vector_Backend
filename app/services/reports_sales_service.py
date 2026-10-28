"""
Sales reports (BRD v2.0 5.8 - 5.10): role dashboards, funnel, leads / SQL / pipeline reports, quarter and
financial-year forecast, team performance, targets, leaderboard, campaign ROI, SQL-to-win and forecast
accuracy (GET /sales-reports/*; chunk A6 moved them out of the routers).

Every figure is limited to the viewer's records and their team's (BR-SH-02); a ``member`` filter
narrows to one person and everyone below them. Amounts are blanked for roles that may not see them
(BR-SF-12), and nothing is ordered by a hidden amount. SQL: app/repositories/reports_sales.py.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Set

from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.domain.reports import ReportInputError, ReportViewer, SalesPeriod, add_months, pct, quarter_bounds
from app.models.sales import LEAD_STAGES
from app.repositories.base import SqlAlchemyUnitOfWork, UnitOfWork
from app.repositories.reports_sales import (
    OpportunityCounts,
    SalesReportRepository,
    Session,
    SqlAlchemySalesReportRepository,
)
from app.services import forecast_snapshots as fs
from app.services import sales as svc
from app.services.contact_service import SALES_LEVELS
from app.services.reports_access import ReportAccess, SessionReportAccess
from app.services.sales_settings import MONEY_KEYS

ACCURACY_MONEY_KEYS = MONEY_KEYS | {"actual", "variance", "won_at_snapshot", "weighted_at_snapshot",
                                    "average_forecast", "average_actual", "committed", "commit_open_at_snapshot",
                                    "weighted_variance"}
OUTCOME_RANK = {"WON": 3, "OPEN": 2, "LOST": 1}
QUEUE_SIZE = 10
ROI_CAMPAIGNS = 200

Owners = Optional[Set[str]]


def _input(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except ReportInputError as exc:
        raise BadRequestError(exc.message) from None


def sales_period(date_from: Optional[date], date_to: Optional[date], today: date) -> SalesPeriod:
    """Inclusive dates; default the last 90 days; 400 when date_from is after date_to."""
    return _input(lambda: SalesPeriod.or_default(date_from, date_to, today))


def kpi_period(date_from: Optional[date], date_to: Optional[date], fy: Optional[int], quarter: Optional[int],
               today: date) -> SalesPeriod:
    """A fiscal year / quarter when fy is given, else the date range (default: the last 90 days)."""
    if quarter and not fy:
        raise BadRequestError("quarter needs fy")
    if fy:
        return SalesPeriod.of_bounds(*quarter_bounds(*svc.fiscal_year_bounds(fy), quarter))
    return sales_period(date_from, date_to, today)


def _name(u: Any) -> str:
    return f"{u.first_name} {u.last_name}".strip()


def _measures(c: OpportunityCounts, stage_prob: Dict[str, int]) -> dict:
    open_amount = sum(svc.as_float(a) for _, _, a in c.open_by_stage)
    weighted = sum(svc.as_float(a) * stage_prob.get(sid, 0) / 100 for sid, _, a in c.open_by_stage)
    won_n, won_amt = c.won_count, svc.as_float(c.won_amount)
    return {
        "new_leads": c.new_leads, "sqls": c.sqls, "open_sqls": c.open_sqls,
        "opportunities_created": c.opportunities_created,
        "open_opportunities": sum(n for _, n, _ in c.open_by_stage), "open_pipeline": round(open_amount, 2),
        "weighted_pipeline": round(weighted, 2), "won_count": won_n, "won_amount": round(won_amt, 2),
        "lost_count": c.lost_count, "win_rate": pct(won_n, won_n + c.lost_count),
        "average_deal": round(won_amt / won_n, 2) if won_n else None,
    }


def _funnel_steps(e: dict, m: dict) -> List[dict]:
    steps = [("sent", "Contacts emailed", e["contacts_emailed"]), ("replied", "Replied", e["contacts_replied"]),
             ("lead", "Leads", m["new_leads"]), ("sql", "SQLs", m["sqls"]),
             ("opportunity", "Opportunities", m["opportunities_created"]), ("won", "Won", m["won_count"])]
    out: List[dict] = []
    prev: Optional[int] = None
    for key, label, n in steps:
        # Step counts are period volumes, not one cohort, so a step can exceed the one before
        # (e.g. deals created directly); a ratio over 100% would mislead, so none is shown.
        ratio = pct(n, prev) if prev is not None else None
        first = pct(n, steps[0][2]) if steps[0][2] else None
        out.append({"key": key, "label": label, "count": n,
                    "from_previous": ratio if ratio is not None and ratio <= 100 else None,
                    "from_first": first if first is not None and first <= 100 else None})
        prev = n
    return out


def _forecast_bucket(rows: List[tuple], start: date, end: date, stage_prob: Dict[str, int]) -> dict:
    b: Dict[str, Any] = {"close_from": start, "close_to": end - timedelta(days=1), "won": 0.0, "won_count": 0,
                         "pipeline": 0.0, "weighted": 0.0, "open_count": 0, "lost": 0.0, "lost_count": 0,
                         "categories": {"COMMIT": 0.0, "BEST_CASE": 0.0, "PIPELINE": 0.0, "OMITTED": 0.0}}
    for status, stage_id, category, n, amount in rows:
        amount = svc.as_float(amount)
        if status == "WON":
            b["won"] += amount
            b["won_count"] += n
        elif status == "OPEN":
            cat = category if category in b["categories"] else "PIPELINE"
            b["categories"][cat] += amount
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


def _pipeline_groups(rows: List[tuple], names: Dict[str, str], stage_prob: Dict[str, int]) -> tuple[dict, dict]:
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
        o["win_rate"] = pct(o["won_count"], o["won_count"] + o["lost_count"])
        for k in ("open_amount", "weighted", "won_amount"):
            o[k] = round(o[k], 2)
    for m in by_month.values():
        m["amount"], m["weighted"] = round(m["amount"], 2), round(m["weighted"], 2)
    return by_owner, by_month


def _rate_rows(rep: dict) -> None:
    rep["lead_to_sql"] = pct(rep["leads_reaching_sql"], rep["leads"])
    rep["sql_to_opportunity"] = pct(rep["sqls_converted"], rep["sqls"])
    rep["win_rate"] = pct(rep["won_count"], rep["won_count"] + rep["lost_count"])


TOTAL_KEYS = ("activities", "calls", "meetings", "tasks_done", "emails_sent", "leads", "sqls", "opportunities",
              "won_count", "lost_count", "leads_reaching_sql", "sqls_converted")


def _rep_row(u: Any, c: Any) -> dict:
    uid = u.user_id
    won = c.won.get(uid, (0, 0))
    r = {"user_id": uid, "name": _name(u), "sales_level": u.sales_level,
         "level_label": SALES_LEVELS.get(u.sales_level), "manager_id": u.manager_id,
         "activities": c.activities.get(uid, 0) + c.tasks_done.get(uid, 0), "calls": c.calls.get(uid, 0),
         "meetings": c.meetings.get(uid, 0), "tasks_done": c.tasks_done.get(uid, 0), "emails_sent": c.emails.get(uid, 0),
         "leads": c.leads.get(uid, 0), "sqls": c.sqls.get(uid, 0), "opportunities": c.opportunities.get(uid, 0),
         "won_count": won[0], "won_amount": round(svc.as_float(won[1]), 2),
         "lost_count": c.lost.get(uid, 0), "open_pipeline": round(svc.as_float(c.open_pipeline.get(uid, 0.0)), 2)}
    r["leads_reaching_sql"] = c.leads_reaching_sql.get(uid, 0)
    r["sqls_converted"] = c.sqls_converted.get(uid, 0)
    _rate_rows(r)
    return r


def _target_row(u: Any, a: dict, target: float) -> dict:
    return {"user_id": u.user_id, "name": _name(u), "sales_level": u.sales_level,
            "level_label": SALES_LEVELS.get(u.sales_level), "manager_id": u.manager_id,
            "target": round(target, 2) if target else None, "won": round(a["won"], 2), "won_count": a["won_count"],
            "commit": round(a["won"] + a["commit"], 2), "best_case": round(a["won"] + a["commit"] + a["best_case"], 2),
            "pipeline": round(a["pipeline"], 2),
            "attainment": pct(a["won"], target) if target else None,
            "gap": round(max(target - a["won"], 0), 2) if target else None}


def _target_agg(ids: List[str], rows: List[tuple]) -> Dict[str, dict]:
    agg: Dict[str, dict] = {uid: {"won": 0.0, "won_count": 0, "commit": 0.0, "best_case": 0.0, "pipeline": 0.0}
                            for uid in ids}
    for owner, status, cat, n, amount in rows:
        a = agg[owner]
        amount = svc.as_float(amount)
        if status == "WON":
            a["won"] += amount
            a["won_count"] += n
        elif status == "OPEN":
            key = {"COMMIT": "commit", "BEST_CASE": "best_case"}.get(cat, "pipeline" if cat != "OMITTED" else None)
            if key:
                a[key] += amount
    return agg


def _roi_deals(rows: List[tuple], stage_prob: Dict[str, int]) -> Dict[str, dict]:
    deals: Dict[str, dict] = {}
    for cid, status, stage_id, n, amount in rows:
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
    return deals


def _roi_row(c: Any, d: dict, emailed: int, replies: int, leads: tuple) -> dict:
    row = {"campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "status": c.status,
           "contacts_emailed": emailed, "replies": replies, "leads": leads[0], "sqls": leads[1],
           "opportunities": d.get("opportunities", 0), "pipeline": round(d.get("pipeline", 0.0), 2),
           "weighted": round(d.get("weighted", 0.0), 2), "won_count": d.get("won_count", 0),
           "revenue": round(d.get("revenue", 0.0), 2),
           "win_rate": pct(d.get("won_count", 0), d.get("won_count", 0) + d.get("lost_count", 0))}
    row["reply_rate"] = pct(row["replies"], emailed)
    row["revenue_per_contact"] = round(row["revenue"] / emailed, 2) if emailed else None
    return row


def _roi_summary(rows: List[dict], show_amounts: bool) -> dict:
    if show_amounts:
        rows.sort(key=lambda r: (-r["revenue"], -r["pipeline"], -r["contacts_emailed"]))
    else:  # ordering by revenue would reveal it (BR-SF-12)
        rows.sort(key=lambda r: (-r["won_count"], -r["opportunities"], -r["contacts_emailed"]))
    totals: Dict[str, Any] = {k: sum(r[k] for r in rows)
                              for k in ("contacts_emailed", "replies", "leads", "sqls", "opportunities", "won_count")}
    totals.update({k: round(sum(r[k] for r in rows), 2) for k in ("pipeline", "weighted", "revenue")})
    return {"rows": rows, "totals": totals}


def _sql_outcome(deals: List[Any]) -> str:
    if not deals:
        return "no_deal"
    best = max(deals, key=lambda d: OUTCOME_RANK.get(d.status, 0))
    return {"WON": "won", "LOST": "lost", "OPEN": "open"}.get(best.status, "open")


class SalesReports:
    """GET /sales-reports/* for one viewer."""

    def __init__(self, repository: SalesReportRepository, access: ReportAccess, uow: Optional[UnitOfWork] = None,
                 today: Callable[[], date] = date.today) -> None:
        self.repo = repository
        self.access = access
        self.uow = uow
        self._today = today

    @classmethod
    def for_session(cls, db: Session) -> "SalesReports":
        return cls(SqlAlchemySalesReportRepository(db), SessionReportAccess(db), SqlAlchemyUnitOfWork.for_session(db))

    # ── scope ──

    def owners(self, user: ReportViewer, member: Optional[str]) -> Owners:
        """Owner ids to report on; None = the whole workspace."""
        visible = self.access.visible_user_ids(user)
        if not member:
            return visible
        target = self.repo.tenant_user(user.tenant_id, member)
        if not target or not self.access.can_see_owner(user, member):
            raise NotFoundError("Team member not found")
        team = self.access.team_user_ids(target)
        return team if visible is None else team & visible

    def period(self, date_from: Optional[date], date_to: Optional[date]) -> SalesPeriod:
        return sales_period(date_from, date_to, self._today())

    def measures(self, tenant_id: str, owners: Owners, p: SalesPeriod) -> dict:
        counts = self.repo.opportunity_counts(tenant_id, owners, p.start, p.end)
        return _measures(counts, self.repo.stage_probabilities(tenant_id))

    def email_measures(self, tenant_id: str, owners: Owners, p: SalesPeriod) -> dict:
        sent, replied = self.repo.email_counts(tenant_id, owners, p.start, p.end)
        return {"contacts_emailed": sent, "contacts_replied": replied}

    def role_view(self, user: ReportViewer) -> str:
        if self.access.sees_everything(user):
            return "SALES_HEAD"
        if user.sales_level == 2:
            return "BUSINESS_DEVELOPMENT"
        return "BUSINESS_EXECUTIVE"

    def team_groups(self, user: ReportViewer) -> List[Any]:
        """Whose rollups a dashboard breaks down by: the viewer's direct reports, or for workspace-wide
        viewers the level 2 (Business Development) heads."""
        direct = self.repo.direct_reports(user.tenant_id, user.user_id)
        if direct:
            return direct
        if self.access.sees_everything(user):
            return self.repo.users_at_level(user.tenant_id, 2) or self.repo.users_at_level(user.tenant_id, None)
        return []

    # ── endpoints ──

    def dashboard(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date],
                  member: Optional[str]) -> dict:
        """Role dashboard (BR-DR-01)."""
        p = self.period(date_from, date_to)
        owners = self.owners(user, member)
        tenant = user.tenant_id
        kpis = {**self.measures(tenant, owners, p), **self.email_measures(tenant, owners, p)}
        teams = []
        for head in self.team_groups(user):
            team = self.access.team_user_ids(head)
            if owners is not None:
                team &= owners
            teams.append({"user_id": head.user_id, "name": _name(head), "sales_level": head.sales_level,
                          "level_label": SALES_LEVELS.get(head.sales_level), "members": len(team),
                          **self.measures(tenant, team, p)})
        return self.access.masked(user, {
            "role_view": self.role_view(user), "period": p.as_dict(), "kpis": kpis, "teams": teams,
            "queue": self._queue(user, p), "funnel": self.funnel(user, p.first, p.last, member)["stages"],
            "leaderboard": self.leaderboard(user, p.first, p.last, member)["rows"][:5]})

    def _queue(self, user: ReportViewer, p: SalesPeriod) -> dict:
        """The viewer's own work queue."""
        tenant, uid, today = user.tenant_id, user.user_id, self._today()
        due_before = datetime.combine(today + timedelta(days=1), time.min)
        return {
            "sql_leads": self.access.lead_dicts(self.repo.sql_leads(tenant, uid, QUEUE_SIZE)),
            "next_steps_due": self.access.lead_dicts(self.repo.leads_due(tenant, uid, due_before, QUEUE_SIZE)),
            "deals_closing": self.access.opp_dicts(self.repo.deals_closing(tenant, uid, today + timedelta(days=30),
                                                                           QUEUE_SIZE)),
            "my": self.measures(tenant, {uid}, p),
        }

    def funnel(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date],
               member: Optional[str]) -> dict:
        """Sent -> replied -> lead -> SQL -> opportunity -> won for the period (BR-DR-03)."""
        p = self.period(date_from, date_to)
        owners = self.owners(user, member)
        e = self.email_measures(user.tenant_id, owners, p)
        m = self.measures(user.tenant_id, owners, p)
        return self.access.masked(user, {"period": p.as_dict(), "stages": _funnel_steps(e, m),
                                         "won_amount": m["won_amount"]})

    def leads_report(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date],
                     member: Optional[str]) -> dict:
        """Leads and SQLs created in the period: by stage, source and owner (BR-DR-02)."""
        p = self.period(date_from, date_to)
        owners = self.owners(user, member)
        r = self.repo.lead_report(user.tenant_id, owners, p.start, p.end)
        names = self.access.user_names([row[0] for row in r.by_owner])
        total = sum(r.by_stage.values())
        sql_total = sum(int(row[2] or 0) for row in r.by_owner)
        return {
            "period": p.as_dict(), "total": total, "sqls": sql_total, "lead_to_sql_rate": pct(sql_total, total),
            "average_days_to_sql": round(float(r.average_days_to_sql), 1) if r.average_days_to_sql is not None else None,
            "by_stage": [{"stage": k, "label": v[0], "count": r.by_stage.get(k, 0)} for k, v in LEAD_STAGES.items()],
            "by_source": sorted([{"source": s, "leads": int(n), "sqls": int(q or 0), "sql_rate": pct(int(q or 0), int(n))}
                                 for s, n, q in r.by_source], key=lambda x: -x["leads"]),
            "disqualified_reasons": sorted([{"reason": why, "count": int(n), "recycling": int(c or 0)}
                                            for why, n, c in r.disqualified], key=lambda x: -x["count"]),
            "by_owner": sorted([{"owner_id": o, "owner_name": names.get(o), "leads": int(n), "sqls": int(q or 0),
                                 "converted": int(c or 0), "disqualified": int(d or 0),
                                 "sql_rate": pct(int(q or 0), int(n))}
                                for o, n, q, c, d in r.by_owner], key=lambda x: -x["leads"]),
        }

    def pipeline(self, user: ReportViewer, client_type: Optional[str], member: Optional[str]) -> dict:
        """Open pipeline by owner and by expected close month; won / lost to date (BR-DR-02)."""
        owners = self.owners(user, member)
        stage_prob = self.access.stage_probabilities(user.tenant_id)
        rows = self.repo.pipeline_rows(user.tenant_id, owners, client_type)
        names = self.access.user_names([r[0] for r in rows])
        by_owner, by_month = _pipeline_groups(rows, names, stage_prob)
        # Ordering by a hidden amount would reveal it (BR-SF-12)
        key = "open_amount" if self.access.can_see_amounts(user) else "open_count"
        return self.access.masked(user, {
            "by_owner": sorted(by_owner.values(), key=lambda r: -r[key]),
            "by_close_month": sorted(by_month.values(), key=lambda r: r["month"] or "9999")})

    def forecast(self, user: ReportViewer, fy: Optional[int], member: Optional[str], client_type: Optional[str]) -> dict:
        """Revenue forecast by quarter for one financial year, and by financial year (BR-FC-01/02)."""
        owners = self.owners(user, member)
        tenant = user.tenant_id
        stage_prob = self.access.stage_probabilities(tenant)
        fy = fy or svc.fiscal_year_of(self._today())

        def bucket(start: date, end: date) -> dict:
            return _forecast_bucket(self.repo.forecast_rows(tenant, owners, client_type, start, end), start, end,
                                    stage_prob)

        def target_for(year: int, quarters: List[int]) -> Optional[float]:
            if client_type:
                return None  # targets are per rep, not per client type
            value = svc.as_float(self.repo.target_total(tenant, year, quarters, owners))
            return round(value, 2) if value else None

        fy_start, fy_end = svc.fiscal_year_bounds(fy)
        quarters = []
        for q in range(4):
            q_start = add_months(fy_start, 3 * q)
            b = bucket(q_start, add_months(q_start, 3))
            b["target"] = target_for(fy, [q + 1])
            b["attainment"] = pct(b["won"], b["target"]) if b["target"] else None
            quarters.append({"quarter": f"Q{q + 1}", "label": f"Q{q + 1} {svc.fiscal_label(fy)}", **b})
        total = bucket(fy_start, fy_end)
        total["target"] = target_for(fy, [1, 2, 3, 4])
        total["attainment"] = pct(total["won"], total["target"]) if total["target"] else None
        years = []
        for y in range(fy - 1, fy + 3):
            yb = bucket(*svc.fiscal_year_bounds(y))
            yb["target"] = target_for(y, [1, 2, 3, 4])
            years.append({"fy": y, "label": svc.fiscal_label(y), **yb})
        undated_n, undated_amount = self.repo.undated_open(tenant, owners, client_type)
        return self.access.masked(user, {
            "fy": fy, "label": svc.fiscal_label(fy), "start": fy_start, "end": fy_end - timedelta(days=1),
            "fiscal_year_start_month": svc.settings.FISCAL_YEAR_START_MONTH,
            "quarters": quarters, "total": total, "years": years,
            "open_without_close_date": {"count": undated_n, "amount": svc.as_float(undated_amount)}})

    def team_performance(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date],
                         member: Optional[str]) -> dict:
        """Scorecard per rep: activities, leads, SQLs, deals won and stage conversion (BR-TP-01)."""
        p = self.period(date_from, date_to)
        owners = self.owners(user, member)
        users = self.repo.scorecard_users(user.tenant_id, owners)
        ids = [u.user_id for u in users]
        if not ids:
            return {"period": p.as_dict(), "reps": [], "totals": {}}
        counts = self.repo.team_counts(user.tenant_id, ids, p.start, p.end)
        reps = [_rep_row(u, counts) for u in users]
        totals: Dict[str, Any] = {k: sum(r[k] for r in reps) for k in TOTAL_KEYS}
        totals["won_amount"] = round(sum(r["won_amount"] for r in reps), 2)
        totals["open_pipeline"] = round(sum(r["open_pipeline"] for r in reps), 2)
        _rate_rows(totals)
        return self.access.masked(user, {"period": p.as_dict(), "reps": reps, "totals": totals})

    def daily_limit(self, user: ReportViewer) -> dict:
        """Today's new-contact count against the per-user limit (BR-OV-01)."""
        return self.access.daily_limit(user)

    def targets(self, user: ReportViewer, fy: Optional[int], quarter: Optional[int], member: Optional[str]) -> dict:
        """Target against won, commit and best case per rep for a fiscal quarter, or the whole year (BR-SF-09)."""
        owners = self.owners(user, member)
        fy = fy or svc.fiscal_year_of(self._today())
        start, end = quarter_bounds(*svc.fiscal_year_bounds(fy), quarter)
        quarters = [quarter] if quarter else [1, 2, 3, 4]
        users = self.repo.target_users(user.tenant_id, owners)
        ids = [u.user_id for u in users]
        targets = {uid: svc.as_float(a) for uid, a in self.repo.targets_by_user(user.tenant_id, fy, quarters, ids).items()}
        agg = _target_agg(ids, self.repo.target_deal_rows(user.tenant_id, ids, start, end))
        rows = [_target_row(u, agg[u.user_id], targets.get(u.user_id) or 0.0) for u in users]
        if self.access.can_see_amounts(user):
            rows.sort(key=lambda r: (-(r["attainment"] or -1), -r["won"]))
        else:  # ranking by revenue would reveal it (BR-SF-12)
            rows.sort(key=lambda r: (-r["won_count"], r["name"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        tot_target = sum(r["target"] or 0 for r in rows)
        tot_won = sum(r["won"] for r in rows)
        totals = {"target": round(tot_target, 2) or None, "won": round(tot_won, 2),
                  "commit": round(sum(r["commit"] for r in rows), 2),
                  "best_case": round(sum(r["best_case"] for r in rows), 2),
                  "attainment": pct(tot_won, tot_target) if tot_target else None}
        return self.access.masked(user, {"fy": fy, "fy_label": svc.fiscal_label(fy), "quarter": quarter,
                                         "start": start, "end": end - timedelta(days=1), "rows": rows,
                                         "totals": totals})

    def leaderboard(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date],
                    member: Optional[str]) -> dict:
        """Reps ranked by revenue won in the period, with deals, SQLs and activity (BR-SF-09)."""
        perf = self.team_performance(user, date_from, date_to, member)
        # perf is already masked for viewers who can't see amounts, so won_amount is None there
        rows = sorted(perf["reps"], key=lambda r: (-(r["won_amount"] or 0), -r["won_count"], -r["sqls"],
                                                   -r["activities"]))
        out = [{"rank": i, "user_id": r["user_id"], "name": r["name"], "level_label": r["level_label"],
                "won_amount": r["won_amount"], "won_count": r["won_count"], "sqls": r["sqls"],
                "activities": r["activities"], "win_rate": r["win_rate"]} for i, r in enumerate(rows, 1)]
        return self.access.masked(user, {"period": perf["period"], "rows": out})

    def campaign_roi(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date],
                     member: Optional[str]) -> dict:
        """Per campaign: contacts emailed, replies, leads, SQLs, opportunities, pipeline and revenue won
        (BR-SF-11). A deal belongs to the campaign its lead came from."""
        p = self.period(date_from, date_to) if date_from else None
        start, end = (p.start, p.end) if p else (None, None)
        owners = self.owners(user, member)
        stage_prob = self.access.stage_probabilities(user.tenant_id)
        campaigns = self.repo.roi_campaigns(user.tenant_id, ROI_CAMPAIGNS)
        ids = [c.campaign_id for c in campaigns]
        if not ids:
            return {"rows": [], "totals": {}}
        msgs = self.repo.roi_messages(ids, owners, start, end)
        sent = {c: n for c, d, n in msgs if d == "OUTBOUND"}
        replied = {c: n for c, d, n in msgs if d == "INBOUND"}
        leads = {c: (int(n), int(q or 0)) for c, n, q in self.repo.roi_leads(ids, owners, start, end)}
        deals = _roi_deals(self.repo.roi_deals(ids, owners, start, end), stage_prob)
        rows = []
        for c in campaigns:
            row = _roi_row(c, deals.get(c.campaign_id, {}), sent.get(c.campaign_id, 0), replied.get(c.campaign_id, 0),
                           leads.get(c.campaign_id, (0, 0)))
            if row["contacts_emailed"] or row["leads"] or row["opportunities"]:
                rows.append(row)
        period = p.as_dict() if p else {"from": None, "to": None}
        return self.access.masked(user, {"period": period,
                                         **_roi_summary(rows, self.access.can_see_amounts(user))})

    # ── SQL-to-win and forecast accuracy (BRD section 8) ──

    def kpi_period(self, date_from: Optional[date], date_to: Optional[date], fy: Optional[int],
                   quarter: Optional[int]) -> SalesPeriod:
        return kpi_period(date_from, date_to, fy, quarter, self._today())

    def sql_to_win(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date], fy: Optional[int],
                   quarter: Optional[int], member: Optional[str]) -> dict:
        """Of the leads that reached SQL in the period, the share that became a won deal."""
        p = self.kpi_period(date_from, date_to, fy, quarter)
        owners = self.owners(user, member)
        return self.access.masked(user, self.sql_to_win_data(user, owners, p))

    def _deals_by_lead(self, tenant_id: str, leads: List[Any]) -> Dict[str, list]:
        lead_ids = [lead.lead_id for lead in leads]
        if not lead_ids:
            return {}
        opp_to_lead = {lead.opportunity_id: lead.lead_id for lead in leads if lead.opportunity_id}
        lead_set = set(lead_ids)
        out: Dict[str, list] = {}
        for o in self.repo.deals_of_leads(tenant_id, lead_ids, [lead.opportunity_id for lead in leads
                                                                if lead.opportunity_id]):
            lid = o.lead_id if o.lead_id in lead_set else opp_to_lead.get(o.opportunity_id)
            if lid:
                bucket = out.setdefault(lid, [])
                if all(d.opportunity_id != o.opportunity_id for d in bucket):
                    bucket.append(o)
        return out

    def sql_to_win_data(self, user: ReportViewer, owners: Owners, p: SalesPeriod) -> dict:
        """The SQL-to-win report before amounts are masked. A lead counts as won when any deal created from
        it is won; the lead owner gets the credit."""
        leads = self.repo.qualified_leads(user.tenant_id, owners, p.start, p.end)
        deals_by_lead = self._deals_by_lead(user.tenant_id, leads)
        totals = {"sqls": 0, "with_deal": 0, "won": 0, "lost": 0, "open": 0, "no_deal": 0}
        won_amount, days = 0.0, []
        per_owner: Dict[str, dict] = {}
        for lead in leads:
            deals = deals_by_lead.get(lead.lead_id, [])
            outcome = _sql_outcome(deals)
            o = per_owner.setdefault(lead.owner_id, {"owner_id": lead.owner_id, "sqls": 0, "won": 0, "lost": 0,
                                                     "open": 0, "no_deal": 0, "won_amount": 0.0})
            for agg in (totals, o):
                agg["sqls"] += 1
                agg[outcome] += 1
            totals["with_deal"] += 1 if deals else 0
            if outcome == "won":
                amount = sum(svc.as_float(d.amount) for d in deals if d.status == "WON")
                won_amount += amount
                o["won_amount"] += amount
                closed = min((d.closed_at for d in deals if d.status == "WON" and d.closed_at), default=None)
                if closed and lead.qualified_at:
                    days.append(max((closed - lead.qualified_at).total_seconds() / 86400, 0))
        names = self.access.user_names(per_owner)
        for o in per_owner.values():
            o["owner_name"] = names.get(o["owner_id"])
            o["won_amount"] = round(o["won_amount"], 2)
            o["sql_to_win_rate"] = pct(o["won"], o["sqls"])
        rows = sorted(per_owner.values(), key=lambda r: (-r["sqls"], -r["won"], r["owner_name"] or ""))
        return {
            "period": p.as_dict(), **totals,
            "sql_to_win_rate": pct(totals["won"], totals["sqls"]),
            "sql_to_deal_rate": pct(totals["with_deal"], totals["sqls"]),
            # Of the SQLs whose deal has closed, the share won (the rest are still being worked)
            "closed_win_rate": pct(totals["won"], totals["won"] + totals["lost"]),
            "won_amount": round(won_amount, 2),
            "average_days_sql_to_win": round(sum(days) / len(days), 1) if days else None,
            "by_owner": rows,
        }

    def forecast_accuracy(self, user: ReportViewer, period_type: str, periods: int, member: Optional[str]) -> dict:
        """The committed forecast stored at the start of each month / fiscal quarter against what was actually
        won by its end: accuracy = actual / committed x 100. Newest period first."""
        owners = self.owners(user, member)
        rows = self.access.forecast_history(user.tenant_id, period_type, owners, periods)
        closed = [r for r in rows if r["accuracy"] is not None]
        current_start, _ = fs.period_bounds(period_type, self._today())
        return self.access.masked(user, {
            "period_type": period_type, "periods": rows,
            "average_accuracy": round(sum(r["accuracy"] for r in closed) / len(closed), 1) if closed else None,
            "last_closed": closed[0] if closed else None,
            "current_period": {"period_start": current_start, "label": fs.period_label(period_type, current_start),
                               "has_snapshot": any(r["period_start"] == current_start for r in rows)},
            "can_take_snapshot": user.role in ("SUPER_ADMIN", "ADMIN"),
            "amounts_hidden": not self.access.can_see_amounts(user),
        }, ACCURACY_MONEY_KEYS)

    def take_forecast_snapshot(self, user: ReportViewer, period_type: Optional[str], replace: bool) -> dict:
        """Admins: store the current period's forecast now (normally the worker does it at period start)."""
        if user.role not in ("SUPER_ADMIN", "ADMIN"):
            raise PermissionDenied("Only admins can take forecast snapshots")
        if self.uow is None:
            raise RuntimeError("taking a snapshot needs a unit of work")
        types = [period_type] if period_type else list(fs.PERIOD_TYPES)
        with self.uow:
            results = [self.access.take_snapshot(user.tenant_id, t, user.user_id, replace) for t in types]
            self.uow.commit()
        return {"snapshots": results}
