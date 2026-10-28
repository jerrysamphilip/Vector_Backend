"""
Accounts (companies) aggregate: typed query objects and pure rules (ADR 0009, chunk A1).

Routers build an ``AccountFilter`` (the request's filter parameters, unvalidated) plus ``Sort`` / ``Paging``
(app/domain/paging.py). The service resolves it into ``AccountCriteria`` for one user (owner "me" resolved, the
JSON AND/OR filter parsed, the caller's ``VisibilityScope`` attached), which app/repositories/accounts.py turns
into SQL.

Also here, free of I/O:

* the company filter tree: company properties (``name``, ``industry`` ...) and ``custom.<key>`` company custom
  properties (C-16), with the operators of the contact filter engine;
* field propagation (C-20): which company fields are copied to the contacts whose primary company it is;
* the backfill planner (C-14): given the contacts without a company and the tenant's companies, what a backfill
  would create, link and refuse. The preview and the run use the same plan.

Errors are ``ValueError`` subclasses (``FilterError`` from app/domain/contacts.py, ``BackfillError``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from app.domain.contacts import (
    COMPANY_FILTER_FIELDS,
    MAX_CONDITIONS,
    MAX_FILTER_DEPTH,
    OPERATORS,
    FilterCondition,
    FilterError,
    FilterGroup,
    FilterNode,
    VisibilityScope,
    parse_datetime,
)
from app.domain.paging import Sort, SortDirection

__all__ = [
    "ACCOUNT_COLUMNS", "ACCOUNT_SORT_FIELDS", "AccountCriteria", "AccountFilter", "BackfillContact",
    "BackfillError", "BackfillGroup", "BackfillPlan", "CompanyIndex", "EDITABLE", "PROPAGATED_FIELDS",
    "account_sort", "name_from_domain", "parse_company_filter", "plan_backfill", "propagation_values",
]

# ── Fields ─────────────────────────────────────────────────────────

EDITABLE = ("name", "domain", "website", "phone", "industry", "emp_band", "street", "city", "state",
            "postal_code", "country", "description", "owner_id", "annual_revenue", "lifecycle_stage")

# Company field -> contact field copied to the contacts whose primary company it is (C-20)
PROPAGATED_FIELDS: Mapping[str, str] = {"name": "company_name", "industry": "industry", "emp_band": "emp_band"}

# Columns of the companies table and export (key -> label); custom properties are appended per tenant
ACCOUNT_COLUMNS: Mapping[str, str] = {
    "name": "Company", "domain": "Domain", "website": "Website", "phone": "Phone", "industry": "Industry",
    "emp_band": "Employees", "city": "City", "state": "State", "country": "Country",
    "lifecycle_stage": "Lifecycle stage", "annual_revenue": "Annual revenue", "contact_count": "Contacts",
    "owner_name": "Owner", "created_at": "Create date", "updated_at": "Last updated",
}
MONEY_COLUMNS = frozenset({"annual_revenue"})


def propagation_values(field_name: str, old: Optional[str], new: Optional[str],
                       contact_value: Optional[str]) -> tuple[bool, Optional[str]]:
    """(change?, value) for a contact's copy of a company field after the company changed it from ``old`` to
    ``new``. The name always follows the company (a contact's company name *is* its company's name); industry
    and size follow only where the contact still had the company's old value or nothing at all, so a value
    someone set on the contact on purpose is kept."""
    old, new, current = old or None, new or None, contact_value or None
    if old == new or current == new:
        return False, None
    if field_name == "name" or current is None or current == old:
        return True, new
    return False, None


# ── Sorting and list criteria ──────────────────────────────────────

ACCOUNT_SORT_FIELDS = frozenset({"name", "contacts", "annual_revenue", "created_at", "updated_at"})
AccountStatus = Literal["active", "archived"]


def account_sort(sort_by: str, sort_order: str) -> Sort:
    if sort_by not in ACCOUNT_SORT_FIELDS:
        raise ValueError(f"Cannot sort by '{sort_by}'")
    return Sort(sort_by, SortDirection(sort_order))


@dataclass(frozen=True, slots=True)
class AccountFilter:
    """The list / export filter parameters as the request gave them."""

    q: Optional[str] = None
    owner: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    industry: Optional[str] = None
    filters: Optional[str] = None
    status: AccountStatus = "active"


@dataclass(frozen=True, slots=True)
class AccountCriteria:
    """A validated company query for one user. ``archived`` lists the recycle bin (deleted after ``cutoff``)."""

    tenant_id: str
    scope: VisibilityScope = field(default_factory=VisibilityScope)
    owner_id: Optional[str] = None
    unassigned: bool = False
    lifecycle_stage: Optional[str] = None
    industry: Optional[str] = None
    q: Optional[str] = None
    tree: Optional[FilterNode] = None
    archived: bool = False
    cutoff: Optional[datetime] = None


def owner_criteria(owner: Optional[str], me: str) -> tuple[Optional[str], bool]:
    """(owner_id, unassigned) for the ``owner`` parameter: 'me', 'unassigned' or a user id."""
    if owner == "me":
        return me, False
    if owner == "unassigned":
        return None, True
    return owner or None, False


# ── Company filter tree (C-16) ─────────────────────────────────────
#
# Same shape as the contact filter: a condition {"field", "operator", "value"} or a group
# {"op": "AND"|"OR", "conditions": [...]}. Fields are COMPANY_FILTER_FIELDS or "custom.<key>".

_CUSTOM_KEY = re.compile(r"[a-z0-9_]{1,64}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RANGE = ("gt", "gte", "lt", "lte", "between")


def _company_value(operator: str, value: Any, is_text: bool = True) -> Any:
    if operator in ("after", "before") and not is_text:
        return parse_datetime(value)
    if operator == "in_last_days":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise FilterError("in_last_days needs a number of days") from None
    return value


def _condition(cond: Mapping[str, Any], me: str, revenue_allowed: Callable[[], bool]) -> FilterCondition:
    fld, operator, value = cond.get("field"), cond.get("operator"), cond.get("value")
    if operator not in OPERATORS:
        raise FilterError(f"Unknown operator: {operator}")
    if value == "me":
        value = me
    name = str(fld or "")
    if name.startswith("custom."):
        key = name[len("custom."):]
        if not _CUSTOM_KEY.fullmatch(key):
            raise FilterError(f"Invalid custom field: {key}")
        is_date = isinstance(value, str) and bool(_ISO_DATE.fullmatch(value))
        numeric = operator in _RANGE and not is_date
        return FilterCondition("custom", key, operator, _company_value(operator, value), numeric=numeric)
    if name.startswith("company."):
        name = name[len("company."):]
    if name not in COMPANY_FILTER_FIELDS:
        raise FilterError(f"Unknown company property: {fld}")
    if name == "annual_revenue" and not revenue_allowed():
        raise FilterError("Your role cannot filter on revenue")
    is_text = name != "annual_revenue"
    return FilterCondition("company", name, operator, _company_value(operator, value, is_text), is_text=is_text)


def parse_company_filter(spec: Any, me: str, revenue_allowed: Callable[[], bool] = lambda: True,
                         depth: int = 0, counter: Optional[list[int]] = None) -> Optional[FilterNode]:
    """The checked tree for a company filter spec; None when empty (everything)."""
    counter = counter if counter is not None else [0]
    if not spec:
        return None
    if not isinstance(spec, Mapping):
        raise FilterError("filters must be an object")
    if depth > MAX_FILTER_DEPTH:
        raise FilterError("Filters are nested too deeply")
    if "conditions" in spec:
        parts = tuple(c for c in (parse_company_filter(s, me, revenue_allowed, depth + 1, counter)
                                  for s in spec.get("conditions") or []) if c is not None)
        if not parts:
            return None
        return FilterGroup("OR" if str(spec.get("op", "AND")).upper() == "OR" else "AND", parts)
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise FilterError(f"At most {MAX_CONDITIONS} conditions")
    return _condition(spec, me, revenue_allowed)


# ── Backfill planner (BR-CM-04, C-14) ──────────────────────────────

BACKFILL_BATCH = 500
SAMPLE_SIZE = 20
EXAMPLES_PER_GROUP = 3

Action = Literal["LINK", "CREATE", "CONFLICT"]


class BackfillError(ValueError):
    """A backfill request that cannot run (already running, already undone ...)."""


def name_from_domain(domain: str) -> str:
    """'acme-labs.com' -> 'Acme Labs' (the name a company created from a domain gets)."""
    label = domain.split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part) or domain


def _fold(value: str) -> str:
    return value.strip().casefold()


@dataclass(frozen=True, slots=True)
class BackfillContact:
    """A contact without a company. ``domain`` is the company domain its email implies (None for free mail)."""

    prospect_id: str
    email: Optional[str]
    company_name: Optional[str]
    domain: Optional[str]
    industry: Optional[str] = None
    emp_band: Optional[str] = None

    @property
    def name(self) -> Optional[str]:
        return (self.company_name or "").strip()[:255] or None


@dataclass(frozen=True, slots=True)
class IndexedCompany:
    account_id: str
    name: str
    domain: Optional[str]


@dataclass
class CompanyIndex:
    """The tenant's companies as the planner sees them (live ones by domain and name, recycle-bin holds)."""

    by_domain: dict[str, IndexedCompany] = field(default_factory=dict)
    by_name: dict[str, IndexedCompany] = field(default_factory=dict)
    deleted_domains: set[str] = field(default_factory=set)
    deleted_names: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, live: Iterable[tuple[str, str, Optional[str]]],
              deleted: Iterable[tuple[str, Optional[str]]]) -> CompanyIndex:
        index = cls()
        for account_id, name, domain in live:
            company = IndexedCompany(account_id, name, domain)
            if domain:
                index.by_domain.setdefault(domain, company)
            index.by_name.setdefault(_fold(name), company)
        for name, domain in deleted:
            index.deleted_names.add(_fold(name))
            if domain:
                index.deleted_domains.add(domain)
        return index

    def name_taken(self, name: str) -> bool:
        key = _fold(name)
        return key in self.by_name or key in self.deleted_names


