"""
Reports query objects and pure report rules (ADR 0009, refactor chunk A6).

Shared by the campaign analytics reports (``/reports``), the sales reports and CRM KPIs
(``/sales-reports``) and the custom report builder (``/reports/custom``):

* ``Owners`` / ``report_owner_scope``: whose campaigns an analytics report covers;
* ``DateWindow`` / ``SalesPeriod``: inclusive date ranges and their defaults (server-local dates, as
  before: the caller passes ``today``, nothing here reads the clock);
* ``ComparisonSort``: the campaign comparison sort, limited to a whitelist;
* ``build_timeseries``: the daily / weekly series of the analytics dashboard;
* ``rate`` / ``pct``: the two percentage conventions;
* the custom report catalogue (objects, groupings, date fields, measures, operators) and the
  validation of a report definition and its filters against it. Column names never come from the
  request: the repository maps these whitelisted names to columns.

Invalid input raises ``ReportInputError`` (a ``ValueError``) carrying the HTTP status the API has
always answered with; services turn it into the matching ``app.core.errors`` domain error.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Protocol, Union

Owners = Union[None, str, Sequence[str]]
"""Campaign owners a report covers: None = everyone, a user id, or a team (possibly empty = nobody)."""


class ReportViewer(Protocol):
    """The signed-in user as the report layers see it (an ``app.models.user.User`` in production)."""

    user_id: str
    tenant_id: str
    role: str
    sales_level: Optional[int]


class ReportInputError(ValueError):
    """A report request the API refuses; ``status`` is the HTTP status it has always answered with."""

    status = 400

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


# ── Scope ─────────────────────────────────────────────────────

def report_owner_scope(role: str, user_id: str, visible: Optional[Iterable[str]], requested: Optional[str]) -> Owners:
    """Whose campaigns a report may cover: agents themselves, workspace-wide viewers anyone (or the one
    user asked for), managers their own team (a single member on request; another team's member is
    refused with 403)."""
    if role == "AGENT":
        return user_id
    if visible is None:
        return requested
    team = set(visible)
    if requested:
        if requested not in team:
            raise ReportInputError("That user is outside your team", 403)
        return requested
    return sorted(team)


def sees_owner(visible: Optional[Iterable[str]], owner_id: Optional[str]) -> bool:
    return visible is None or owner_id in set(visible)


# ── Periods ───────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DateWindow:
    """Inclusive dates; SQL compares ``>= start`` and ``< end_exclusive``."""

    start: date
    end: date

    @property
    def end_exclusive(self) -> date:
        return self.end + timedelta(days=1)

    @classmethod
    def or_default(cls, start: Optional[date], end: Optional[date], today: date, days: int = 30) -> DateWindow:
        """Missing end -> today; missing start -> ``days`` days ending on the end date."""
        end = end or today
        return cls(start or end - timedelta(days=days - 1), end)


@dataclass(frozen=True, slots=True)
class OptionalWindow:
    """A date filter where either bound may be missing (campaign comparison)."""

    start: Optional[date] = None
    end: Optional[date] = None

    @property
    def end_exclusive(self) -> Optional[date]:
        return self.end + timedelta(days=1) if self.end else None


@dataclass(frozen=True, slots=True)
class SalesPeriod:
    """``[start, end)`` datetimes for SQL and the inclusive ``first`` / ``last`` dates for the response."""

    start: datetime
    end: datetime
    first: date
    last: date

    @classmethod
    def of_dates(cls, first: date, last: date) -> SalesPeriod:
        return cls(datetime.combine(first, time.min), datetime.combine(last + timedelta(days=1), time.min),
                   first, last)

    @classmethod
    def or_default(cls, date_from: Optional[date], date_to: Optional[date], today: date, days: int = 90) -> SalesPeriod:
        """Inclusive dates -> [start, end) datetimes. Default: the last ``days`` days."""
        last = date_to or today
        first = date_from or last - timedelta(days=days - 1)
        if first > last:
            raise ReportInputError("date_from is after date_to")
        return cls.of_dates(first, last)

    @classmethod
    def of_bounds(cls, start: date, end_exclusive: date) -> SalesPeriod:
        """A fiscal year / quarter given as ``[start, end_exclusive)`` dates."""
        return cls(datetime.combine(start, time.min), datetime.combine(end_exclusive, time.min),
                   start, end_exclusive - timedelta(days=1))

    def as_dict(self) -> dict:
        return {"from": self.first, "to": self.last}


def add_months(d: date, months: int) -> date:
    """First day of the month ``months`` after d's month."""
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def quarter_bounds(fy_start: date, fy_end: date, quarter: Optional[int]) -> tuple[date, date]:
    """``[start, end)`` of fiscal quarter ``quarter`` (1-4) of the year starting fy_start; the whole
    year when quarter is None."""
    if not quarter:
        return fy_start, fy_end
    start = add_months(fy_start, 3 * (quarter - 1))
    return start, add_months(start, 3)


