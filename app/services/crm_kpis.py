"""
CRM KPIs (BRD v2.0 section 8).

  Duplicate contact rate    contacts in an unresolved duplicate group / live contacts x 100 (target < 1%)
  Reply-to-lead conversion  leads created from replies / contacts who replied positively x 100
  Lead-to-SQL conversion    leads created in the period that reached SQL / leads created x 100
  SQL-to-win rate           SQLs (qualified in the period) whose deal was won / SQLs x 100
  Forecast accuracy         actual revenue won in the quarter / committed forecast at its start x 100
  CRM adoption              users active in the week / licensed (active, non platform) users x 100

The pure maths (percentages, week windows, quarter selection, the KPI record) is at the top and
unit-tested on its own (tests/unit/test_crm_kpis.py); the database measures below it run single
grouped SQL queries (app/repositories/reports_kpis.py), so they stay cheap on 100k-contact
workspaces. GET /sales-reports/kpis (app/services/reports_kpis_service.py) puts them together with
the role scoping.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.core.clock import utcnow
from app.repositories.reports_kpis import KpiRepository
from app.services import sales as svc
from app.services.crm import _FREEMAIL_LABELS

logger = logging.getLogger(__name__)

# The source the one-click "lead from reply" flow records (app/routers/sales_router.py lead_from_message)
REPLY_LEAD_SOURCE = "Campaign reply"
# A lead counts as created from a reply when its contact replied positively this long before it
# (the same window as the reply-suggestions list that offers those replies)
REPLY_LEAD_WINDOW_DAYS = 30
ADOPTION_WEEKS = 9               # the current week and the 8 before it
DUPLICATE_RATE_TARGET = 1.0      # BRD: < 1%

DEFINITIONS = {
    "duplicate_contact_rate": {
        "label": "Duplicate contact rate",
        "formula": "duplicate contacts ÷ total contacts × 100",
        "definition": "Live contacts that belong to an unresolved duplicate group (same email apart from case, "
                      "dots or +tags in personal addresses; same or similar name at the same company; or the same "
                      "phone number), out of all live contacts in scope. Merging the group resolves it.",
    },
    "reply_to_lead": {
        "label": "Reply-to-lead conversion",
        "formula": "leads created ÷ positive replies × 100",
        "definition": "Leads created in the period from a reply (source \"Campaign reply\", as the one-click "
                      "Create lead from a reply records, or the contact replied positively in the 30 days "
                      "before), out of the contacts who replied positively in the period. Positive replies "
                      "are classified automatically by the AI reply classifier.",
    },
    "lead_to_sql": {
        "label": "Lead-to-SQL conversion",
        "formula": "SQLs ÷ leads × 100",
        "definition": "Leads created in the period that have reached SQL, out of all leads created in the period.",
    },
    "sql_to_win": {
        "label": "SQL-to-win rate",
        "formula": "deals won ÷ SQLs × 100",
        "definition": "Leads that reached SQL in the period whose deal was won, out of all of them.",
    },
    "forecast_accuracy": {
        "label": "Forecast accuracy",
        "formula": "actual revenue ÷ committed forecast × 100, per quarter",
        "definition": "Revenue won with a close date in the fiscal quarter, against the committed forecast stored "
                      "at its start: deals already won plus open deals in the Commit forecast category closing in "
                      "the quarter. 100% means the team won exactly what it committed; it can exceed 100%.",
    },
    "crm_adoption": {
        "label": "CRM adoption",
        "formula": "active users per week ÷ licensed users × 100",
        "definition": "Users who signed in or used the app during the week (Monday to Sunday, UTC), out of the "
                      "workspace's active users (licensed seats).",
    },
}
KPI_ORDER = ("duplicate_contact_rate", "reply_to_lead", "lead_to_sql", "sql_to_win", "forecast_accuracy",
             "crm_adoption")


# ── Pure maths ─────────────────────────────────────────────────

def pct(part, whole) -> Optional[float]:
    """part / whole x 100 to one decimal; None when there is no denominator."""
    if not whole:
        return None
    return round(100.0 * float(part) / float(whole), 1)


def forecast_accuracy_pct(actual: float, committed: Optional[float]) -> Optional[float]:
    """BRD forecast accuracy: actual revenue / committed forecast x 100 (can exceed 100).
    None when nothing was committed."""
    if committed is None or committed <= 0:
        return None
    return round(100.0 * float(actual) / float(committed), 1)


def variance_accuracy_pct(actual: float, forecast: Optional[float]) -> Optional[float]:
    """Closeness to a forecast: 1 - |actual - forecast| / forecast (the measure used before the BRD one)."""
    if not forecast:
        return None
    return round(100.0 * (1 - abs(float(actual) - float(forecast)) / float(forecast)), 1)


def meets_target(value: Optional[float], target: Optional[dict]) -> Optional[bool]:
    if value is None or not target:
        return None
    op, limit = target["operator"], target["value"]
    return {"<": value < limit, "<=": value <= limit, ">": value > limit, ">=": value >= limit}[op]


def week_start(d: date) -> date:
    """Monday of d's week."""
    return d - timedelta(days=d.weekday())


def week_windows(anchor: date, count: int = ADOPTION_WEEKS) -> List[Tuple[date, date]]:
    """[(monday, next monday)] for the week containing anchor and the count-1 weeks before, oldest first."""
    first = week_start(anchor) - timedelta(weeks=count - 1)
    return [(first + timedelta(weeks=i), first + timedelta(weeks=i + 1)) for i in range(count)]