@dataclass
class BackfillGroup:
    """Contacts that end up at the same company. ``account_id`` is the existing company (LINK); CREATE makes
    ``name`` / ``domain``; ``set_domain`` gives a domain-less existing company its domain; CONFLICT is refused
    (``reason``)."""

    action: Action
    name: str
    domain: Optional[str] = None
    account_id: Optional[str] = None
    set_domain: Optional[str] = None
    reason: Optional[str] = None
    contacts: list[BackfillContact] = field(default_factory=list)
    name_mismatches: int = 0

    def add(self, contact: BackfillContact) -> None:
        self.contacts.append(contact)
        if contact.name and _fold(contact.name) != _fold(self.name) and self.action != "CONFLICT":
            self.name_mismatches += 1

    @property
    def fills(self) -> int:
        """Contacts whose empty company name the run fills with the company's name."""
        return sum(1 for c in self.contacts if not c.name) if self.action != "CONFLICT" else 0

    def sample(self) -> dict[str, Any]:
        return {"action": self.action, "company_name": self.name, "domain": self.domain or self.set_domain,
                "account_id": self.account_id, "set_domain": self.set_domain, "reason": self.reason,
                "contacts": len(self.contacts), "name_mismatches": self.name_mismatches,
                "example_emails": [c.email for c in self.contacts[:EXAMPLES_PER_GROUP]]}


