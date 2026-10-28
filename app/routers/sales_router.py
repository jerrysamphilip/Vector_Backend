# app/routers/sales_router.py
"""
Phase 2 sales API (BRD v2.0):
  /team/hierarchy      sales levels and managers (5.4)
  /leads               leads, board, SQL queue, qualification, conversion (5.6)
  /sales/stages        configurable sales stages with probabilities (BR-SP-01)
  /opportunities       deals, board (5.7)
  /proposals           proposal pipeline (BR-SP-02)
  /pipeline/revenue    weighted / unweighted revenue pipeline (BR-SP-03)

Everything is limited to the records of the user and their team.

Thin routes (ADR 0009): parse the request, call one service method (app/services/sales_leads.py,
sales_deals.py, sales_admin.py), return its result. Record ids in paths are UUIDs (C-7); anything
else answers 404 as before.
"""
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.concurrency import if_match_version
from app.core.database import get_db
from app.domain.sales import (LEAD_SORT_FIELDS, OPPORTUNITY_SORT_FIELDS, LeadFilter, ListQuery, OpportunityFilter,
                              ProposalFilter, split_csv)
from app.routers.tasks_router import task_dict
from app.schemas.sales_schema import (ConvertRequest, DealActivity, FromMessage, HierarchyUpdate, LeadCreate,
                                      LeadUpdate, OppWrite, ProposalWrite, StageWrite)
from app.services.sales import entity_id
from app.services.sales_admin import SalesAdminService
from app.services.sales_deals import DealService
from app.services.sales_leads import LeadService