def adoption_weeks(activity: Iterable[Tuple[str, date]], licensed: Set[str], windows: Sequence[Tuple[date, date]],
                   today: date) -> List[dict]:
    """Per week: distinct licensed users with an activity day in it / licensed users x 100."""
    active: Dict[date, Set[str]] = {start: set() for start, _ in windows}
    for user_id, day in activity:
        if user_id not in licensed:
            continue
        for start, end in windows:
            if start <= day < end:
                active[start].add(user_id)
                break
    out = []
    for start, end in windows:
        n = len(active[start])
        out.append({"week_start": start, "week_end": end - timedelta(days=1), "active_users": n,
                    "licensed_users": len(licensed), "value": pct(n, len(licensed)),
                    "partial": start <= today < end})
    return out


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def quarter_start_of(d: date) -> date:
    fy_start, _ = svc.fiscal_year_bounds(svc.fiscal_year_of(d))
    return add_months(fy_start, 3 * (svc.fiscal_quarter_of(d) - 1))


def forecast_quarters(fy: Optional[int], quarter: Optional[int], period_end: date) -> List[date]:
    """Starts of the fiscal quarters the forecast-accuracy KPI covers: the chosen quarter; every quarter
    of the chosen year; or, for a date range, the last quarter that ended on or before its end date."""
    if fy:
        fy_start, _ = svc.fiscal_year_bounds(fy)
        if quarter:
            return [add_months(fy_start, 3 * (quarter - 1))]
        return [add_months(fy_start, 3 * i) for i in range(4)]
    return [add_months(quarter_start_of(period_end + timedelta(days=1)), -3)]


def kpi(key: str, value: Optional[float], numerator=None, denominator=None, *, period: dict,
        scope: str, available: bool = True, note: Optional[str] = None, target: Optional[dict] = None,
        numerator_label: str = "", denominator_label: str = "", **extra) -> dict:
    d = DEFINITIONS[key]
    return {"key": key, "label": d["label"], "formula": d["formula"], "definition": d["definition"],
            "value": value, "unit": "%", "numerator": numerator, "denominator": denominator,
            "numerator_label": numerator_label, "denominator_label": denominator_label,
            "target": target, "meets_target": meets_target(value, target), "period": period, "scope": scope,
            "available": available, "note": note, **extra}


def reply_to_lead_note(positive: int, classifier_on: bool) -> Optional[str]:
    if positive:
        return None
    if not classifier_on:
        return ("No positive replies in the period. Replies are classified as positive by the AI reply "
                "classifier, which is not configured for this workspace.")
    return "No positive replies in the period."


# ── Database measures (SQL in app/repositories/reports_kpis.py) ─────────

def personal_labels() -> List[str]:
    """Free-mail provider labels: dots and +tags are ignored in those addresses."""
    return sorted(_FREEMAIL_LABELS)


# (tenant, owners) -> (fingerprint, result, time): the grouped query costs about 3 s per 100k contacts,
# so its result is reused while the contacts are unchanged (same count and latest update) for a while
_DUP_CACHE: Dict[tuple, tuple] = {}
DUP_CACHE_SECONDS = 600
DUP_QUERY_LIMIT_MS = 20_000      # MySQL stops the grouped query after this (about 600k contacts)


def duplicate_counts(repo: KpiRepository, tenant_id: str, owners: Optional[Set[str]]) -> Optional[Tuple[int, int]]:
    """(contacts in a duplicate group, live contacts) among the tenant's live contacts owned by owners
    (None = all, unowned included). Same rules as GET /contacts/duplicates (BR-CM-32), as one grouped
    query: each contact gets its match keys (normalised email; first + last name + company; first
    initial + last name + company; each phone's last 10 digits), a key shared by two or more contacts
    marks them all as duplicates. None when the query ran past DUP_QUERY_LIMIT_MS (it is then not cached)."""
    fingerprint = repo.contacts_fingerprint(tenant_id, owners)
    cache_key = (tenant_id, None if owners is None else frozenset(owners))
    hit = _DUP_CACHE.get(cache_key)
    now = utcnow()
    if hit and hit[0] == fingerprint and (now - hit[2]).total_seconds() < DUP_CACHE_SECONDS:
        return hit[1]
    result = repo.duplicate_counts(tenant_id, owners, personal_labels(), DUP_QUERY_LIMIT_MS)
    if result is None:
        return None
    if len(_DUP_CACHE) > 1000:
        _DUP_CACHE.clear()
    _DUP_CACHE[cache_key] = (fingerprint, result, now)
    return result


def reply_to_lead_counts(repo: KpiRepository, tenant_id: str, owners: Optional[Set[str]], start: datetime,
                         end: datetime) -> Tuple[int, int]:
    """(leads created from replies in [start, end), contacts with a positive reply in [start, end))."""
    return repo.reply_to_lead_counts(tenant_id, owners, start, end, REPLY_LEAD_SOURCE, REPLY_LEAD_WINDOW_DAYS)


def lead_to_sql_counts(repo: KpiRepository, tenant_id: str, owners: Optional[Set[str]], start: datetime,
                       end: datetime) -> Tuple[int, int]:
    """(leads created in [start, end) that reached SQL, leads created in [start, end)) - as the Leads report."""
    return repo.lead_to_sql_counts(tenant_id, owners, start, end)


def adoption(repo: KpiRepository, tenant_id: str, anchor: date, members: Optional[Set[str]] = None,
             today: Optional[date] = None) -> List[dict]:
    """CRM adoption for the week containing anchor and the 8 weeks before it, oldest first."""
    windows = week_windows(anchor)
    licensed = repo.licensed_users(tenant_id, members)
    rows = repo.activity_days(tenant_id, windows[0][0], windows[-1][1])
    return adoption_weeks(rows, licensed, windows, today or utcnow().date())