# ── Percentages ───────────────────────────────────────────────

def rate(part: Union[int, float], whole: Union[int, float]) -> float:
    """Analytics rate: part / whole x 100 to two decimals; 0.0 without a denominator."""
    return round((part / whole) * 100, 2) if whole > 0 else 0.0


def pct(part: Any, whole: Any) -> Optional[float]:
    """Sales report rate: part / whole x 100 to one decimal; None without a denominator."""
    return round(100.0 * part / whole, 1) if whole else None


# ── Campaign comparison sort ──────────────────────────────────

COMPARISON_RATE_FIELDS = frozenset({"open_rate", "reply_rate", "bounce_rate"})
COMPARISON_COLUMN_FIELDS = frozenset({"sent_count", "opened_count", "replied_count", "bounced_count",
                                      "campaign_name", "created_at"})


@dataclass(frozen=True, slots=True)
class ComparisonSort:
    """Sort of the campaign comparison: an unknown field sorts by created_at; anything but "asc" is
    descending (as the endpoint has always behaved)."""

    field: str = "open_rate"
    descending: bool = True

    @classmethod
    def parse(cls, sort_by: Optional[str], sort_dir: Optional[str]) -> ComparisonSort:
        name = sort_by if sort_by in COMPARISON_RATE_FIELDS | COMPARISON_COLUMN_FIELDS else "created_at"
        return cls(name, sort_dir != "asc")

    @property
    def is_rate(self) -> bool:
        return self.field in COMPARISON_RATE_FIELDS


# ── Time series ───────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SeriesPoint:
    date: str
    sent: int
    opened: int
    replied: int
    bounced: int


def build_timeseries(window: DateWindow, sent: Mapping[date, int], opened: Mapping[date, int],
                     replied: Mapping[date, int], bounced: Mapping[date, int], granularity: str) -> list[SeriesPoint]:
    """One point per day of the window (missing days count 0); ``weekly`` sums consecutive 7-day
    chunks from the start date when the window is longer than a week."""
    points = []
    current = window.start
    while current <= window.end:
        points.append(SeriesPoint(current.isoformat(), sent.get(current, 0), opened.get(current, 0),
                                  replied.get(current, 0), bounced.get(current, 0)))
        current += timedelta(days=1)
    if granularity != "weekly" or len(points) <= 7:
        return points
    weekly = []
    for i in range(0, len(points), 7):
        chunk = points[i:i + 7]
        weekly.append(SeriesPoint(chunk[0].date, sum(p.sent for p in chunk), sum(p.opened for p in chunk),
                                  sum(p.replied for p in chunk), sum(p.bounced for p in chunk)))
    return weekly


