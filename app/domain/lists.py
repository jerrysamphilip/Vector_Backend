"""
Lists aggregate: contact lists and upload ("prospect") lists -- typed query objects and pure rules
(ADR 0009, chunk A1; BR-CM-22/23/24, gap list C-21).

Routers build a ``ListFilter`` / ``ProspectListFilter`` plus ``Paging`` (app/domain/paging.py); the
services resolve them for the caller and the repository turns them into SQL. List names are unique per
workspace, compared without case (and, through the column collation, without accents).

Errors are ``ValueError`` subclasses (``ListRuleError``); the services turn them into 400s.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

from app.domain.paging import Sort, SortDirection

LIST_TYPES = ("STATIC", "ACTIVE")
MAX_UNPAGED_LISTS = 500  # GET /lists without page parameters (the original response)
PROSPECT_LIST_SORT_FIELDS = frozenset({"list_name", "prospect_count", "uploaded_at"})

NAME_REQUIRED = "Give the list a name"
NAME_TAKEN = "A list with this name already exists. Choose another name."
ACTIVE_NEEDS_FILTER = "An active list needs at least one filter condition"
ACTIVE_UPDATES_ITSELF = "Active lists update automatically from their filters"
ONLY_ACTIVE_FILTERS = "Only active lists have filters"
BAD_LIST_TYPE = "list_type must be STATIC or ACTIVE"


class ListRuleError(ValueError):
    """A list request that breaks a rule (400)."""


@dataclass(frozen=True, slots=True)
class ListFilter:
    """GET /lists: type and name search."""

    list_type: Optional[str] = None
    q: Optional[str] = None

    @property
    def search(self) -> Optional[str]:
        return self.q.strip() if self.q and self.q.strip() else None


@dataclass(frozen=True, slots=True)
class ProspectListFilter:
    """GET /prospect-lists: name search and sort (``prospect_count`` sorts like ``uploaded_at``, as before)."""

    search: Optional[str] = None
    sort: Sort = field(default_factory=lambda: Sort("uploaded_at", SortDirection.DESC))


def prospect_list_sort(sort_by: Optional[str], sort_order: Optional[str]) -> Sort:
    field_name = sort_by or "uploaded_at"
    if field_name not in PROSPECT_LIST_SORT_FIELDS:
        raise ListRuleError(f"Cannot sort by '{field_name}'")
    return Sort(field_name, SortDirection(sort_order or "desc"))


def clean_name(value: Any) -> Optional[str]:
    """Trimmed list name, None when blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def require_name(value: Any) -> str:
    name = clean_name(value)
    if not name:
        raise ListRuleError(NAME_REQUIRED)
    return name


def list_type_of(value: Optional[str]) -> str:
    list_type = (value or "STATIC").upper()
    if list_type not in LIST_TYPES:
        raise ListRuleError(BAD_LIST_TYPE)
    return list_type


def require_conditions(filters: Any) -> None:
    """An active list's filter must be a group with at least one condition."""
    if not filters or not isinstance(filters, dict) or not filters.get("conditions"):
        raise ListRuleError(ACTIVE_NEEDS_FILTER)


def require_static(list_type: Optional[str]) -> None:
    if list_type != "STATIC":
        raise ListRuleError(ACTIVE_UPDATES_ITSELF)


def new_members(requested: Iterable[str], valid: set[str], existing: set[str]) -> list[str]:
    """Ids to add, in request order, once each: in the workspace and not already in the list."""
    return list(dict.fromkeys(pid for pid in requested if pid in valid and pid not in existing))


def user_label(first_name: Optional[str], last_name: Optional[str]) -> str:
    return f"{first_name} {last_name}".strip()


# ── Re-validation of an upload list (prospect lists) ─────────────────

@dataclass(slots=True)
class RevalidationOutcome:
    status: str = "valid"  # valid / warning / invalid
    issues: list[str] = field(default_factory=list)

    def warn(self, issue: str) -> None:
        self.issues.append(issue)
        if self.status != "invalid":
            self.status = "warning"

    def fail(self, issue: str) -> None:
        self.issues.append(issue)
        self.status = "invalid"


def duplicate_emails(emails: Iterable[Optional[str]]) -> set[str]:
    """Lower-cased addresses that appear more than once."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for email in emails:
        if not email:
            continue
        key = email.lower()
        if key in seen:
            dupes.add(key)
        seen.add(key)
    return dupes


def unsubscribed_issue(reason: Optional[str]) -> str:
    return f"Globally unsubscribed ({reason})" if reason else "Globally unsubscribed"
