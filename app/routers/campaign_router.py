# app/routers/campaign_router.py
"""
Campaign Management API endpoints.

Thin HTTP layer: parses and validates the request, enforces auth, scopes the campaign to the
caller's tenant, calls a campaign service and shapes the response. Business rules and queries
live in app/services (campaign_service, campaign_query_service, campaign_send_now_service,
campaign_tracking_service, campaign_schedule_service, campaign_template_service).
Service domain errors (app/core/errors.py) are turned into HTTP responses centrally.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.core.errors import BadRequestError
from app.models.campaign import Campaign
from app.models.user import User
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignEnrollmentRequest,
    CampaignFilter,
    CampaignListResponse,
    CampaignResponse,
    CampaignStateChange,
    CampaignStatus,
    CampaignUpdate,
)
from app.schemas.sequence_schema import (
    SequenceResponse,
    SequenceStepCreate,
    SequenceStepResponse,
    SequenceStepUpdate,
)
from app.services.audit_service import AuditService
from app.services.campaign_query_service import CampaignQueryService
from app.services.campaign_schedule_service import CampaignScheduleService
from app.services.campaign_send_now_service import (
    SEND_NOW_BATCH,
    CampaignSendNowService,
    format_cursor as format_send_now_cursor,
    parse_cursor,
)
from app.services.campaign_service import CampaignService
from app.services.campaign_template_service import CampaignTemplateService
from app.services.campaign_tracking_service import CampaignTrackingService
from app.services.daily_limit import enrollment_notice
from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
from app.services.reconnect_service import ProspectRevalidationService
from app.services.sequence_service import SequenceService
from app.utils.error_utils import handle_route_error

logger = logging.getLogger(__name__)

__all__ = ["router", "SEND_NOW_BATCH", "format_send_now_cursor", "parse_send_now_cursor"]

router = APIRouter(prefix="/campaigns", tags=["Campaigns"])

ANY_ROLE = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
MANAGER_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER")
ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
MANAGE_CAMPAIGNS = [Depends(require_permission("manage_campaigns"))]


# =============================
# DEPENDENCIES
# =============================

def get_campaign_service(db: Session = Depends(get_db)) -> CampaignService:
    return CampaignService(db)


def get_audit_service(db: Session = Depends(get_db)) -> AuditService:
    return AuditService(db)


def get_sequence_service(db: Session = Depends(get_db)) -> SequenceService:
    return SequenceService(db)


def get_query_service(db: Session = Depends(get_db)) -> CampaignQueryService:
    return CampaignQueryService(db)


def _get_campaign_for_user(queries: CampaignQueryService, campaign_id: str, current_user: User) -> Campaign:
    """The campaign if it belongs to the caller's tenant; 404 for both missing and cross-tenant."""
    return queries.get_campaign_for_tenant(campaign_id, current_user.tenant_id)


def _state_change(campaign_id: str, old_status: str, campaign: Campaign,
                  reason: Optional[str] = None) -> CampaignStateChange:
    return CampaignStateChange(
        campaign_id=campaign_id,
        previous_status=CampaignStatus(old_status),
        new_status=CampaignStatus(campaign.status),
        reason=reason,
        changed_at=datetime.utcnow(),
    )


# =============================
# CAMPAIGN CRUD ENDPOINTS
# =============================

@router.post("", response_model=CampaignResponse, status_code=201, dependencies=MANAGE_CAMPAIGNS)
async def create_campaign(
    data: CampaignCreate,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Optional[CampaignResponse]:
    """
    Create a new campaign.

    Creates a campaign in DRAFT status. Use `/campaigns/{id}/launch` to activate.
    """
    try:
        campaign = service.create(tenant_id=current_user.tenant_id, user_id=current_user.user_id, data=data)
        db.commit()
        return service.get_with_details(campaign.campaign_id)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="create_campaign")


