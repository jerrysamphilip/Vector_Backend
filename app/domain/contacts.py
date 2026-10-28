"""
Contacts aggregate: typed query objects and pure rules (ADR 0009, chunk L1).

Routers build a ``ContactFilter`` (the request's filter parameters, unvalidated) plus ``Sort`` /
``Paging`` (app/domain/paging.py). The service validates and resolves it into ``ContactCriteria``
(list looked up, grades parsed, owner "me" resolved, the JSON AND/OR filter parsed into a
``FilterGroup`` / ``FilterCondition`` tree, the caller's ``VisibilityScope`` attached), which the
repository compiles to SQL. Every value here is frozen and free of I/O.

Errors: a malformed AND/OR filter raises ``FilterError`` (a ``ValueError``); the service turns it into
a 400 where the API always did.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional, Union

from app.domain.paging import Sort, SortDirection

# ── Sorting ─────────────────────────────────────────────────────────

CONTACT_SORT_FIELDS = frozenset(
    {"name", "email", "company", "lifecycle_stage", "lead_status", "created_at", "updated_at", "score"})


def contact_sort(sort_by: str, sort_order: str) -> Sort:
    """The list view's sort (``sort_by`` one of CONTACT_SORT_FIELDS, ``sort_order`` asc / desc)."""
    if sort_by not in CONTACT_SORT_FIELDS:
        raise ValueError(f"Cannot sort by '{sort_by}'")
    return Sort(sort_by, SortDirection(sort_order))


# ── Visibility (BR-SH-02) ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class VisibilityScope:
    """Whose records a user may see: ``owner_ids`` None means everyone's; otherwise those owners,
    plus unassigned records when ``include_unassigned`` (users who manage contacts pick them up)."""

    owner_ids: Optional[frozenset[str]] = None
    include_unassigned: bool = False

    @property
    def everyone(self) -> bool:
        return self.owner_ids is None

    def allows(self, owner_id: Optional[str]) -> bool:
        if self.owner_ids is None:
            return True
        return owner_id in self.owner_ids or (owner_id is None and self.include_unassigned)


