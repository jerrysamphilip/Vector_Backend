"""
CRM KPIs (BRD v2.0 section 8).

  Duplicate contact rate    contacts in an unresolved duplicate group / live contacts x 100 (target < 1%)
  Reply-to-lead conversion  leads created from replies / contacts who replied positively x 100
  Lead-to-SQL conversion    leads created in the period that reached SQL / leads created x 100
  SQL-to-win rate           SQLs (qualified in the period) whose deal was won / SQLs x 100
  Forecast accuracy         actual revenue won in the quarter / committed forecast at its start x 100
  CRM adoption              users active in the week / licensed (active, non platform) users x 100

The pure maths (percentages, week windows, quarter selection, the KPI record) is at the top and
unit-tested on its own (tests/unit/test_crm_kpis.py); the database measures below it are single
grouped SQL queries, so they stay cheap on 100k-contact workspaces. GET /sales-reports/kpis
(app/routers/sales_kpi_router.py) puts them together with the role scoping.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.services import sales as svc

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


# ── Database measures ──────────────────────────────────────────

def _owner_filter(owners: Optional[Set[str]], column: str) -> Tuple[str, dict]:
    if owners is None:
        return "", {}
    return f" AND {column} IN :owners", {"owners": sorted(owners) or ["-"]}


def _personal_labels() -> List[str]:
    from app.services.crm import _FREEMAIL_LABELS
    return sorted(_FREEMAIL_LABELS)


# (tenant, owners) -> (fingerprint, result, time): the grouped query costs about 3 s per 100k contacts,
# so its result is reused while the contacts are unchanged (same count and latest update) for a while
_DUP_CACHE: Dict[tuple, tuple] = {}
DUP_CACHE_SECONDS = 600
DUP_QUERY_LIMIT_MS = 20_000      # MySQL stops the grouped query after this (about 600k contacts)


def duplicate_counts(db: Session, tenant_id: str, owners: Optional[Set[str]]) -> Optional[Tuple[int, int]]:
    """(contacts in a duplicate group, live contacts) among the tenant's live contacts owned by owners
    (None = all, unowned included). Same rules as GET /contacts/duplicates (BR-CM-32), as one grouped
    query: each contact gets its match keys (normalised email; first + last name + company; first
    initial + last name + company; each phone's last 10 digits), a key shared by two or more contacts
    marks them all as duplicates. None when the query ran past DUP_QUERY_LIMIT_MS (it is then not cached)."""
    owner_sql, params = _owner_filter(owners, "owner_id")
    fp_query = text(f"""SELECT COUNT(*), MAX(updated_at) FROM prospects
                        WHERE tenant_id = :tenant AND deleted_at IS NULL{owner_sql}""")
    if owners is not None:
        fp_query = fp_query.bindparams(bindparam("owners", expanding=True))
    fingerprint = tuple(db.execute(fp_query, {"tenant": tenant_id, **params}).one())
    cache_key = (tenant_id, None if owners is None else frozenset(owners))
    hit = _DUP_CACHE.get(cache_key)
    now = datetime.utcnow()
    if hit and hit[0] == fingerprint and (now - hit[2]).total_seconds() < DUP_CACHE_SECONDS:
        return hit[1]
    result = _duplicate_counts_sql(db, tenant_id, owners, owner_sql, params)
    if result is None:
        return None
    if len(_DUP_CACHE) > 1000:
        _DUP_CACHE.clear()
    _DUP_CACHE[cache_key] = (fingerprint, result, now)
    return result


def _duplicate_counts_sql(db: Session, tenant_id: str, owners, owner_sql: str,
                          params: dict) -> Optional[Tuple[int, int]]:
    norm = "REGEXP_REPLACE(LOWER(COALESCE({0}, '')), '[^a-z0-9]', '')"
    email = "LOWER(email)"
    local = f"SUBSTRING_INDEX(SUBSTRING_INDEX({email}, '@', 1), '+', 1)"
    domain = f"IF(LOCATE('@', {email}) = 0, '', SUBSTRING({email}, LOCATE('@', {email}) + 1))"
    personal = f"({domain} = '' OR SUBSTRING_INDEX({domain}, '.', 1) IN :labels)"
    # LIMIT keeps MySQL from merging s into every branch below (which would rerun the regexes per
    # branch): s is materialised once, then each match key is grouped once.
    query = text(f"""
        WITH s AS (
            SELECT prospect_id,
                   CONCAT(IF({personal}, REPLACE({local}, '.', ''), {local}), '@', {domain}) AS ekey,
                   {norm.format('first_name')} AS f, {norm.format('last_name')} AS l,
                   COALESCE(NULLIF(account_id, ''), {norm.format('company_name')}) AS c,
                   REGEXP_REPLACE(COALESCE(phone, ''), '[^0-9]', '') AS p1,
                   REGEXP_REPLACE(COALESCE(mobile_phone, ''), '[^0-9]', '') AS p2
            FROM prospects
            WHERE tenant_id = :tenant AND deleted_at IS NULL{owner_sql}
            LIMIT 18446744073709551615
        ),
        k AS (
            SELECT prospect_id, CONCAT('e|', ekey) AS k FROM s
            UNION ALL SELECT prospect_id, CONCAT('n|', f, '|', l, '|', c) FROM s WHERE f <> '' AND l <> '' AND c <> ''
            UNION ALL SELECT prospect_id, CONCAT('i|', LEFT(f, 1), '|', l, '|', c) FROM s
                      WHERE CHAR_LENGTH(f) > 1 AND l <> '' AND c <> ''
            UNION ALL SELECT prospect_id, CONCAT('p|', RIGHT(p1, 10)) FROM s WHERE CHAR_LENGTH(p1) >= 7
            UNION ALL SELECT prospect_id, CONCAT('p|', RIGHT(p2, 10)) FROM s WHERE CHAR_LENGTH(p2) >= 7
        ),
        d AS (SELECT k FROM k GROUP BY k HAVING COUNT(DISTINCT prospect_id) > 1)
        SELECT /*+ MAX_EXECUTION_TIME({int(DUP_QUERY_LIMIT_MS)}) */
               (SELECT COUNT(DISTINCT k.prospect_id) FROM k JOIN d ON d.k = k.k), (SELECT COUNT(*) FROM s)
    """).bindparams(bindparam("labels", expanding=True))
    if owners is not None:
        query = query.bindparams(bindparam("owners", expanding=True))
    try:
        # Own connection: a query stopped by the time limit must not spoil the request's session
        with db.get_bind().connect() as conn:
            row = conn.execute(query, {"tenant": tenant_id, "labels": _personal_labels(), **params}).one()
    except DBAPIError as exc:
        logger.warning("[KPI] Duplicate contact count for %s stopped: %s", tenant_id, exc.orig)
        return None
    return int(row[0] or 0), int(row[1] or 0)


def reply_to_lead_counts(db: Session, tenant_id: str, owners: Optional[Set[str]], start: datetime,
                         end: datetime) -> Tuple[int, int]:
    """(leads created from replies in [start, end), contacts with a positive reply in [start, end)).
    Leads are scoped by lead owner, positive replies by contact owner."""
    lead_owner_sql, lp = _owner_filter(owners, "l.owner_id")
    contact_owner_sql, cp = _owner_filter(owners, "p.owner_id")
    leads = text(f"""
        SELECT COUNT(*) FROM leads l
        WHERE l.tenant_id = :tenant AND l.created_at >= :start AND l.created_at < :end{lead_owner_sql}
          AND (l.source = :source OR EXISTS (
                SELECT 1 FROM email_messages m JOIN email_events e ON e.message_id = m.message_id
                WHERE m.prospect_id = l.prospect_id AND e.event_type = :positive
                  AND e.event_time <= l.created_at
                  AND e.event_time >= l.created_at - INTERVAL {int(REPLY_LEAD_WINDOW_DAYS)} DAY))
    """)
    positives = text(f"""
        SELECT COUNT(DISTINCT m.prospect_id) FROM email_events e
        JOIN email_messages m ON m.message_id = e.message_id
        JOIN prospects p ON p.prospect_id = m.prospect_id
        WHERE p.tenant_id = :tenant AND e.event_type = :positive
          AND e.event_time >= :start AND e.event_time < :end{contact_owner_sql}
    """)
    if owners is not None:
        leads = leads.bindparams(bindparam("owners", expanding=True))
        positives = positives.bindparams(bindparam("owners", expanding=True))
    from app.models.email_message import EmailEvent
    common = {"tenant": tenant_id, "start": start, "end": end, "positive": EmailEvent.EVENT_POSITIVE_REPLY}
    n_leads = db.execute(leads, {**common, "source": REPLY_LEAD_SOURCE, **lp}).scalar() or 0
    n_pos = db.execute(positives, {**common, **cp}).scalar() or 0
    return int(n_leads), int(n_pos)


def lead_to_sql_counts(db: Session, tenant_id: str, owners: Optional[Set[str]], start: datetime,
                       end: datetime) -> Tuple[int, int]:
    """(leads created in [start, end) that reached SQL, leads created in [start, end)) - as the Leads report."""
    owner_sql, params = _owner_filter(owners, "owner_id")
    q = text(f"""SELECT COALESCE(SUM(qualified_at IS NOT NULL), 0), COUNT(*) FROM leads
                 WHERE tenant_id = :tenant AND created_at >= :start AND created_at < :end{owner_sql}""")
    if owners is not None:
        q = q.bindparams(bindparam("owners", expanding=True))
    row = db.execute(q, {"tenant": tenant_id, "start": start, "end": end, **params}).one()
    return int(row[0] or 0), int(row[1] or 0)


def licensed_users(db: Session, tenant_id: str, members: Optional[Set[str]] = None) -> Set[str]:
    """Licensed seats: the workspace's ACTIVE users (platform admins have no workspace)."""
    from app.models.user import User
    q = db.query(User.user_id).filter(User.tenant_id == tenant_id, User.status == "ACTIVE",
                                      User.role != "PLATFORM_ADMIN")
    if members is not None:
        q = q.filter(User.user_id.in_(members or {"-"}))
    return {u for (u,) in q}


def adoption(db: Session, tenant_id: str, anchor: date, members: Optional[Set[str]] = None,
             today: Optional[date] = None) -> List[dict]:
    """CRM adoption for the week containing anchor and the 8 weeks before it, oldest first."""
    from app.models.user_activity import UserActivityDay
    windows = week_windows(anchor)
    licensed = licensed_users(db, tenant_id, members)
    rows = db.query(UserActivityDay.user_id, UserActivityDay.day).filter(
        UserActivityDay.tenant_id == tenant_id, UserActivityDay.day >= windows[0][0],
        UserActivityDay.day < windows[-1][1]).all()
    return adoption_weeks(rows, licensed, windows, today or datetime.utcnow().date())