REASONS = {
    "DOMAIN_IN_RECYCLE_BIN": "A deleted company still owns this domain; restore it to link these contacts.",
    "NAME_IN_RECYCLE_BIN": "A deleted company still has this name; restore it to link these contacts.",
}


@dataclass
class BackfillPlan:
    groups: list[BackfillGroup]
    skipped: int  # contacts with neither a company domain nor a company name
    total: int

    def actionable(self) -> list[BackfillGroup]:
        return [g for g in self.groups if g.action != "CONFLICT"]

    def stats(self) -> dict[str, int]:
        live = self.actionable()
        return {
            "contacts_considered": self.total,
            "contacts_to_link": sum(len(g.contacts) for g in live),
            "companies_to_create": sum(1 for g in live if g.action == "CREATE"),
            "existing_companies_used": sum(1 for g in live if g.action == "LINK"),
            "domains_to_set": sum(1 for g in live if g.set_domain),
            "company_names_to_fill": sum(g.fills for g in live),
            "name_mismatches": sum(g.name_mismatches for g in live),
            "contacts_in_conflict": sum(len(g.contacts) for g in self.groups if g.action == "CONFLICT"),
            "contacts_skipped": self.skipped,
        }

    def samples(self, limit: int = SAMPLE_SIZE) -> list[dict[str, Any]]:
        return [g.sample() for g in sorted(self.actionable(), key=lambda g: -len(g.contacts))[:limit]]

    def conflicts(self, limit: int = SAMPLE_SIZE) -> list[dict[str, Any]]:
        rows = [{**g.sample(), "message": REASONS.get(g.reason or "", g.reason)}
                for g in self.groups if g.action == "CONFLICT"]
        mismatched = [{**g.sample(), "reason": "NAME_MISMATCH",
                       "message": "Some contacts give a different company name than the company their email "
                                  "domain belongs to; they are linked by domain."}
                      for g in self.actionable() if g.name_mismatches]
        return (rows + mismatched)[:limit]