def team_ids(manager_pairs: Iterable[tuple[str, Optional[str]]], root: str) -> set[str]:
    """``root`` and everyone below them, from (user_id, manager_id) pairs (direct and indirect reports)."""
    children: dict[str, list[str]] = {}
    for uid, manager_id in manager_pairs:
        if manager_id:
            children.setdefault(manager_id, []).append(uid)
    seen, stack = {root}, [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


# ── Small pure helpers ──────────────────────────────────────────────

_PHONE_QUERY = re.compile(r"[\d\s+().\-/]+")
GRADES = ("A", "B", "C", "D")


def phone_digits(phone: Optional[str]) -> str:
    return re.sub(r"\D", "", phone or "")


def parse_grades(grade: str) -> list[str]:
    """'a, B,x' -> ['A', 'B']; unknown grades are dropped."""
    return [g for g in (x.strip().upper() for x in grade.split(",")) if g in GRADES]


def search_terms(q: str) -> tuple[str, ...]:
    """Terms a contact search matches against the lower-cased search text: the query itself, plus its
    digits when the query looks like a phone number (digits and phone punctuation only), so a mixed
    query such as "zorblax66034" does not match every record that merely contains "66034"."""
    term = q.strip().lower()
    digits = phone_digits(q)
    if len(digits) >= 4 and digits != term and _PHONE_QUERY.fullmatch(q.strip()):
        return term, digits
    return (term,)


# ── AND / OR filter tree (BR-CM-18/22) ──────────────────────────────
#
# A filter is either a condition {"field", "operator", "value"} or a group
# {"op": "AND"|"OR", "conditions": [...]} — groups nest. Fields are contact properties,
# "custom.<key>" for custom properties, plus "tags", "list", "company.<prop>".

CONTACT_FILTER_FIELDS = (
    "first_name", "last_name", "email", "phone", "mobile_phone", "designation", "company_name", "industry",
    "emp_band", "linkedin_url", "poc_city", "poc_state", "poc_country", "timezone", "lifecycle_stage",
    "lead_status", "lead_source", "legal_basis", "owner_id", "account_id", "consent_status", "is_valid_email",
    "email_type", "created_at", "updated_at",
)
COMPANY_FILTER_FIELDS = (
    "name", "domain", "industry", "emp_band", "country", "city", "lifecycle_stage", "annual_revenue", "owner_id",
)
NON_TEXT_CONTACT_FIELDS = frozenset({"is_valid_email", "created_at", "updated_at"})
OPERATORS = frozenset({"is", "is_not", "in", "not_in", "contains", "not_contains", "starts_with", "is_empty",
                       "is_not_empty", "gt", "gte", "lt", "lte", "between", "before", "after", "in_last_days"})
TAG_OPERATORS = frozenset({"is", "in", "contains", "is_not", "not_in", "not_contains", "is_empty", "is_not_empty"})

MAX_FILTER_DEPTH = 4
MAX_CONDITIONS = 50

_CUSTOM_KEY = re.compile(r"[a-z0-9_]{1,64}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

ConditionKind = Literal["tags", "list", "custom", "company", "contact"]


class FilterError(ValueError):
    """A filter the engine cannot run (unknown field or operator, bad value, too deep / too many)."""


@dataclass(frozen=True)
class FilterCondition:
    """One checked condition. ``name`` is the property without its prefix (custom key, company property,
    contact column); ``value`` is normalised ("me" resolved, dates parsed where compared as dates,
    ``in_last_days`` as an int). ``is_text`` / ``numeric`` say how the repository compares it."""

    kind: ConditionKind
    name: str
    operator: str
    value: Any = None
    is_text: bool = True
    numeric: bool = False


@dataclass(frozen=True)
class FilterGroup:
    op: Literal["AND", "OR"]
    children: tuple[FilterNode, ...]


FilterNode = Union[FilterCondition, FilterGroup]


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


def parse_datetime(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise FilterError(f"Invalid date: {value}") from None


def _compared_value(operator: str, value: Any, is_text: bool) -> Any:
    """The value as the comparison uses it (dates parsed for non-text before/after, days as int)."""
    if operator in ("after", "before") and is_text is False:
        return parse_datetime(value)
    if operator == "in_last_days":
        return int(value)
    return value


def _tags_condition(operator: str, value: Any) -> FilterCondition:
    if operator not in TAG_OPERATORS:
        raise FilterError("Tags support is / is_not / is_empty / is_not_empty")
    return FilterCondition("tags", "tags", operator, value)


def _custom_condition(key: str, operator: str, value: Any) -> FilterCondition:
    if not _CUSTOM_KEY.fullmatch(key):
        raise FilterError(f"Invalid custom field: {key}")
    if operator in ("contains", "is") and isinstance(value, str):  # multi-checkbox values are arrays
        return FilterCondition("custom", key, operator, value)
    is_date = isinstance(value, str) and bool(_ISO_DATE.fullmatch(value))
    numeric = operator in ("gt", "gte", "lt", "lte", "between") and not is_date
    return FilterCondition("custom", key, operator, _compared_value(operator, value, True), numeric=numeric)


def _company_condition(prop: str, operator: str, value: Any, revenue_allowed: Callable[[], bool]) -> FilterCondition:
    if prop == "annual_revenue" and not revenue_allowed():
        raise FilterError("Your role cannot filter on revenue")
    if prop not in COMPANY_FILTER_FIELDS:
        raise FilterError(f"Unknown company property: company.{prop}")
    is_text = prop != "annual_revenue"
    return FilterCondition("company", prop, operator, _compared_value(operator, value, is_text), is_text=is_text)


def _contact_condition(fld: Any, operator: str, value: Any) -> FilterCondition:
    name = fld or ""
    if name not in CONTACT_FILTER_FIELDS:
        raise FilterError(f"Unknown property: {fld}")
    is_text = name not in NON_TEXT_CONTACT_FIELDS
    if name == "is_valid_email" and isinstance(value, str):
        value = value.lower() in ("true", "1", "yes")
    return FilterCondition("contact", name, operator, _compared_value(operator, value, is_text), is_text=is_text)


def parse_condition(cond: Mapping[str, Any], me: str, revenue_allowed: Callable[[], bool]) -> FilterCondition:
    """One condition, checked in the order the engine always used (operator, then the field)."""
    fld, operator, value = cond.get("field"), cond.get("operator"), cond.get("value")
    if operator not in OPERATORS:
        raise FilterError(f"Unknown operator: {operator}")
    if value == "me":
        value = me
    if fld == "tags":
        return _tags_condition(operator, value)
    if fld == "list":
        return FilterCondition("list", "list", operator, value)
    if fld and fld.startswith("custom."):
        return _custom_condition(fld[len("custom."):], operator, value)
    if fld and fld.startswith("company."):
        return _company_condition(fld[len("company."):], operator, value, revenue_allowed)
    return _contact_condition(fld, operator, value)


def parse_filter_tree(spec: Any, me: str, revenue_allowed: Callable[[], bool] = lambda: True,
                      depth: int = 0, counter: Optional[list[int]] = None) -> Optional[FilterNode]:
    """The checked tree for a filter spec; None when it is empty (means "everything").

    ``me`` replaces the value "me" (the current user's id); ``revenue_allowed`` is asked only when a
    condition filters on the company's annual revenue."""
    counter = counter if counter is not None else [0]
    if not spec:
        return None
    if depth > MAX_FILTER_DEPTH:
        raise FilterError("Filters are nested too deeply")
    if "conditions" in spec:
        parts = tuple(c for c in (parse_filter_tree(s, me, revenue_allowed, depth + 1, counter)
                                  for s in spec.get("conditions") or []) if c is not None)
        if not parts:
            return None
        return FilterGroup("OR" if str(spec.get("op", "AND")).upper() == "OR" else "AND", parts)
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise FilterError(f"At most {MAX_CONDITIONS} conditions")
    return parse_condition(spec, me, revenue_allowed)


def parse_filter_param(raw: Optional[str]) -> Optional[dict]:
    """The ``filters`` query parameter (JSON object) as a spec; None when absent."""
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        raise FilterError("filters must be JSON") from None
    if not isinstance(spec, dict):
        raise FilterError("filters must be an object")
    return spec


# ── List / board / export filters ───────────────────────────────────

@dataclass(frozen=True, slots=True)
class ContactFilter:
    """The filters the list, board and export endpoints accept, as sent (all optional).

    ``owner``: "me", "team", "unassigned" or a user id. ``filters``: the JSON AND/OR filter.
    ``grade``: "A" or "A,B"; ``score_min`` / ``score_max``: 0-100 (BR-CM-43)."""

    q: Optional[str] = None
    owner: Optional[str] = None
    account_id: Optional[str] = None
    tag: Optional[str] = None
    list_id: Optional[str] = None
    campaign_id: Optional[str] = None
    consent_status: Optional[str] = None
    email_valid: Optional[bool] = None
    has_phone: Optional[bool] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    lead_status: Optional[str] = None
    filters: Optional[str] = None
    grade: Optional[str] = None
    score_min: Optional[int] = None
    score_max: Optional[int] = None

    @property
    def searching(self) -> bool:
        return bool(self.q and self.q.strip())


@dataclass(frozen=True, slots=True)
class ListScope:
    """Membership of one list: static lists by their member rows, active lists by their filter tree
    evaluated live over the list tenant's live contacts."""

    list_id: str
    tenant_id: str
    active: bool = False
    tree: Optional[FilterNode] = None


@dataclass(frozen=True)
class ContactCriteria:
    """A validated ``ContactFilter`` for one user: what the repository turns into SQL."""

    tenant_id: str
    visibility: VisibilityScope = field(default_factory=VisibilityScope)
    owner_id: Optional[str] = None
    unassigned: bool = False
    search: tuple[str, ...] = ()
    account_id: Optional[str] = None
    tag: Optional[str] = None
    in_list: Optional[ListScope] = None
    campaign_id: Optional[str] = None
    consent_status: Optional[str] = None
    email_valid: Optional[bool] = None
    has_phone: Optional[bool] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    lead_status: Optional[str] = None
    grades: Optional[tuple[str, ...]] = None
    score_min: Optional[int] = None
    score_max: Optional[int] = None
    tree: Optional[FilterNode] = None


def owner_criteria(owner: Optional[str], me: str) -> tuple[Optional[str], bool]:
    """(owner id to match, unassigned only) for the owner filter; "team" narrows nothing further."""
    if owner == "me":
        return me, False
    if owner == "unassigned":
        return None, True
    if owner and owner != "team":
        return owner, False
    return None, False


# ── Tasks (BR-CM-15) ────────────────────────────────────────────────

TASK_PRIORITIES = ("LOW", "MEDIUM", "HIGH")
TASK_TYPES = ("TODO", "CALL", "EMAIL", "MEETING")


@dataclass(frozen=True, slots=True)
class TaskFilter:
    """The task queue's filters. ``scope``: mine / all / created / team; ``status``: OPEN / DONE / ALL;
    ``due``: overdue / today / week / none. A record id (deal, contact, company) wins over ``scope``."""

    scope: str = "mine"
    status: Optional[str] = "OPEN"
    due: Optional[str] = None
    prospect_id: Optional[str] = None
    account_id: Optional[str] = None
    opportunity_id: Optional[str] = None
