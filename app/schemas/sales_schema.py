"""
Request bodies of the sales API (app/routers/sales_router.py, sales_admin_router.py, quotes_router.py).

The class names are the OpenAPI component names; keep them stable.
"""
from datetime import date, datetime
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Sales hierarchy, leads, stages, deals, proposals (sales_router) ──

class HierarchyUpdate(BaseModel):
    sales_level: Optional[int] = None  # 1..4, checked in set_hierarchy (400, not 422)
    manager_id: Optional[str] = Field(None, max_length=36)


class LeadCreate(BaseModel):
    prospect_id: str
    owner_id: Optional[str] = None
    source: Optional[str] = None
    stage: Optional[str] = "NEW"
    next_step: Optional[str] = None
    next_step_at: Optional[datetime] = None
    campaign_id: Optional[str] = None


class LeadUpdate(BaseModel):
    owner_id: Optional[str] = None
    stage: Optional[str] = None
    source: Optional[str] = None
    next_step: Optional[str] = None
    next_step_at: Optional[datetime] = None
    disqualified_reason: Optional[str] = None
    qualification: Optional[Dict[str, Any]] = None
    recycle_in_days: Optional[int] = None   # reopen a disqualified lead after this many days (BR-SF-02)
    expected_version: Optional[int] = Field(None, ge=1)  # row_version the client loaded (T-3)


class ConvertRequest(BaseModel):
    name: Optional[str] = None
    amount: Optional[Any] = None
    close_date: Optional[date] = None
    stage_id: Optional[str] = None
    owner_id: Optional[str] = None
    client_type: Optional[str] = None
    next_step: Optional[str] = None


class FromMessage(BaseModel):
    message_id: str
    owner_id: Optional[str] = None


class StageWrite(BaseModel):
    name: Optional[str] = None
    probability: Optional[int] = None
    sort_order: Optional[int] = None
    is_won: Optional[bool] = None
    is_lost: Optional[bool] = None
    active: Optional[bool] = None


class OppWrite(BaseModel):
    name: Optional[str] = None
    account_id: Optional[str] = None
    prospect_id: Optional[str] = None
    owner_id: Optional[str] = None
    stage_id: Optional[str] = None
    amount: Optional[Any] = None
    close_date: Optional[date] = None
    client_type: Optional[str] = None
    closed_reason: Optional[str] = None
    next_step: Optional[str] = None
    description: Optional[str] = None
    forecast_category: Optional[str] = None
    expected_version: Optional[int] = Field(None, ge=1)  # updates only: row_version the client loaded (T-3)


class DealActivity(BaseModel):
    activity_type: str = "NOTE"
    subject: Optional[str] = None
    body: Optional[str] = None
    outcome: Optional[str] = None
    duration_minutes: Optional[int] = None
    occurred_at: Optional[datetime] = None
    add_to_calendar: bool = False
    # Colleagues @mentioned in a note (BR-CM-16)
    mentioned_user_ids: Optional[List[Annotated[str, Field(min_length=1, max_length=36)]]] = Field(None, max_length=20)


class ProposalWrite(BaseModel):
    opportunity_id: Optional[str] = None
    title: Optional[str] = None
    amount: Optional[Any] = None
    status: Optional[str] = None
    valid_until: Optional[date] = None
    notes: Optional[str] = None
    expected_version: Optional[int] = Field(None, ge=1)  # updates only: row_version the client loaded (T-3)


# ── Settings, workflow rules, notifications, targets, templates (sales_admin_router) ──

class RuleWrite(BaseModel):
    name: Optional[str] = None
    object_type: Optional[str] = None
    field: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    actions: Optional[List[Dict[str, Any]]] = None
    active: Optional[bool] = None


class ReadRequest(BaseModel):
    ids: Optional[List[str]] = None   # none = all


class PrefRequest(BaseModel):
    email: bool


class TargetWrite(BaseModel):
    user_id: str
    fy: int
    quarter: int
    amount: Any


class TemplateWrite(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    shared: Optional[bool] = None


class PreviewRequest(BaseModel):
    prospect_id: Optional[str] = None
    count_use: bool = False


# ── Products, quote lines, discount approval (quotes_router) ──

class ProductWrite(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[Any] = None
    unit: Optional[str] = None
    active: Optional[bool] = None


class LineIn(BaseModel):
    product_id: Optional[str] = None
    description: Optional[str] = None
    quantity: Any = 1
    unit_price: Optional[Any] = None
    discount_pct: Any = 0


class LinesIn(BaseModel):
    lines: List[LineIn]
    expected_version: Optional[int] = Field(None, ge=1)  # the proposal's row_version the client loaded (T-3)


class ApprovalSettingsIn(BaseModel):
    # None turns approval off; otherwise quotes with a discount above this percent need approval
    discount_approval_threshold: Optional[float] = Field(None, ge=0, le=100)


class SubmitIn(BaseModel):
    note: Optional[str] = Field(None, max_length=1000)
    expected_version: Optional[int] = Field(None, ge=1)  # the proposal's row_version the client loaded (T-3)


class DecisionIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=1000)
    expected_version: Optional[int] = Field(None, ge=1)  # the proposal's row_version the client loaded (T-3)