class _Planner:
    def __init__(self, index: CompanyIndex) -> None:
        self.index = index
        self.by_domain: dict[str, BackfillGroup] = {}
        self.by_name: dict[str, BackfillGroup] = {}
        self.groups: list[BackfillGroup] = []
        self.claimed_domainless: set[str] = set()

    def _new(self, group: BackfillGroup) -> BackfillGroup:
        self.groups.append(group)
        if group.action == "CREATE":
            self.by_name.setdefault(_fold(group.name), group)
        return group

    def _create_name(self, base: str, domain: Optional[str]) -> str:
        taken = self.index.name_taken(base) or _fold(base) in self.by_name
        return f"{base} ({domain})"[:255] if taken and domain else base

    def for_domain(self, domain: str, name: Optional[str]) -> BackfillGroup:
        group = self.by_domain.get(domain)
        if group:
            return group
        existing = self.index.by_domain.get(domain)
        if existing:
            group = self._new(BackfillGroup("LINK", existing.name, domain, existing.account_id))
        elif name and self._domainless(name):
            same = self.index.by_name[_fold(name)]
            self.claimed_domainless.add(same.account_id)
            group = self._new(BackfillGroup("LINK", same.name, None, same.account_id, set_domain=domain))
        elif domain in self.index.deleted_domains:
            group = self._new(BackfillGroup("CONFLICT", name or domain, domain, reason="DOMAIN_IN_RECYCLE_BIN"))
        else:
            new_name = self._create_name(name or name_from_domain(domain), domain)
            group = self._new(BackfillGroup("CREATE", new_name, domain))
        self.by_domain[domain] = group
        return group

    def _domainless(self, name: str) -> bool:
        same = self.index.by_name.get(_fold(name))
        return bool(same and not same.domain and same.account_id not in self.claimed_domainless)

    def for_name(self, name: str) -> BackfillGroup:
        key = _fold(name)
        group = self.by_name.get(key)
        if group:
            return group
        existing = self.index.by_name.get(key)
        if existing:
            group = self._new(BackfillGroup("LINK", existing.name, existing.domain, existing.account_id))
        elif key in self.index.deleted_names:
            group = self._new(BackfillGroup("CONFLICT", name, None, reason="NAME_IN_RECYCLE_BIN"))
        else:
            group = self._new(BackfillGroup("CREATE", name, None))
        self.by_name[key] = group
        return group


def plan_backfill(contacts: Sequence[BackfillContact], index: CompanyIndex) -> BackfillPlan:
    """What a backfill does with these contacts (in this order), mirroring the live rule (BR-CM-04): the email's
    company domain finds or creates the company (a domain-less company with the same name gets the domain);
    personal addresses fall back to the company name. A company in the recycle bin keeps its domain and name,
    so its contacts are reported as conflicts instead of being linked to it or to a duplicate."""
    planner = _Planner(index)
    skipped = 0
    for contact in contacts:
        if contact.domain:
            planner.for_domain(contact.domain, contact.name).add(contact)
        elif contact.name:
            planner.for_name(contact.name).add(contact)
        else:
            skipped += 1
    return BackfillPlan(planner.groups, skipped, len(contacts))


def batches(groups: Sequence[BackfillGroup], size: int = BACKFILL_BATCH) -> list[list[BackfillGroup]]:
    """Groups cut into batches of about ``size`` contacts (a group is never split)."""
    out: list[list[BackfillGroup]] = []
    current: list[BackfillGroup] = []
    count = 0
    for group in groups:
        current.append(group)
        count += len(group.contacts)
        if count >= size:
            out.append(current)
            current, count = [], 0
    if current:
        out.append(current)
    return out
