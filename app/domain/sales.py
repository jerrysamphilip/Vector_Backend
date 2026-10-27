"""
Sales aggregate: value objects, status rules and typed query objects (ADR 0009, chunk A3).

Leads, opportunities (deals), proposals (quotes) and their stages. Everything here is pure: no I/O,
no SQLAlchemy, no FastAPI, no models. Routers build the query objects from request parameters,
services apply the business rules, repositories turn the query objects into SQL.

Invalid values raise ``ValueError``; the service layer maps them to the domain errors the API has
always returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Optional, Protocol
from uuid import UUID

from app.domain.paging import Paging, Sort, SortDirection

# ── Status vocabularies (shared with app.models.sales, which re-exports them) ──

# Lead stages, in order, with the contact lifecycle stage each one implies (BR-LD-02)
LEAD_STAGES: dict[str, tuple[str, Optional[str]]] = {
    "NEW": ("New", "LEAD"),
    "CONTACTED": ("Contacted", "LEAD"),
    "ENGAGED": ("Engaged (MQL)", "MQL"),
    "SQL": ("Sales qualified (SQL)", "SQL"),
    "CONVERTED": ("Converted to opportunity", "OPPORTUNITY"),
    "DISQUALIFIED": ("Disqualified", None),
}
OPEN_LEAD_STAGES = ("NEW", "CONTACTED", "ENGAGED", "SQL")

# SQL qualification criteria (BR-LD-03): BANT. All must be met to move a lead to SQL.
SQL_CRITERIA = {
    "budget": "Budget confirmed",
    "authority": "Talking to the decision maker",
    "need": "Clear business need",
    "timeline": "Buying timeline known",
}

PROPOSAL_STATUSES = {
    "DRAFT": "Draft", "SENT": "Sent", "UNDER_REVIEW": "Under review",
    "ACCEPTED": "Accepted", "REJECTED": "Rejected",
}
CLIENT_TYPES = {"NEW": "New client", "EXISTING": "Existing client"}

# Seeded per workspace on first use; admins can rename, re-weight, add or retire stages
DEFAULT_SALES_STAGES = [
    ("Qualification", 10, False, False),
    ("Needs analysis", 25, False, False),
    ("Proposal", 50, False, False),
    ("Negotiation", 75, False, False),
    ("Closed won", 100, True, False),
    ("Closed lost", 0, False, True),
]

FORECAST_CATEGORIES = {"PIPELINE": "Pipeline", "BEST_CASE": "Best case", "COMMIT": "Commit",
                       "CLOSED": "Closed", "OMITTED": "Omitted"}

# Fields tracked in the change history (property_changes)
LEAD_FIELDS = ("owner_id", "stage", "source", "next_step", "next_step_at", "disqualified_reason", "recycle_at")
OPP_FIELDS = ("name", "owner_id", "stage_id", "amount", "close_date", "client_type", "closed_reason",
              "next_step", "description", "account_id", "prospect_id", "forecast_category")

SALES_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")

LEAD_SORT_FIELDS = frozenset({"created_at", "stage_changed_at", "next_step_at"})
OPPORTUNITY_SORT_FIELDS = frozenset({"close_date", "amount", "created_at", "updated_at", "name"})


class StageStatus(str, Enum):
    """What a sales stage means for its deals: still open, won or lost."""

    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"

    @classmethod
    def of(cls, is_won: bool, is_lost: bool) -> StageStatus:
        return cls.WON if is_won else cls.LOST if is_lost else cls.OPEN


class ForecastCategory(str, Enum):
    PIPELINE = "PIPELINE"
    BEST_CASE = "BEST_CASE"
    COMMIT = "COMMIT"
    CLOSED = "CLOSED"
    OMITTED = "OMITTED"


class StageLike(Protocol):
    """Anything with the stage fields the rules read (the ORM row, or a test double)."""

    is_won: Any
    is_lost: Any
    probability: Any


def stage_status(stage: StageLike) -> str:
    return StageStatus.of(bool(stage.is_won), bool(stage.is_lost)).value


def category_for(stage: StageLike) -> str:
    """Default forecast category from the stage (BR-SF-08)."""
    if stage.is_won:
        return ForecastCategory.CLOSED.value
    if stage.is_lost:
        return ForecastCategory.OMITTED.value
    if stage.probability >= 70:
        return ForecastCategory.COMMIT.value
    if stage.probability >= 40:
        return ForecastCategory.BEST_CASE.value
    return ForecastCategory.PIPELINE.value


def missing_criteria(qualification: Optional[Mapping[str, Any]]) -> list[str]:
    """Labels of the SQL criteria not yet confirmed."""
    q = qualification or {}
    return [label for key, label in SQL_CRITERIA.items() if not q.get(key)]


def lead_stage_error(current: Optional[str], new: str, qualification: Optional[Mapping[str, Any]],
                     disqualified_reason: Optional[str]) -> Optional[str]:
    """Why a lead may not move from ``current`` to ``new`` (None = allowed). ``new == current`` is a no-op."""
    if new not in LEAD_STAGES:
        return f"stage must be one of {list(LEAD_STAGES)}"
    if new == current:
        return None
    if current == "CONVERTED":
        return "This lead is already converted; work the opportunity instead"
    if new == "CONVERTED":
        return "Use Convert to create the opportunity"
    if new == "SQL":
        missing = missing_criteria(qualification)
        if missing:
            return "To mark as SQL, confirm: " + ", ".join(missing)
    if new == "DISQUALIFIED" and not (disqualified_reason or "").strip():
        return "Give a reason for disqualifying the lead"
    return None


def lifecycle_moves_forward(order: list[str], current: Optional[str], target: Optional[str]) -> bool:
    """Contacts only move forward through the lifecycle (never backward)."""
    if not target:
        return False
    return not (current in order and target in order and order.index(target) <= order.index(current))


def stage_change_recipients(owner_id: Optional[str], manager_id: Optional[str],
                            actor_id: Optional[str]) -> list[str]:
    """Who hears about a deal moving: its owner and the owner's manager, never the person who moved it."""
    return [u for u in dict.fromkeys((owner_id, manager_id)) if u and u != actor_id]