@router.get("", response_model=CampaignListResponse)
async def list_campaigns(
    status: Optional[CampaignStatus] = Query(None, description="Filter by status"),
    created_by: Optional[str] = Query(None, description="Filter by creator user ID"),
    campaign_name: Optional[str] = Query(None, description="Search by name (partial match)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    service: CampaignService = Depends(get_campaign_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> CampaignListResponse:
    """
    List campaigns with filtering and pagination.

    Supports filtering by:
    - **status**: DRAFT, ACTIVE, PAUSED, COMPLETED
    - **created_by**: User ID
    - **campaign_name**: Partial name match
    """
    try:
        filters = CampaignFilter(
            status=status,
            created_by=created_by,
            campaign_name=campaign_name,
            page=page,
            page_size=page_size,
        )
        return service.list_campaigns(current_user.tenant_id, filters)
    except Exception as e:
        handle_route_error(e, context="list_campaigns")


@router.get("/{campaign_id}", response_model=CampaignResponse)
def get_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> CampaignResponse:
    """
    Get campaign details including metrics.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        campaign = service.get_with_details(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return campaign
    except Exception as e:
        handle_route_error(e, context="get_campaign")


@router.put("/{campaign_id}", response_model=CampaignResponse, dependencies=MANAGE_CAMPAIGNS)
async def update_campaign(
    campaign_id: str,
    data: CampaignUpdate,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Optional[CampaignResponse]:
    """
    Update campaign settings.

    Only updates provided fields. Cannot change status directly - use pause/resume/launch.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        campaign = service.update(campaign_id, current_user.user_id, data)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        db.commit()
        return service.get_with_details(campaign_id)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="update_campaign")


@router.post("/{campaign_id}/sequences", response_model=SequenceStepResponse, status_code=201,
             dependencies=MANAGE_CAMPAIGNS)
async def add_sequence_step(
    campaign_id: str,
    data: SequenceStepCreate,
    seq_service: SequenceService = Depends(get_sequence_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> SequenceStepResponse:
    """
    Add a sequence step to a campaign.

    Maximum 7 steps allowed per campaign.
    """
    _get_campaign_for_user(queries, campaign_id, current_user)
    try:
        step = seq_service.add_step(campaign_id, data)
        db.commit()
        return seq_service.to_response(step)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{campaign_id}/sequences", response_model=SequenceResponse)
async def get_campaign_sequences(
    campaign_id: str,
    seq_service: SequenceService = Depends(get_sequence_service),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> SequenceResponse:
    """
    Get all sequence steps for a campaign.

    Returns steps ordered by step_number with template info if attached.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        steps = seq_service.get_campaign_sequences(campaign_id)
        return SequenceResponse(
            campaign_id=campaign_id,
            steps=[seq_service.to_response(s) for s in steps],
            total_steps=len(steps),
            total_wait_days=seq_service.get_total_wait_days(campaign_id),
        )
    except Exception as e:
        handle_route_error(e, context="get_campaign_sequences")


@router.put("/sequences/{sequence_id}", response_model=SequenceStepResponse, dependencies=MANAGE_CAMPAIGNS)
async def update_sequence_step(
    sequence_id: str,
    data: SequenceStepUpdate,
    seq_service: SequenceService = Depends(get_sequence_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> SequenceStepResponse:
    """
    Update a sequence step.

    Cannot change step_number. To reorder, delete and recreate.
    """
    try:
        queries.require_sequence_step_for_tenant(sequence_id, current_user.tenant_id)
        step = seq_service.update_step(sequence_id, data)
        if not step:
            raise HTTPException(status_code=404, detail="Sequence step not found")
        db.commit()
        return seq_service.to_response(step)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="update_sequence_step")


@router.delete("/sequences/{sequence_id}", status_code=204, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def delete_sequence_step(
    sequence_id: str,
    seq_service: SequenceService = Depends(get_sequence_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> None:
    """
    Delete a sequence step.
    """
    try:
        queries.require_sequence_step_for_tenant(sequence_id, current_user.tenant_id)
        if not seq_service.delete_step(sequence_id):
            raise HTTPException(status_code=404, detail="Sequence step not found")
        db.commit()
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="delete_sequence_step")


def _enrolled_prospect_view(p: Any) -> Dict[str, Any]:
    prospect_id = p.prospect_id if hasattr(p, 'prospect_id') else str(p.id)
    return {
        "id": prospect_id,
        "prospect_id": prospect_id,
        "email": p.email,
        "name": f"{p.first_name or ''} {p.last_name or ''}".strip() or p.email.split('@')[0],
        "first_name": p.first_name,
        "last_name": p.last_name,
        "company": p.company_name,
        "company_name": p.company_name,
        "designation": p.designation,
        "poc_city": p.poc_city,
        "poc_state": p.poc_state,
        "poc_country": getattr(p, 'poc_country', None),
        "consent_status": p.consent_status,
        "notes": getattr(p, 'notes', None),
        "notes_list_id": getattr(p, 'notes_list_id', None),
        "status": p.status if hasattr(p, 'status') else 'ACTIVE',
        "assigned_inbox": getattr(p, 'assigned_inbox', None),
    }


@router.get("/{campaign_id}/prospects", status_code=200, response_model=None)
async def get_enrolled_prospects(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> List[Dict[str, Any]]:
    """
    Get all prospects enrolled in a campaign.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        return [_enrolled_prospect_view(p) for p in service.get_enrolled_prospects(campaign_id)]
    except Exception as e:
        handle_route_error(e, context="get_enrolled_prospects")


@router.post("/{campaign_id}/prospects", status_code=200, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def enroll_prospects(
    campaign_id: str,
    request: CampaignEnrollmentRequest,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Enroll prospects into the campaign.

    Checks for:
    - Global unsubscribe
    - Valid email
    - Consent status (must be OPT_IN)

    Returns the count of newly enrolled prospects and a reason for each one not enrolled.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        count, rejections = service.enroll_prospects_with_report(
            campaign_id=campaign_id,
            user_id=current_user.user_id,
            request=request
        )
        db.commit()
        return {
            "enrolled_count": count,
            "rejected_count": len(rejections),
            "rejected_summary": summarize(rejections),
            "rejected": rejections[:MAX_REJECTIONS_RETURNED],
            "daily_limit_notice": enrollment_notice(db, current_user, count, request.prospect_ids, rejections,
                                                    campaign_id),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{campaign_id}/prospects/{prospect_id}", status_code=200, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def remove_prospect(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, bool]:
    """
    Remove a prospect from the campaign.

    Works after the campaign has been scheduled: cancels any not-yet-sent
    emails for this prospect and stops the sequence for them. Already-sent
    emails are left untouched.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        removed = service.remove_prospect(
            campaign_id=campaign_id,
            prospect_id=prospect_id,
            user_id=current_user.user_id,
        )
        if not removed:
            raise HTTPException(status_code=404, detail="Prospect not enrolled in this campaign")
        db.commit()
        return {"removed": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{campaign_id}/content/stub", status_code=200, response_model=None)
async def generate_stub_content(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, int]:
    """
    Stage 4 Stub: Generate placeholder templates.
    required for Stage 6 validation.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        count = service.generate_stub_content(campaign_id, current_user.user_id)
        db.commit()
        return {"generated_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{campaign_id}", status_code=204, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def delete_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ADMIN_ROLES)),
) -> None:
    """
    Delete a campaign.

    This permanently removes the campaign and all associated data.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        if not service.delete(campaign_id, current_user.user_id):
            raise HTTPException(status_code=404, detail="Campaign not found")
        db.commit()
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="delete_campaign")


# =============================
# STATE MANAGEMENT ENDPOINTS
# =============================

@router.post("/{campaign_id}/launch", response_model=CampaignStateChange, dependencies=MANAGE_CAMPAIGNS)
async def launch_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*MANAGER_ROLES)),
) -> CampaignStateChange:
    """
    Launch a draft campaign.

    Changes status from DRAFT to ACTIVE and starts the email sending process.
    """
    try:
        old_status = _get_campaign_for_user(queries, campaign_id, current_user).status
        campaign = service.launch(campaign_id, current_user.user_id)
        db.commit()
        return _state_change(campaign_id, old_status, campaign)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="launch_campaign")


@router.post("/{campaign_id}/pause", response_model=CampaignStateChange, dependencies=MANAGE_CAMPAIGNS)
async def pause_campaign(
    campaign_id: str,
    reason: Optional[str] = Query(None, description="Reason for pausing"),
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*MANAGER_ROLES)),
) -> CampaignStateChange:
    """
    Pause an active campaign.

    Stops sending emails until resumed. Currently queued emails are not affected.
    """
    try:
        old_status = _get_campaign_for_user(queries, campaign_id, current_user).status
        campaign = service.pause(campaign_id, current_user.user_id, reason)
        db.commit()
        return _state_change(campaign_id, old_status, campaign, reason=reason)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="pause_campaign")


@router.post("/{campaign_id}/resume", response_model=CampaignStateChange, dependencies=MANAGE_CAMPAIGNS)
async def resume_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*MANAGER_ROLES)),
) -> CampaignStateChange:
    """
    Resume a paused campaign.

    Continues sending emails from where it was paused.
    """
    try:
        old_status = _get_campaign_for_user(queries, campaign_id, current_user).status
        campaign = service.resume(campaign_id, current_user.user_id)
        db.commit()
        return _state_change(campaign_id, old_status, campaign)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="resume_campaign")


# =============================
# METRICS & AUDIT ENDPOINTS
# =============================

@router.get("/{campaign_id}/metrics", response_model=None)
async def get_campaign_metrics(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Get real-time campaign metrics.

    Returns: sent_count, opened_count, replied_count, bounced_count, and rates.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        return service.metrics.get_metrics_dict(campaign_id)
    except Exception as e:
        handle_route_error(e, context="get_campaign_metrics")


@router.get("/{campaign_id}/audit", response_model=None)
async def get_campaign_audit_trail(
    campaign_id: str,
    limit: int = Query(20, ge=1, le=100, description="Maximum records"),
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> List[Dict[str, Any]]:
    """
    Get audit trail for a campaign.

    Shows who created, updated, paused, resumed, or launched the campaign.
    """
    try:
        _get_campaign_for_user(queries, campaign_id, current_user)
        return [
            {"log_id": log.log_id, "action": log.action, "user_id": log.user_id, "created_at": log.created_at}
            for log in service.audit.get_campaign_history(campaign_id, limit)
        ]
    except Exception as e:
        handle_route_error(e, context="get_campaign_audit_trail")


# =============================
# SEND NOW
# =============================

def parse_send_now_cursor(cursor: Optional[str]) -> Optional[Tuple[int, str]]:
    """'<step_number>:<message_id>' -> (step, message_id); None/'' -> None; garbage -> HTTP 400."""
    try:
        return parse_cursor(cursor)
    except BadRequestError as e:
        raise HTTPException(status_code=400, detail=e.message) from e


@router.post("/{campaign_id}/send-now", dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def send_campaign_now(
    campaign_id: str,
    limit: int = Query(SEND_NOW_BATCH, ge=1, le=100),
    cursor: Optional[str] = Query(None, max_length=100),
    since: Optional[datetime] = None,
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Immediately send this campaign's queued emails that are due in sequence order,
    at most `limit` per request so a big queue never times out one HTTP request.
    The response says how many remain (`remaining`, `has_more`); to continue the same
    run, call again with the returned `cursor` and `since` (= the first call's
    `started_at`). Within a run, follow-ups that only became sendable because their
    previous step went out during the run are left to the scheduler.
    """
    campaign = _get_campaign_for_user(queries, campaign_id, current_user)
    return await CampaignSendNowService(db).send_now(campaign, limit=limit, cursor=cursor, since=since)


# =============================
# MANUAL EVENT TRACKING
# =============================

@router.post("/{campaign_id}/prospects/{prospect_id}/mark-replied", response_model=None)
async def mark_prospect_replied(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Manually mark a prospect as replied.

    Creates a REPLY event for the most recent sent message to this prospect.
    Updates campaign prospect status to REPLIED.
    """
    _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignTrackingService(db, service.metrics).mark_prospect_replied(
        campaign_id, prospect_id, current_user.user_id)


@router.post("/{campaign_id}/prospects/{prospect_id}/mark-opened", response_model=None)
async def mark_prospect_opened(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Manually mark a prospect as having opened the email.

    Creates an OPEN event for the most recent sent message to this prospect.
    """
    _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignTrackingService(db, service.metrics).mark_prospect_opened(
        campaign_id, prospect_id, current_user.user_id)


@router.post("/{campaign_id}/messages/{message_id}/mark-replied", response_model=None)
async def mark_message_replied(
    campaign_id: str,
    message_id: str,
    db: Session = Depends(get_db),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Mark a specific message as replied.

    Alternative to prospect-based endpoint when you know the exact message ID.
    """
    _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignTrackingService(db).mark_message_replied(campaign_id, message_id, current_user.user_id)


# =============================
# ANALYTICS SCHEMAS
# =============================

class DailyMetrics(BaseModel):
    date: str
    sent: int = 0
    opened: int = 0
    replied: int = 0
    bounced: int = 0
    sender_bounced: int = 0
    positive_replied: int = 0
    ooo: int = 0


class SequenceMetrics(BaseModel):
    step: int
    type: str = "Email"
    sent: int = 0
    opened_count: int = 0
    replied_count: int = 0
    bounced_count: int = 0
    open_rate: float = 0.0
    click_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0
    positive_replied_count: int = 0
    ooo_count: int = 0
    sender_bounced_count: int = 0


class LeadStatsItem(BaseModel):
    name: str
    value: int


class LeadStats(BaseModel):
    total: int
    data: List[LeadStatsItem]


class CampaignAnalyticsResponse(BaseModel):
    campaign_id: str
    bar_data: List[DailyMetrics]
    sequences: List[SequenceMetrics]
    lead_stats: LeadStats
    total_messages_sent: int
    metrics: Dict[str, Any]


class TemplateInfo(BaseModel):
    template_id: str
    sequence_id: Optional[str] = None
    step_number: Optional[int] = None
    designation: Optional[str] = None
    subject: str
    body: str
    tone: Optional[str] = None
    ai_model: Optional[str] = None
    is_ai_generated: bool = False
    is_approved: bool = False
    approved_at: Optional[datetime] = None
    created_at: datetime


class CampaignTemplatesResponse(BaseModel):
    campaign_id: str
    templates: List[TemplateInfo]
    total: int


class ScheduleItem(BaseModel):
    date: str
    status: str
    prospect_count: int
    step_number: int
    prospects: List[dict] = []


class CampaignScheduleResponse(BaseModel):
    campaign_id: str
    schedule: List[ScheduleItem]
    total_scheduled: int
    total_sent: int


class RegenerateRequest(BaseModel):
    # max_length matches email_templates.tone (String(50)): longer values were a 500 (DataError 1406)
    tone: Optional[str] = Field("professional", max_length=50, description="Tone: professional, casual, formal")
    regenerate_subject: bool = True
    regenerate_body: bool = True
    creative_email: bool = False
    custom_instruction: Optional[str] = None


class RegenerateResponse(BaseModel):
    template_id: str
    subject: str
    body: str
    ai_model: str
    regenerated_at: datetime


class ApproveResponse(BaseModel):
    template_id: str
    is_approved: bool
    approved_at: Optional[datetime] = None


class ApproveAllResponse(BaseModel):
    approved_count: int
    template_ids: List[str]


# =============================
# ANALYTICS ENDPOINTS
# =============================

@router.get("/{campaign_id}/analytics", response_model=CampaignAnalyticsResponse)
def get_campaign_analytics(
    campaign_id: str,
    days: int = Query(7, ge=1, le=365, description="Number of days for bar chart"),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> CampaignAnalyticsResponse:
    """
    Get detailed analytics for a campaign.

    Returns daily bar chart data, per-sequence step metrics, and lead status breakdown.
    """
    _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignAnalyticsResponse(**queries.get_analytics(campaign_id, days))


# =============================
# CAMPAIGN TEMPLATES
# =============================

@router.get("/{campaign_id}/templates", response_model=CampaignTemplatesResponse)
async def get_campaign_templates(
    campaign_id: str,
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> CampaignTemplatesResponse:
    """Get all templates for a campaign."""
    _get_campaign_for_user(queries, campaign_id, current_user)
    templates = [TemplateInfo(**t) for t in queries.list_templates(campaign_id)]
    return CampaignTemplatesResponse(campaign_id=campaign_id, templates=templates, total=len(templates))


@router.post("/{campaign_id}/templates/{template_id}/regenerate", response_model=RegenerateResponse)
async def regenerate_template(
    campaign_id: str,
    template_id: str,
    request: RegenerateRequest,
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> RegenerateResponse:
    """Regenerate template content using AI."""
    campaign = _get_campaign_for_user(queries, campaign_id, current_user)
    template = CampaignTemplateService(db).regenerate(
        campaign,
        template_id,
        tone=request.tone,
        regenerate_subject=request.regenerate_subject,
        regenerate_body=request.regenerate_body,
        creative_email=request.creative_email,
        custom_instruction=request.custom_instruction,
    )
    return RegenerateResponse(
        template_id=template.template_id,
        subject=template.subject,
        body=template.body,
        ai_model=template.ai_model or "gpt-4o-mini",
        regenerated_at=template.generated_at,
    )


@router.get("/{campaign_id}/sequences/{step}/emails", response_model=None)
async def get_sequence_step_emails(
    campaign_id: str,
    step: int,
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Get the email template and any related prospect replies for a specific sequence step."""
    _get_campaign_for_user(queries, campaign_id, current_user)
    return queries.get_step_emails(campaign_id, step)


@router.get("/{campaign_id}/schedule", response_model=CampaignScheduleResponse)
def get_campaign_schedule(
    campaign_id: str,
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> CampaignScheduleResponse:
    """Get schedule timeline for a campaign."""
    _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignScheduleResponse(campaign_id=campaign_id, **queries.get_schedule(campaign_id))


# =============================
# SCHEDULE MANAGEMENT
# =============================

class RescheduleRequest(BaseModel):
    """
    Request body for rescheduling a queued/scheduled email message.
    new_scheduled_at should be a UTC datetime string, e.g. '2026-03-15T09:00:00'.
    """
    new_scheduled_at: datetime
    reason: Optional[str] = None


@router.patch("/{campaign_id}/messages/{message_id}/reschedule", response_model=None)
async def reschedule_message(
    campaign_id: str,
    message_id: str,
    request: RescheduleRequest,
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Update the scheduled_at date/time of a QUEUED or SCHEDULED email message.

    - Cannot reschedule messages that are already SENT or FAILED.
    - Accepts a UTC datetime for new_scheduled_at.
    - Returns the updated schedule info.
    """
    _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignScheduleService(db).reschedule_message(
        campaign_id, message_id, request.new_scheduled_at, request.reason)


# =============================
# BULK DATE RESCHEDULE
# =============================

class RescheduleDateRequest(BaseModel):
    """
    Bulk-reschedule all QUEUED messages on a given date + sequence step.
    date            : ISO date string 'YYYY-MM-DD'
    step_number     : sequence step to target
    new_scheduled_at: user's desired UTC datetime (treated as local campaign-TZ wall-clock)
    """
    date: str
    step_number: int
    new_scheduled_at: datetime
    reason: Optional[str] = None


@router.post("/{campaign_id}/schedule/reschedule-date", response_model=None)
async def reschedule_date(
    campaign_id: str,
    request: RescheduleDateRequest,
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Bulk-reschedule all QUEUED/SCHEDULED messages for a campaign that fall on
    the given date AND belong to the given sequence step.

    Rules applied (in campaign timezone):
      - Before window open  → snap to window-open time today
      - Inside window       → keep user's exact time
      - After window close  → push to next business day at window-open
      - Weekend / Holiday   → push to next business day at window-open
    Adds per-message random jitter (0-5 min) so emails don't all queue together.
    """
    campaign = _get_campaign_for_user(queries, campaign_id, current_user)
    return CampaignScheduleService(db).reschedule_date(
        campaign,
        date=request.date,
        step_number=request.step_number,
        new_scheduled_at=request.new_scheduled_at,
        reason=request.reason,
    )


# =============================
# TEMPLATE APPROVAL
# =============================

@router.post("/{campaign_id}/templates/{template_id}/approve", response_model=ApproveResponse)
async def approve_template(
    campaign_id: str,
    template_id: str,
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*MANAGER_ROLES)),
) -> ApproveResponse:
    """Approve a template for use in campaigns."""
    _get_campaign_for_user(queries, campaign_id, current_user)
    template = CampaignTemplateService(db).approve(campaign_id, template_id, current_user.user_id)
    return ApproveResponse(template_id=template.template_id, is_approved=True, approved_at=template.approved_at)


@router.post("/{campaign_id}/templates/approve-all", response_model=ApproveAllResponse)
async def approve_all_templates(
    campaign_id: str,
    queries: CampaignQueryService = Depends(get_query_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*MANAGER_ROLES)),
) -> ApproveAllResponse:
    """Approve all templates for a campaign at once."""
    _get_campaign_for_user(queries, campaign_id, current_user)
    approved_ids = CampaignTemplateService(db).approve_all(campaign_id, current_user.user_id)
    return ApproveAllResponse(approved_count=len(approved_ids), template_ids=approved_ids)


# ======================================================
# RECONNECT EMAILS — Bounce Re-validation
# (ADMIN / OWNER only)
# ======================================================

class RevalidateProspectRequest(BaseModel):
    new_email: str = Field(..., description="The prospect's updated, valid email address")
    reason: str = Field(..., min_length=5, description="Why was the email changed? (audit trail)")


@router.get("/bounced-prospects/revalidation-candidates", response_model=None)
def list_bounced_revalidation_candidates(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ADMIN_ROLES)),
) -> Dict[str, Any]:
    """
    ADMIN-ONLY. List all prospects who hard-bounced and are candidates for
    re-validation (i.e., their email can be updated and suppression cleared).

    Voluntary unsubscribes are NEVER included in this list.
    """
    return {"candidates": ProspectRevalidationService(db).list_bounced_prospects(current_user.tenant_id)}


@router.patch("/prospects/{prospect_id}/revalidate", response_model=None)
def revalidate_bounced_prospect(
    prospect_id: str,
    request: RevalidateProspectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ADMIN_ROLES)),
) -> Any:
    """
    ADMIN-ONLY. Re-validate a bounced prospect by updating their email address.

    This will:
    - Update the prospect's email in the database.
    - Clear the hard-bounce auto-suppression from GlobalUnsubscribes.
    - Reset BOUNCED CampaignProspect rows to RECONNECT_ELIGIBLE.

    STRICTLY PROHIBITED: This endpoint cannot clear voluntary unsubscribes.
    Prospects who unsubscribed have a 180-day minimum lockout that
    cannot be overridden by anyone, including Admins.
    """
    try:
        return ProspectRevalidationService(db).revalidate_prospect(
            tenant_id=current_user.tenant_id,
            prospect_id=prospect_id,
            new_email=request.new_email,
            reason=request.reason,
            performed_by=current_user.user_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