router = APIRouter(tags=["Sales"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")

LEAD_MISSING, DEAL_MISSING, PROPOSAL_MISSING = "Lead not found", "Opportunity not found", "Proposal not found"


def lead_service(db=Depends(get_db)) -> LeadService:
    return LeadService.for_session(db)


def deal_service(db=Depends(get_db)) -> DealService:
    return DealService.for_session(db)


def admin_service(db=Depends(get_db)) -> SalesAdminService:
    return SalesAdminService.for_session(db)


# =============================================================
# Sales hierarchy (BR-SH-01/02)
# =============================================================

@router.get("/team/hierarchy")
def get_hierarchy(service: SalesAdminService = Depends(admin_service), current_user: Any = Depends(tenant_user)):
    return service.hierarchy(current_user)


@router.put("/team/hierarchy/{user_id}")
def set_hierarchy(user_id: str, payload: HierarchyUpdate, service: SalesAdminService = Depends(admin_service),
                  current_user: Any = Depends(admin_user)):
    """Set a user's sales level and manager (admins). The manager must sit at a higher level."""
    return service.set_hierarchy(current_user, entity_id(user_id, "User not found"), payload.sales_level,
                                 payload.manager_id or None)


# =============================================================
# Leads (BR-LD-01..06)
# =============================================================

@router.get("/leads/meta")
def leads_meta(service: LeadService = Depends(lead_service), current_user: Any = Depends(tenant_user)):
    return service.meta(current_user)


@router.get("/leads")
def list_leads(stage: Optional[str] = None, owner: Optional[str] = None, q: Optional[str] = None,
               source: Optional[str] = None, open_only: bool = False, prospect_id: Optional[str] = None,
               sort_by: str = Query("created_at", pattern="^(created_at|stage_changed_at|next_step_at)$"),
               sort_order: str = Query("desc", pattern="^(asc|desc)$"),
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
               service: LeadService = Depends(lead_service), current_user: Any = Depends(tenant_user)):
    flt = LeadFilter.from_params(current_user.user_id, stage, owner, q, source, open_only, prospect_id)
    return service.list_leads(current_user, flt,
                              ListQuery.build(sort_by, sort_order, page, page_size, LEAD_SORT_FIELDS))


@router.get("/leads/board")
def lead_board(owner: Optional[str] = None, q: Optional[str] = None, source: Optional[str] = None,
               include_closed: bool = False, per_stage: int = Query(50, ge=1, le=200),
               service: LeadService = Depends(lead_service), current_user: Any = Depends(tenant_user)):
    """Leads pipeline board by stage (BR-LD-05)."""
    flt = LeadFilter.from_params(current_user.user_id, owner=owner, q=q, source=source)
    return service.board(current_user, flt, include_closed, per_stage)


@router.get("/leads/sql-queue")
def sql_queue(owner: Optional[str] = None, service: LeadService = Depends(lead_service),
              current_user: Any = Depends(tenant_user)):
    """Sales-qualified leads waiting to be converted, oldest first, with age and next step (BR-LD-03/04)."""
    return service.sql_queue(current_user, LeadFilter.from_params(current_user.user_id, owner=owner))


@router.post("/leads", status_code=201)
def create_lead(payload: LeadCreate, service: LeadService = Depends(lead_service),
                current_user: Any = Depends(tenant_user)):
    return service.create(current_user, payload.model_dump())


@router.post("/leads/from-message", status_code=201)
def lead_from_message(payload: FromMessage, service: LeadService = Depends(lead_service),
                      current_user: Any = Depends(tenant_user)):
    """One click: turn a positive campaign reply into an engaged lead (BR-SF-01)."""
    return service.create_from_message(current_user, payload.message_id, payload.owner_id)


@router.get("/leads/reply-suggestions")
def reply_suggestions(service: LeadService = Depends(lead_service), current_user: Any = Depends(tenant_user)):
    """Recent positive replies from contacts you can see who have no open lead yet (BR-SF-01)."""
    return service.reply_suggestions(current_user)


@router.get("/leads/{lead_id}")
def get_lead(lead_id: str, service: LeadService = Depends(lead_service), current_user: Any = Depends(tenant_user)):
    return service.detail(current_user, entity_id(lead_id, LEAD_MISSING))


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: str, payload: LeadUpdate, service: LeadService = Depends(lead_service),
                current_user: Any = Depends(tenant_user), if_match: Optional[int] = Depends(if_match_version)):
    return service.update(current_user, entity_id(lead_id, LEAD_MISSING), payload.model_dump(exclude_unset=True),
                          if_match)


@router.post("/leads/{lead_id}/convert", status_code=201)
def convert(lead_id: str, payload: ConvertRequest, service: LeadService = Depends(lead_service),
            current_user: Any = Depends(tenant_user)):
    return service.convert(current_user, entity_id(lead_id, LEAD_MISSING), payload.model_dump(exclude_unset=True))


@router.delete("/leads/{lead_id}")
def delete_lead(lead_id: str, service: LeadService = Depends(lead_service), current_user: Any = Depends(tenant_user)):
    return service.delete(current_user, entity_id(lead_id, LEAD_MISSING))


# =============================================================
# Sales stages (BR-SP-01)
# =============================================================

@router.get("/sales/stages")
def list_stages(include_inactive: bool = False, service: DealService = Depends(deal_service),
                current_user: Any = Depends(tenant_user)):
    return service.list_stages(current_user, include_inactive)


@router.post("/sales/stages", status_code=201)
def create_stage(payload: StageWrite, service: DealService = Depends(deal_service),
                 current_user: Any = Depends(admin_user)):
    return service.create_stage(current_user, payload.model_dump(exclude_unset=True))


@router.patch("/sales/stages/{stage_id}")
def update_stage(stage_id: str, payload: StageWrite, service: DealService = Depends(deal_service),
                 current_user: Any = Depends(admin_user)):
    return service.update_stage(current_user, entity_id(stage_id, "Stage not found"),
                                payload.model_dump(exclude_unset=True))


# =============================================================
# Opportunities (BR-SP-01, 03, 04)
# =============================================================

@router.get("/opportunities")
def list_opps(owner: Optional[str] = None, status: Optional[str] = None, stage_id: Optional[str] = None,
              client_type: Optional[str] = None, close_from: Optional[date] = None, close_to: Optional[date] = None,
              q: Optional[str] = None, account_id: Optional[str] = None, prospect_id: Optional[str] = None,
              stale: bool = False, sort_by: str = Query("close_date", pattern="^(close_date|amount|created_at|updated_at|name)$"),
              sort_order: str = Query("asc", pattern="^(asc|desc)$"),
              page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
              service: DealService = Depends(deal_service), current_user: Any = Depends(tenant_user)):
    flt = OpportunityFilter.from_params(current_user.user_id, owner, status, stage_id, client_type, close_from,
                                        close_to, q, account_id, prospect_id)
    return service.list_opportunities(current_user, flt, stale,
                                      ListQuery.build(sort_by, sort_order, page, page_size, OPPORTUNITY_SORT_FIELDS))


@router.get("/opportunities/board")
def opp_board(owner: Optional[str] = None, client_type: Optional[str] = None, q: Optional[str] = None,
              include_closed: bool = False, service: DealService = Depends(deal_service),
              current_user: Any = Depends(tenant_user)):
    flt = OpportunityFilter.from_params(current_user.user_id, owner=owner, client_type=client_type, q=q)
    return service.board(current_user, flt, include_closed)


@router.post("/opportunities", status_code=201)
def create_opp(payload: OppWrite, service: DealService = Depends(deal_service),
               current_user: Any = Depends(tenant_user)):
    return service.create_opportunity(current_user, payload.model_dump(exclude_unset=True))


@router.get("/opportunities/{opportunity_id}")
def get_opp(opportunity_id: str, service: DealService = Depends(deal_service),
            current_user: Any = Depends(tenant_user)):
    return service.detail(current_user, entity_id(opportunity_id, DEAL_MISSING))


@router.patch("/opportunities/{opportunity_id}")
def update_opp(opportunity_id: str, payload: OppWrite, service: DealService = Depends(deal_service),
               current_user: Any = Depends(tenant_user), if_match: Optional[int] = Depends(if_match_version)):
    """Edit a deal, including board drag-and-drop stage moves (stage_id); send the row_version you
    loaded as expected_version or If-Match (T-3)."""
    return service.update_opportunity(current_user, entity_id(opportunity_id, DEAL_MISSING),
                                      payload.model_dump(exclude_unset=True), if_match)


@router.delete("/opportunities/{opportunity_id}")
def delete_opp(opportunity_id: str, service: DealService = Depends(deal_service),
               current_user: Any = Depends(tenant_user)):
    return service.delete_opportunity(current_user, entity_id(opportunity_id, DEAL_MISSING))


# Deal activity and timeline (BR-SF-06)

@router.post("/opportunities/{opportunity_id}/activities", status_code=201)
def log_deal_activity(opportunity_id: str, payload: DealActivity, service: DealService = Depends(deal_service),
                      current_user: Any = Depends(tenant_user)):
    return service.log_activity(current_user, entity_id(opportunity_id, DEAL_MISSING), payload.model_dump())


@router.get("/opportunities/{opportunity_id}/timeline")
def deal_timeline(opportunity_id: str, service: DealService = Depends(deal_service), db=Depends(get_db),
                  current_user: Any = Depends(tenant_user)):
    """Activities, tasks, emails with the deal's contact, proposals and changes, newest first."""
    return service.timeline(current_user, entity_id(opportunity_id, DEAL_MISSING),
                            lambda task, names: task_dict(db, task, names))


# =============================================================
# Proposals (BR-SP-02)
# =============================================================

@router.get("/proposals")
def list_proposals(status: Optional[str] = None, client_type: Optional[str] = None, owner: Optional[str] = None,
                   opportunity_id: Optional[str] = None, service: DealService = Depends(deal_service),
                   current_user: Any = Depends(tenant_user)):
    """The proposal pipeline: each status with its proposals and total (BR-SP-02, BR-SP-04)."""
    opps = OpportunityFilter.from_params(current_user.user_id, owner=owner, client_type=client_type,
                                         opportunity_id=opportunity_id)
    return service.list_proposals(current_user, ProposalFilter(statuses=split_csv(status), opportunities=opps))


@router.post("/proposals", status_code=201)
def create_proposal(payload: ProposalWrite, service: DealService = Depends(deal_service),
                    current_user: Any = Depends(tenant_user)):
    return service.create_proposal(current_user, payload.model_dump(exclude_unset=True))


@router.patch("/proposals/{proposal_id}")
def update_proposal(proposal_id: str, payload: ProposalWrite, service: DealService = Depends(deal_service),
                    current_user: Any = Depends(tenant_user), if_match: Optional[int] = Depends(if_match_version)):
    return service.update_proposal(current_user, entity_id(proposal_id, PROPOSAL_MISSING),
                                   payload.model_dump(exclude_unset=True), if_match)


@router.delete("/proposals/{proposal_id}")
def delete_proposal(proposal_id: str, service: DealService = Depends(deal_service),
                    current_user: Any = Depends(tenant_user)):
    return service.delete_proposal(current_user, entity_id(proposal_id, PROPOSAL_MISSING))


# =============================================================
# Revenue pipeline (BR-SP-03/04)
# =============================================================

@router.get("/pipeline/revenue")
def revenue_pipeline(owner: Optional[str] = None, client_type: Optional[str] = None,
                     close_from: Optional[date] = None, close_to: Optional[date] = None,
                     service: DealService = Depends(deal_service), current_user: Any = Depends(tenant_user)):
    """Open pipeline by stage, unweighted and weighted, plus won and lost in the same filter.
    Totals are sums of the same opportunities GET /opportunities returns for these filters."""
    flt = OpportunityFilter.from_params(current_user.user_id, owner=owner, client_type=client_type,
                                        close_from=close_from, close_to=close_to)
    return service.revenue(current_user, flt)