def stage_change_message(deal_name: str, stage_name: str, status: str, actor_name: Optional[str],
                         previous_stage: Optional[str] = None, reason: Optional[str] = None) -> tuple[str, str, str]:
    """(kind, title, body) for a deal won / lost / stage-change notification."""
    deal_name = deal_name or "Deal"
    who = actor_name or "Someone"
    if status == "WON":
        kind, title = "DEAL_WON", f"Deal won: {deal_name}"
        body = f"{who} marked the deal won."
    elif status == "LOST":
        kind, title = "DEAL_LOST", f"Deal lost: {deal_name}"
        body = f"{who} marked the deal lost."
    else:
        kind, title = "DEAL_STAGE", f"Deal moved to {stage_name}: {deal_name}"
        body = f"{who} moved the deal" + (f" from {previous_stage}" if previous_stage else "") + f" to {stage_name}."
    if reason and status in ("WON", "LOST"):
        body += f" Reason: {reason}"
    return kind, title, body


def stale_reason(idle: int, stale_days: int, close_date: Optional[date], today: date) -> Optional[str]:
    """Why an open deal is stale (no activity for stale_days, or past its close date); None when it is not."""
    overdue = bool(close_date and close_date < today)
    if idle < stale_days and not overdue:
        return None
    return (f"no activity for {idle} days" if idle >= stale_days else "") + \
        (" and " if idle >= stale_days and overdue else "") + (f"close date {close_date} has passed" if overdue else "")


def idle_days(last_change: Optional[datetime], last_touch: Optional[datetime], now: datetime) -> int:
    latest = max([d for d in (last_change, last_touch) if d] or [now])
    return max((now - latest).days, 0)


def parse_amount(value: Any) -> Optional[Decimal]:
    """An amount from the API: None / "" = no amount; commas allowed; never negative; 2 decimals."""
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError("Amount must be a number") from None
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    return amount.quantize(Decimal("0.01"))


def stage_write_error(data: Mapping[str, Any]) -> Optional[str]:
    """Validation of a stage create / update payload (None = fine)."""
    if "probability" in data and data["probability"] is not None and not 0 <= data["probability"] <= 100:
        return "Probability must be between 0 and 100"
    if data.get("is_won") and data.get("is_lost"):
        return "A stage cannot be both won and lost"
    return None


def hierarchy_error(level: Optional[int], manager_level: Optional[int], has_manager: bool,
                    manager_is_self_or_team: bool) -> Optional[str]:
    """Rule for a user's sales level and manager (BR-SH-01): managers sit at a higher level."""
    if has_manager:
        if not manager_level or (level and manager_level >= level):
            return "The manager must be at a higher level (a lower level number)"
        if manager_is_self_or_team:
            return "That would make the user manage their own manager"
    elif level and level > 1:
        return "Levels 2 to 4 need a manager"
    return None


# ── Visibility ───────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OwnerScope:
    """Which owners' records a user may see (BR-SH-02).

    ``owner_ids`` None means everyone's; otherwise only those owners, plus unassigned records when
    ``include_unowned`` (users who manage contacts can pick unassigned records up).
    """

    owner_ids: Optional[frozenset[str]] = None
    include_unowned: bool = False

    @property
    def unrestricted(self) -> bool:
        return self.owner_ids is None

    def allows(self, owner_id: Optional[str]) -> bool:
        if self.owner_ids is None:
            return True
        return owner_id in self.owner_ids or (owner_id is None and self.include_unowned)


EVERYONE = OwnerScope()


def resolve_owner(owner: Optional[str], user_id: str) -> Optional[str]:
    """``owner=me`` filters on the current user."""
    if owner == "me":
        return user_id
    return owner or None


