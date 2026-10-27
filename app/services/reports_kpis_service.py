"""
The six CRM KPIs of BRD v2.0 section 8 in one call (GET /sales-reports/kpis; chunk A6 moved the assembly
out of app/routers/sales_kpi_router.py).

Same scoping as the other sales reports: every figure covers the viewer's records and their team's
(BR-SH-02), ``member`` narrows to one person and everyone below them, and amounts are blanked for roles
that may not see them (BR-SF-12). Duplicate contact rate and CRM adoption measure the workspace itself
(data quality, seat usage), so only workspace-wide viewers (SUPER_ADMIN / ADMIN and sales level 1, the
Sales Head) get them; for everyone else they come back with available = false.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Optional, Set

from app.core.config import settings
from app.domain.reports import ReportViewer, SalesPeriod, add_months
from app.repositories.reports_kpis import KpiRepository, Session, SqlAlchemyKpiRepository
from app.services import crm_kpis as kp
from app.services import sales as svc
from app.services.reports_sales_service import ACCURACY_MONEY_KEYS, SalesReports

RESTRICTED = "Workspace-level KPI: shown to admins and the Sales Head."
DUPLICATE_TARGET = {"operator": "<", "value": kp.DUPLICATE_RATE_TARGET, "label": "< 1%"}
QUARTER_KEYS = ("label", "period_start", "period_end", "closed", "has_snapshot", "committed", "actual",
                "accuracy_to_date")


def forecast_note(figures: list[dict], usable: list[dict], committed: float, label: str) -> Optional[str]:
    single = len(figures) == 1
    if not any(f["has_snapshot"] for f in figures):
        return (f"No forecast was stored at the start of {label}. It is stored automatically in the first days "
                "of every quarter (admins can store it by hand on the Forecast tab).")
    if all(f["legacy_snapshot"] for f in figures if f["has_snapshot"]):
        return f"The forecast for {label} was stored before committed forecasts were recorded."
    if not usable:
        return f"No quarter of {label} has finished yet."
    if not committed:
        return f"Nothing was committed for {label}."
    if single and not figures[0]["closed"]:
        return f"{label} is in progress: revenue won so far against the committed forecast."
    return None


class CrmKpis:
    def __init__(self, reports: SalesReports, repository: KpiRepository,
                 today: Callable[[], date] = date.today) -> None:
        self.reports = reports
        self.repo = repository
        self._today = today

    @classmethod
    def for_session(cls, db: Session) -> "CrmKpis":
        return cls(SalesReports.for_session(db), SqlAlchemyKpiRepository(db))

    @property
    def access(self) -> Any:
        return self.reports.access

    def kpis(self, user: ReportViewer, date_from: Optional[date], date_to: Optional[date], fy: Optional[int],
             quarter: Optional[int], member: Optional[str]) -> dict:
        """The six BRD section 8 KPIs: value (%), numerator, denominator, BRD target, period and a note.
        Period: fy (+ quarter) or date_from / date_to (default: the last 90 days)."""
        p = self.reports.kpi_period(date_from, date_to, fy, quarter)
        owners = self.reports.owners(user, member)
        workspace_view = self.access.sees_everything(user)
        show_amounts = self.access.can_see_amounts(user)
        out = [
            self._duplicates(user, owners, member, workspace_view),
            self._reply_to_lead(user, owners, p),
            self._lead_to_sql(user, owners, p),
            self._sql_to_win(user, owners, p),
            self._forecast(user, owners, fy, quarter, p.last, show_amounts),
            self._adoption(user, owners, member, p, workspace_view),
        ]
        return {"period": {**p.as_dict(), "fy": fy, "quarter": quarter}, "member": member,
                "workspace_view": workspace_view, "amounts_hidden": not show_amounts, "kpis": out}

    # 1. Duplicate contact rate (workspace data quality; member= narrows to that team's contacts)
    def _duplicates(self, user: ReportViewer, owners: Optional[Set[str]], member: Optional[str],
                    workspace_view: bool) -> dict:
        as_of = {"as_of": self._today()}
        if not workspace_view:
            return kp.kpi("duplicate_contact_rate", None, period=as_of, scope="workspace", available=False,
                          note=RESTRICTED, target=dict(DUPLICATE_TARGET))
        counts = kp.duplicate_counts(self.repo, user.tenant_id, owners)
        dups, total = counts if counts else (None, None)
        if counts is None:
            note: Optional[str] = "Too many contacts to check for duplicates right now; try again later."
        else:
            note = None if total else "No contacts yet."
        return kp.kpi("duplicate_contact_rate", kp.pct(dups, total) if counts else None, dups, total,
                      period=as_of, scope="team" if member else "workspace", numerator_label="Duplicate contacts",
                      denominator_label="Live contacts", target=dict(DUPLICATE_TARGET), note=note)

    # 2. Reply-to-lead conversion
    def _reply_to_lead(self, user: ReportViewer, owners: Optional[Set[str]], p: SalesPeriod) -> dict:
        leads, positive = kp.reply_to_lead_counts(self.repo, user.tenant_id, owners, p.start, p.end)
        classifier = bool(settings.OPENAI_API_KEY)
        return kp.kpi("reply_to_lead", kp.pct(leads, positive), leads, positive, period=p.as_dict(), scope="team",
                      numerator_label="Leads from replies", denominator_label="Contacts who replied positively",
                      note=kp.reply_to_lead_note(positive, classifier), classifier_available=classifier)

    # 3. Lead-to-SQL conversion
    def _lead_to_sql(self, user: ReportViewer, owners: Optional[Set[str]], p: SalesPeriod) -> dict:
        sqls, leads = kp.lead_to_sql_counts(self.repo, user.tenant_id, owners, p.start, p.end)
        return kp.kpi("lead_to_sql", kp.pct(sqls, leads), sqls, leads, period=p.as_dict(), scope="team",
                      numerator_label="Reached SQL", denominator_label="Leads created",
                      note=None if leads else "No leads created in the period.")

    # 4. SQL-to-win rate (the same count as GET /sales-reports/sql-to-win)
    def _sql_to_win(self, user: ReportViewer, owners: Optional[Set[str]], p: SalesPeriod) -> dict:
        s2w = self.reports.sql_to_win_data(user, owners, p)
        return kp.kpi("sql_to_win", kp.pct(s2w["won"], s2w["sqls"]), s2w["won"], s2w["sqls"], period=p.as_dict(),
                      scope="team", numerator_label="SQLs won", denominator_label="SQLs",
                      note=None if s2w["sqls"] else "No leads reached SQL in the period.")

    # 5. Forecast accuracy (per fiscal quarter)
    def _forecast(self, user: ReportViewer, owners: Optional[Set[str]], fy: Optional[int], quarter: Optional[int],
                  last: date, show_amounts: bool) -> dict:
        starts = kp.forecast_quarters(fy, quarter, last)
        figures = [self.access.forecast_figures(user.tenant_id, s, owners) for s in starts]
        single = len(figures) == 1
        # A whole year counts its finished quarters; a single quarter may still be in progress
        usable = [f for f in figures if f["committed"] is not None and (f["closed"] or single)]
        actual = round(sum(f["actual"] for f in usable), 2)
        committed = round(sum(f["committed"] for f in usable), 2)
        value = kp.forecast_accuracy_pct(actual, committed) if usable else None
        label = figures[0]["label"] if single else f"{svc.fiscal_label(fy) if fy else fy} (finished quarters)"
        quarters = [{k: f[k] for k in QUARTER_KEYS} for f in figures]
        if not show_amounts:
            quarters = self.access.mask_money(quarters, ACCURACY_MONEY_KEYS)
        return kp.kpi("forecast_accuracy", value, actual if usable and show_amounts else None,
                      committed if usable and show_amounts else None,
                      period={"from": starts[0], "to": add_months(starts[-1], 3) - timedelta(days=1), "label": label},
                      scope="team", note=forecast_note(figures, usable, committed, label),
                      numerator_label="Revenue won", denominator_label="Committed forecast",
                      in_progress=bool(single and not figures[0]["closed"]), quarters=quarters,
                      amounts_hidden=not show_amounts)

    # 6. CRM adoption (week containing the period end, and the 8 weeks before)
    def _adoption(self, user: ReportViewer, owners: Optional[Set[str]], member: Optional[str], p: SalesPeriod,
                  workspace_view: bool) -> dict:
        if not workspace_view:
            return kp.kpi("crm_adoption", None, period=p.as_dict(), scope="workspace", available=False,
                          note=RESTRICTED)
        # member= narrows seats and activity to that person's team (owners is exactly that team here)
        today = self._today()
        weeks = kp.adoption(self.repo, user.tenant_id, min(p.last, today), owners if member else None)
        current = weeks[-1]
        return kp.kpi("crm_adoption", current["value"], current["active_users"], current["licensed_users"],
                      period={"from": current["week_start"], "to": current["week_end"],
                              "label": "This week" if current["partial"] else f"Week of {current['week_start']}"},
                      scope="team" if member else "workspace", numerator_label="Active users this week",
                      denominator_label="Licensed users", trend=weeks,
                      last_full_week=weeks[-2] if current["partial"] else current,
                      note="Week to date." if current["partial"] else None)