def median_hours(pairs: Iterable[tuple[Optional[datetime], Optional[datetime]]]) -> float:
    """Median (upper middle) of the positive (first reply - first sent) gaps in hours; 0.0 without any."""
    diffs = []
    for first_reply, first_sent in pairs:
        if first_reply and first_sent:
            hours = (first_reply - first_sent).total_seconds() / 3600
            if hours > 0:
                diffs.append(hours)
    diffs.sort()
    return diffs[len(diffs) // 2] if diffs else 0.0


# ── Custom report catalogue ───────────────────────────────────

OPERATORS = {"is": "is", "is_not": "is not", "contains": "contains", "is_empty": "is empty",
             "is_not_empty": "is known"}


@dataclass(frozen=True)
class ReportObject:
    """One reportable object: whitelisted grouping fields, date fields and measures with their labels."""

    label: str
    fields: dict[str, str]
    dates: dict[str, str]
    measures: dict[str, str]


CUSTOM_OBJECTS: dict[str, ReportObject] = {
    "leads": ReportObject(
        "Leads",
        {"stage": "Stage", "owner": "Owner", "source": "Source", "campaign": "Campaign",
         "created_month": "Created month", "disqualified_reason": "Disqualification reason"},
        {"created_at": "Created", "qualified_at": "Became SQL", "converted_at": "Converted"},
        {"count": "Number of leads"}),
    "deals": ReportObject(
        "Opportunities",
        {"stage": "Stage", "status": "Status", "owner": "Owner", "client_type": "Client type",
         "forecast_category": "Forecast category", "campaign": "Campaign", "company": "Company",
         "close_month": "Close month", "created_month": "Created month", "closed_reason": "Win / loss reason"},
        {"created_at": "Created", "close_date": "Close date", "closed_at": "Closed"},
        {"count": "Number of deals", "sum_amount": "Total amount", "avg_amount": "Average amount"}),
    "contacts": ReportObject(
        "Contacts",
        {"lifecycle_stage": "Lifecycle stage", "lead_status": "Lead status", "owner": "Owner",
         "country": "Country", "industry": "Industry", "lead_source": "Lead source",
         "consent_status": "Subscription", "created_month": "Created month"},
        {"created_at": "Created"},
        {"count": "Number of contacts"}),
    "activities": ReportObject(
        "Activities",
        {"activity_type": "Type", "owner": "Logged by", "source": "Source", "month": "Month"},
        {"occurred_at": "Date"},
        {"count": "Number of activities"}),
    "tasks": ReportObject(
        "Tasks",
        {"status": "Status", "owner": "Owner", "priority": "Priority", "task_type": "Type"},
        {"created_at": "Created", "due_at": "Due"},
        {"count": "Number of tasks"}),
}
MONEY_MEASURES = frozenset({"sum_amount", "avg_amount"})


def catalogue(can_build: bool) -> dict:
    """GET /reports/custom/meta."""
    return {"can_build": can_build, "operators": OPERATORS, "objects": {
        key: {"label": obj.label, "fields": dict(obj.fields), "dates": dict(obj.dates),
              "measures": dict(obj.measures)} for key, obj in CUSTOM_OBJECTS.items()}}


def can_build_reports(role: str, sales_level: Optional[int]) -> bool:
    return role in ("SUPER_ADMIN", "ADMIN", "MANAGER") or (sales_level or 9) <= 2


@dataclass(frozen=True)
class DefinitionSpec:
    """A custom report definition as received (``app.schemas.reports_custom_schema.Definition``)."""

    object: str
    group_by: str
    group_by_2: Optional[str] = None
    measure: str = "count"
    filters: Sequence[Mapping[str, Any]] = ()
    date_field: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None


def check_definition(d: DefinitionSpec) -> ReportObject:
    """Object, groupings and measure against the catalogue (in the order the API has always checked)."""
    obj = CUSTOM_OBJECTS.get(d.object)
    if not obj:
        raise ReportInputError(f"object must be one of {list(CUSTOM_OBJECTS)}")
    for g in filter(None, (d.group_by, d.group_by_2)):
        if g not in obj.fields:
            raise ReportInputError(f"Can group {obj.label.lower()} by {list(obj.fields)}")
    if d.measure not in obj.measures:
        raise ReportInputError(f"measure must be one of {list(obj.measures)}")
    return obj


def check_date_field(obj: ReportObject, date_field: str) -> None:
    if date_field not in obj.dates:
        raise ReportInputError(f"date_field must be one of {list(obj.dates)}")


def date_bounds(date_from: Optional[date], date_to: Optional[date]) -> tuple[Optional[datetime], Optional[datetime]]:
    """``>= from`` (midnight) and ``< day after to`` (midnight)."""
    lower = datetime.combine(date_from, time.min) if date_from else None
    upper = datetime.combine(date_to + timedelta(days=1), time.min) if date_to else None
    return lower, upper


@dataclass(frozen=True)
class FieldKind:
    """How a grouping field's values compare: numeric / date / enum / string (enum lists its values)."""

    kind: str = "string"
    enums: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilterSpec:
    """One validated filter. ``values`` holds one value, or several for "any of" (``is`` / ``is not``)."""

    field: str
    operator: str
    values: tuple[Any, ...] = ()
    many: bool = False


def _bad_value(label: str, message: str) -> ReportInputError:
    return ReportInputError(f"Filter on {label}: {message}", 422)


def coerce_value(label: str, kind: FieldKind, value: Any) -> Any:
    """One filter value checked against the field's kind; anything that cannot be compared is a 422."""
    if value is None or isinstance(value, (dict, list)):
        raise _bad_value(label, "the value must be a single text, number or date")
    if kind.kind == "numeric":
        try:
            if isinstance(value, bool):
                raise ValueError
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise _bad_value(label, f"{value!r} is not a number") from None
    if kind.kind == "date":
        try:
            return datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise _bad_value(label, f"{value!r} is not a date (YYYY-MM-DD)") from None
    text = str(value)
    if kind.kind == "enum" and text not in kind.enums:
        raise _bad_value(label, f"{value!r} must be one of {list(kind.enums)}")
    return text


def check_filter(fields: Mapping[str, str], kinds: Mapping[str, FieldKind], f: Mapping[str, Any],
                 user_id: str) -> FilterSpec:
    """A report filter validated against the whitelist. A list value means "any of" and is accepted
    only with is / is not; ``owner`` = "me" is the viewer."""
    name, op, value = f.get("field"), f.get("operator") or "is", f.get("value")
    if not isinstance(name, str) or not isinstance(op, str) or name not in fields or op not in OPERATORS:
        raise ReportInputError(f"Bad filter on {name}")
    if op in ("is_empty", "is_not_empty"):
        return FilterSpec(name, op)
    label, kind = fields[name], kinds.get(name, FieldKind())
    if name == "owner" and value == "me":
        value = user_id
    if isinstance(value, list):
        if op not in ("is", "is_not"):
            raise _bad_value(label, f"a list of values is only allowed with 'is' / 'is not', not '{op}'")
        return FilterSpec(name, op, tuple(coerce_value(label, kind, v) for v in value), many=True)
    return FilterSpec(name, op, (coerce_value(label, kind, value),))


# ── Custom report result shaping ──────────────────────────────

def _num(v: Any) -> float:
    return round(float(v), 2) if v is not None else 0


def _blank(v: Any) -> bool:
    return v in (None, "")


def shape_flat(rows: Sequence[tuple[Any, Any]], labels: Mapping[Any, str], group_by: str) -> list[dict]:
    """One grouping: biggest value first, or chronological for month groupings."""
    out = [{"key": r[0], "label": "(none)" if _blank(r[0]) else labels.get(r[0], r[0]), "value": _num(r[1])}
           for r in rows]
    if group_by.endswith("month"):
        out.sort(key=lambda r: r["key"] or "9999")
    else:
        out.sort(key=lambda r: -r["value"])
    return out


def shape_matrix(rows: Sequence[tuple[Any, Any, Any]], labels: Mapping[Any, str], col_labels: Mapping[Any, str],
                 averaged: bool) -> tuple[list[dict], list[dict]]:
    """Two groupings: rows of the first, a value per column of the second (columns in first-seen order);
    a row total unless the measure is an average."""
    columns: list[Any] = []
    matrix: dict[Any, dict] = {}
    for a, b, v in rows:
        ck = "(none)" if _blank(b) else b
        if ck not in columns:
            columns.append(ck)
        rk = "(none)" if _blank(a) else a
        matrix.setdefault(rk, {"key": rk, "label": "(none)" if _blank(a) else labels.get(a, a), "values": {}})
        matrix[rk]["values"][ck] = _num(v)
    out = list(matrix.values())
    for r in out:
        r["value"] = None if averaged else round(sum(r["values"].values()), 2)
    out.sort(key=lambda r: -(r["value"] or 0))
    return out, [{"key": c, "label": col_labels.get(c, c)} for c in columns]


def report_total(rows: Sequence[Mapping[str, Any]], averaged: bool) -> Optional[float]:
    return None if averaged else round(sum(r["value"] or 0 for r in rows), 2)