def split_csv(value: Optional[str]) -> tuple[str, ...]:
    return tuple(value.split(",")) if value else ()


def search_term(q: Optional[str]) -> Optional[str]:
    return q.strip() if q and q.strip() else None


# ── Query objects ────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LeadFilter:
    """Filters of the lead list, board and SQL queue (C-6 parameter object)."""

    stages: tuple[str, ...] = ()
    open_only: bool = False
    owner_id: Optional[str] = None
    source: Optional[str] = None
    search: Optional[str] = None
    prospect_id: Optional[str] = None

    @classmethod
    def from_params(cls, user_id: str, stage: Optional[str] = None, owner: Optional[str] = None,
                    q: Optional[str] = None, source: Optional[str] = None, open_only: bool = False,
                    prospect_id: Optional[str] = None) -> LeadFilter:
        return cls(stages=split_csv(stage), open_only=open_only, owner_id=resolve_owner(owner, user_id),
                   source=source or None, search=search_term(q), prospect_id=prospect_id or None)

    def with_stage(self, stage: str) -> LeadFilter:
        return LeadFilter(stages=(stage,), open_only=self.open_only, owner_id=self.owner_id, source=self.source,
                          search=self.search, prospect_id=self.prospect_id)

    @property
    def effective_stages(self) -> tuple[str, ...]:
        """Explicit stages win over open_only."""
        if self.stages:
            return self.stages
        return OPEN_LEAD_STAGES if self.open_only else ()


@dataclass(frozen=True, slots=True)
class OpportunityFilter:
    """Filters of the deal list, board, proposal pipeline and revenue pipeline (C-6)."""

    owner_id: Optional[str] = None
    statuses: tuple[str, ...] = ()
    stage_id: Optional[str] = None
    stage_ids: Optional[tuple[str, ...]] = None
    client_type: Optional[str] = None
    close_from: Optional[date] = None
    close_to: Optional[date] = None
    search: Optional[str] = None
    account_id: Optional[str] = None
    prospect_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    # Stale deals only: open, and not updated since stale_before or past their close date (today)
    stale_before: Optional[datetime] = None
    today: Optional[date] = None

    @classmethod
    def from_params(cls, user_id: str, owner: Optional[str] = None, status: Optional[str] = None,
                    stage_id: Optional[str] = None, client_type: Optional[str] = None,
                    close_from: Optional[date] = None, close_to: Optional[date] = None, q: Optional[str] = None,
                    account_id: Optional[str] = None, prospect_id: Optional[str] = None,
                    opportunity_id: Optional[str] = None) -> OpportunityFilter:
        return cls(owner_id=resolve_owner(owner, user_id), statuses=split_csv(status), stage_id=stage_id or None,
                   client_type=client_type or None, close_from=close_from, close_to=close_to,
                   search=search_term(q), account_id=account_id or None, prospect_id=prospect_id or None,
                   opportunity_id=opportunity_id or None)

    def replace(self, **changes: Any) -> OpportunityFilter:
        values = {f: getattr(self, f) for f in self.__dataclass_fields__}
        values.update(changes)
        return OpportunityFilter(**values)


@dataclass(frozen=True, slots=True)
class ListQuery:
    """Sort + paging for a list endpoint; ``sort.field`` is checked against the allowed fields."""

    sort: Sort
    paging: Paging = field(default_factory=Paging)

    @classmethod
    def build(cls, sort_by: str, sort_order: str, page: int, page_size: int,
              allowed: frozenset[str]) -> ListQuery:
        if sort_by not in allowed:
            raise ValueError(f"Cannot sort by '{sort_by}'")
        direction = SortDirection.ASC if sort_order == "asc" else SortDirection.DESC
        return cls(Sort(sort_by, direction), Paging(page, page_size))


@dataclass(frozen=True, slots=True)
class ProposalFilter:
    statuses: tuple[str, ...] = ()
    opportunities: OpportunityFilter = field(default_factory=OpportunityFilter)
    limit: int = 1000


@dataclass(frozen=True, slots=True)
class ApprovalQuery:
    """/quotes/approvals: whose quotes and which approval states."""

    scope: str = "mine"            # mine / requested
    status: str = "PENDING"        # PENDING / APPROVED / REJECTED / ALL
    limit: int = 200

    @property
    def statuses(self) -> tuple[str, ...]:
        return ("PENDING", "APPROVED", "REJECTED") if self.status == "ALL" else (self.status,)


def naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Timestamps are stored as naive UTC like every other timestamp."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def parse_entity_id(value: str) -> Optional[UUID]:
    """A record id from a URL: the canonical 8-4-4-4-12 UUID form (any case), else None (C-7).

    Every sales record id is a UUID string, so anything else cannot name a record: the API keeps
    answering 404 for it, as it did when the id went to the database as text."""
    if len(value) != 36:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
