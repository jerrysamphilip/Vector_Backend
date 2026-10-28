# app/routers/campaign_router.py
"""
Campaign Management API endpoints.

Thin HTTP layer (ADR 0009, chunk A2): parses the request, enforces auth and calls ONE service method
(app/services/campaign_service.py, sequence_service, campaign_query_service, campaign_schedule_service,
campaign_template_service, campaign_tracking_service, campaign_wizard_service, campaign_edit_service),
which scopes the campaign to the caller's tenant and owns the unit of work. Service domain errors
(app/core/errors.py) are turned into HTTP responses centrally; the routes that always mapped any other
error through handle_route_error still do.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.auth import require_permission, require_role
from app.core.concurrency import if_match_version, resolve_expected
from app.core.database import get_db
from app.core.errors import BadRequestError
from app.schemas.campaign_api_schema import (
    ApproveAllResponse,
    ApproveResponse,
    CampaignScheduleResponse,
    CampaignTemplatesResponse,
    DailyMetrics,
    LeadStatsItem,
    RegenerateResponse,
    RescheduleDateRequest,
    RescheduleRequest,
    RevalidateProspectRequest,
    SequenceMetrics,
    TemplateInfo,
)
from app.schemas.campaign_edit_schema import (
    CampaignEditRequest,
    CampaignEditResult,
    EmailPreview,
    EmailPreviewRequest,
    TemplateAssignments,
)
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignDuplicateRequest,
    CampaignDuplicateResponse,
    CampaignEnrollmentRequest,
    CampaignFilter,
    CampaignListResponse,
    CampaignResponse,
    CampaignStateChange,
    CampaignStatus,
    CampaignUpdate,
    CampaignVersionedAction,
    EnrollmentCheckRequest,
)
from app.schemas.sequence_schema import (
    SequenceResponse,
    SequenceStepCreate,
    SequenceStepResponse,
    SequenceStepUpdate,
)
from app.services.campaign_edit_service import CampaignEditService
from app.services.campaign_query_service import CampaignQueryService
from app.services.campaign_schedule_service import CampaignScheduleService
from app.services.campaign_send_now_service import (
    SEND_NOW_BATCH,
    format_cursor as format_send_now_cursor,
    parse_cursor,
)
from app.services.campaign_service import CampaignService
from app.services.campaign_template_service import CampaignTemplateService, Regeneration
from app.services.campaign_tracking_service import CampaignTrackingService
from app.services.campaign_wizard_service import CampaignWizardService
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

def get_campaign_service(db=Depends(get_db)) -> CampaignService:
    return CampaignService.for_session(db)


def get_sequence_service(db=Depends(get_db)) -> SequenceService:
    return SequenceService.for_session(db)


def get_query_service(db=Depends(get_db)) -> CampaignQueryService:
    return CampaignQueryService.for_session(db)


def campaign_edit_service(db=Depends(get_db)) -> CampaignEditService:
    return CampaignEditService.for_session(db)


def campaign_template_service(db=Depends(get_db)) -> CampaignTemplateService:
    return CampaignTemplateService.for_session(db)


def campaign_schedule_service(db=Depends(get_db)) -> CampaignScheduleService:
    return CampaignScheduleService.for_session(db)


def campaign_tracking_service(db=Depends(get_db)) -> CampaignTrackingService:
    return CampaignTrackingService.for_session(db)


def campaign_wizard_service(db=Depends(get_db)) -> CampaignWizardService:
    return CampaignWizardService.for_session(db)


def revalidation_service(db=Depends(get_db)) -> ProspectRevalidationService:
    return ProspectRevalidationService(db)


def _action_version(payload: Optional[CampaignVersionedAction], if_match: Optional[int]) -> Optional[int]:
    return resolve_expected(payload.expected_version if payload else None, if_match)


# =============================
# CAMPAIGN CRUD ENDPOINTS
# =============================

@router.post("", response_model=CampaignResponse, status_code=201, dependencies=MANAGE_CAMPAIGNS)
async def create_campaign(
    data: CampaignCreate,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Optional[CampaignResponse]:
    """
    Create a new campaign.

    Creates a campaign in DRAFT status. Use `/campaigns/{id}/launch` to activate.
    """
    try:
        return service.create_campaign(current_user, data)
    except Exception as e:
        handle_route_error(e, context="create_campaign")
        return None


@router.get("", response_model=CampaignListResponse)
async def list_campaigns(
    status: Optional[CampaignStatus] = Query(None, description="Filter by status"),
    created_by: Optional[str] = Query(None, description="Filter by creator user ID"),
    campaign_name: Optional[str] = Query(None, description="Search by name (partial match)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> CampaignListResponse:
    """
    List campaigns with filtering and pagination.

    Supports filtering by:
    - **status**: DRAFT, ACTIVE, PAUSED, COMPLETED
    - **created_by**: User ID
    - **campaign_name**: Partial name match
    """
    try:
        filters = CampaignFilter(status=status, created_by=created_by, campaign_name=campaign_name, page=page,
                                 page_size=page_size)
        return service.list_for(current_user, filters)
    except Exception as e:
        handle_route_error(e, context="list_campaigns")
        raise


@router.get("/{campaign_id}", response_model=CampaignResponse)
def get_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> CampaignResponse:
    """
    Get campaign details including metrics.
    """
    try:
        return service.details_for(current_user, campaign_id)
    except Exception as e:
        handle_route_error(e, context="get_campaign")
        raise


@router.put("/{campaign_id}", response_model=CampaignResponse, dependencies=MANAGE_CAMPAIGNS)
async def update_campaign(
    campaign_id: str,
    data: CampaignUpdate,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
    if_match: Optional[int] = Depends(if_match_version),
) -> Optional[CampaignResponse]:
    """
    Update campaign settings.

    Only updates provided fields. Cannot change status directly - use pause/resume/launch.
    Send the row_version you loaded as expected_version or If-Match (T-3); stale -> 409.
    """
    try:
        return service.update_campaign(current_user, campaign_id, data, if_match)
    except Exception as e:
        handle_route_error(e, context="update_campaign")
        return None


@router.post("/{campaign_id}/duplicates", response_model=CampaignDuplicateResponse, status_code=201,
             dependencies=MANAGE_CAMPAIGNS)
def duplicate_campaign(
    campaign_id: str,
    data: CampaignDuplicateRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> CampaignDuplicateResponse:
    """
    Duplicate a campaign as a new DRAFT (S-12): settings, steps, every persona's templates, subject
    variants, attachment metadata and automation rules. Optional overrides (name, dates, send window,
    timezone, mailboxes) and contacts to enrol (list_ids / prospect_ids; by default skipping contacts
    already in the source). Returns the copy, what was copied, and personas without templates.
    """
    return service.duplicate_campaign(current_user, campaign_id, data)


@router.post("/{campaign_id}/enrollments/checks", response_model=None, dependencies=MANAGE_CAMPAIGNS)
def check_enrollment(
    campaign_id: str,
    request: EnrollmentCheckRequest,
    wizard: CampaignWizardService = Depends(campaign_wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Enrolment dry run (S-20): who would be enrolled and, for everyone else, the reason, using the same
    rules as the real enrolment of ``path`` (``campaign``: POST /campaigns/{id}/prospects; ``wizard``:
    POST /campaigns/{id}/enrollments). Nothing is written.
    """
    return wizard.check_enrollment_request(current_user, campaign_id, request)


@router.post("/{campaign_id}/sequences", response_model=SequenceStepResponse, status_code=201,
             dependencies=MANAGE_CAMPAIGNS)
async def add_sequence_step(
    campaign_id: str,
    data: SequenceStepCreate,
    seq_service: SequenceService = Depends(get_sequence_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> SequenceStepResponse:
    """
    Add a sequence step to a campaign.

    Maximum 7 steps allowed per campaign.
    """
    return seq_service.add_step_for(current_user, campaign_id, data)


@router.get("/{campaign_id}/sequences", response_model=SequenceResponse)
async def get_campaign_sequences(
    campaign_id: str,
    seq_service: SequenceService = Depends(get_sequence_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> SequenceResponse:
    """
    Get all sequence steps for a campaign.

    Returns steps ordered by step_number with template info if attached.
    """
    try:
        return seq_service.steps_for(current_user, campaign_id)
    except Exception as e:
        handle_route_error(e, context="get_campaign_sequences")
        raise


@router.put("/sequences/{sequence_id}", response_model=SequenceStepResponse, dependencies=MANAGE_CAMPAIGNS)
async def update_sequence_step(
    sequence_id: str,
    data: SequenceStepUpdate,
    seq_service: SequenceService = Depends(get_sequence_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
    if_match: Optional[int] = Depends(if_match_version),
) -> SequenceStepResponse:
    """
    Update a sequence step.

    Cannot change step_number. To reorder, delete and recreate.
    Send the step's row_version as expected_version or If-Match (T-3); stale -> 409.
    """
    try:
        return seq_service.update_step_for(current_user, sequence_id, data, if_match)
    except Exception as e:
        handle_route_error(e, context="update_sequence_step")
        raise


@router.delete("/sequences/{sequence_id}", status_code=204, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def delete_sequence_step(
    sequence_id: str,
    editor: CampaignEditService = Depends(campaign_edit_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> None:
    """
    Delete a sequence step.
    """
    try:
        editor.delete_step(current_user, sequence_id)
    except Exception as e:
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
        "removed": bool(getattr(p, 'removed', False)),
        # Lead-list timing columns (F-12): last_sent_at, delivered_at, bounce_reason, ooo,
        # steps_sent, steps_total, next_email_at, stopped_reason
        **(p.timing.as_dict() if getattr(p, 'timing', None) is not None else {}),
    }


@router.get("/{campaign_id}/prospects", status_code=200, response_model=None)
async def get_enrolled_prospects(
    campaign_id: str,
    include_removed: bool = Query(False, description="Also list contacts removed from the campaign"),
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> List[Dict[str, Any]]:
    """
    Get all prospects enrolled in a campaign.
    """
    try:
        return [_enrolled_prospect_view(p) for p in service.enrolled_for(current_user, campaign_id, include_removed)]
    except Exception as e:
        handle_route_error(e, context="get_enrolled_prospects")
        raise


@router.post("/{campaign_id}/prospects", status_code=200, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def enroll_prospects(
    campaign_id: str,
    request: CampaignEnrollmentRequest,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Enroll prospects into the campaign.

    Checks for:
    - Global unsubscribe
    - Valid email
    - Consent status (must be OPT_IN)

    Returns the count of newly enrolled prospects and a reason for each one not enrolled.
    """
    return service.enroll(current_user, campaign_id, request)


@router.delete("/{campaign_id}/prospects/{prospect_id}", status_code=200, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def remove_prospect(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, bool]:
    """
    Remove a prospect from the campaign.

    Works after the campaign has been scheduled: cancels any not-yet-sent
    emails for this prospect and stops the sequence for them. Already-sent
    emails are left untouched.
    """
    return service.remove_contact(current_user, campaign_id, prospect_id)


@router.post("/{campaign_id}/content/stub", status_code=200, response_model=None)
async def generate_stub_content(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, int]:
    """
    Stage 4 Stub: Generate placeholder templates.
    required for Stage 6 validation.
    """
    return service.stub_content(current_user, campaign_id)


@router.delete("/{campaign_id}", status_code=204, dependencies=MANAGE_CAMPAIGNS, response_model=None)
async def delete_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ADMIN_ROLES)),
) -> None:
    """
    Delete a campaign.

    This permanently removes the campaign and all associated data.
    """
    try:
        service.delete_campaign(current_user, campaign_id)
    except Exception as e:
        handle_route_error(e, context="delete_campaign")


# =============================
# EDIT AFTER LAUNCH (F-04)
# =============================

@router.post("/{campaign_id}/edits", response_model=CampaignEditResult, dependencies=MANAGE_CAMPAIGNS)
def edit_campaign(
    campaign_id: str,
    data: CampaignEditRequest,
    editor: CampaignEditService = Depends(campaign_edit_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> Dict[str, Any]:
    """
    Edit a DRAFT, ACTIVE or PAUSED campaign: settings, mailboxes and the step list with each step's
    persona versions. Unsent emails (queued / scheduled / paused) follow: content edits re-render them,
    schedule edits re-plan them in step order, a new step is sent to the contacts who have not passed it,
    mailbox changes re-bind them. A step that was already sent can't be deleted (409); the start date is
    locked after launch (400). Send the campaign's row_version as expected_version or If-Match (stale -> 409).
    """
    return editor.edit(current_user, campaign_id, data.to_domain(), if_match)


@router.post("/{campaign_id}/edits/checks", response_model=CampaignEditResult, dependencies=MANAGE_CAMPAIGNS)
def check_campaign_edit(
    campaign_id: str,
    data: CampaignEditRequest,
    editor: CampaignEditService = Depends(campaign_edit_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> Dict[str, Any]:
    """Dry run of POST /campaigns/{id}/edits: the same checks and counts ("N unsent emails will be updated"),
    nothing is saved."""
    return editor.edit(current_user, campaign_id, data.to_domain(), if_match, dry_run=True)


@router.post("/{campaign_id}/email-previews", response_model=EmailPreview)
def preview_step_email(
    campaign_id: str,
    data: EmailPreviewRequest,
    editor: CampaignEditService = Depends(campaign_edit_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    One step email exactly as it will be sent to one enrolled contact: the scheduled email when it exists
    (else the persona template launch picks), rendered for the contact, with the sender's postal address,
    unsubscribe line and sign-off added as at send time. ``template_id`` previews another persona version;
    ``subject`` / ``body`` preview unsaved editor content. Nothing is written.
    """
    return editor.preview(current_user, campaign_id, data.prospect_id, data.step_number,
                          template_id=data.template_id, subject=data.subject, body=data.body)


@router.get("/{campaign_id}/template-assignments", response_model=TemplateAssignments)
def get_template_assignments(
    campaign_id: str,
    limit: int = Query(200, ge=1, le=2000, description="Contacts per page"),
    offset: int = Query(0, ge=0, description="Contacts to skip"),
    editor: CampaignEditService = Depends(campaign_edit_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Which persona template each enrolled contact gets per step (F-05), with each version's recipients."""
    return editor.template_assignments(current_user, campaign_id, limit=limit, offset=offset)


# =============================
# STATE MANAGEMENT ENDPOINTS
# =============================

@router.post("/{campaign_id}/launch", response_model=CampaignStateChange, dependencies=MANAGE_CAMPAIGNS)
async def launch_campaign(
    campaign_id: str,
    payload: Optional[CampaignVersionedAction] = None,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> CampaignStateChange:
    """
    Launch a draft campaign.

    Changes status from DRAFT to ACTIVE and starts the email sending process.
    """
    try:
        return service.launch_campaign(current_user, campaign_id, _action_version(payload, if_match))
    except Exception as e:
        handle_route_error(e, context="launch_campaign")
        raise


@router.post("/{campaign_id}/pause", response_model=CampaignStateChange, dependencies=MANAGE_CAMPAIGNS)
async def pause_campaign(
    campaign_id: str,
    reason: Optional[str] = Query(None, description="Reason for pausing"),
    payload: Optional[CampaignVersionedAction] = None,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> CampaignStateChange:
    """
    Pause an active campaign.

    Stops sending emails until resumed. Currently queued emails are not affected.
    """
    try:
        return service.pause_campaign(current_user, campaign_id, reason, _action_version(payload, if_match))
    except Exception as e:
        handle_route_error(e, context="pause_campaign")
        raise


@router.post("/{campaign_id}/resume", response_model=CampaignStateChange, dependencies=MANAGE_CAMPAIGNS)
async def resume_campaign(
    campaign_id: str,
    payload: Optional[CampaignVersionedAction] = None,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> CampaignStateChange:
    """
    Resume a paused campaign.

    Continues sending emails from where it was paused.
    """
    try:
        return service.resume_campaign(current_user, campaign_id, _action_version(payload, if_match))
    except Exception as e:
        handle_route_error(e, context="resume_campaign")
        raise


# =============================
# METRICS & AUDIT ENDPOINTS
# =============================

@router.get("/{campaign_id}/metrics", response_model=None)
async def get_campaign_metrics(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Get real-time campaign metrics.

    Returns: sent_count, opened_count, replied_count, bounced_count, and rates.
    """
    try:
        return service.metrics_for(current_user, campaign_id)
    except Exception as e:
        handle_route_error(e, context="get_campaign_metrics")
        raise


@router.get("/{campaign_id}/audit", response_model=None)
async def get_campaign_audit_trail(
    campaign_id: str,
    limit: int = Query(20, ge=1, le=100, description="Maximum records"),
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> List[Dict[str, Any]]:
    """
    Get audit trail for a campaign.

    Shows who created, updated, paused, resumed, or launched the campaign.
    """
    try:
        return service.audit_trail(current_user, campaign_id, limit)
    except Exception as e:
        handle_route_error(e, context="get_campaign_audit_trail")
        raise


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
    service: CampaignService = Depends(get_campaign_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Immediately send this campaign's queued emails that are due in sequence order,
    at most `limit` per request so a big queue never times out one HTTP request.
    The response says how many remain (`remaining`, `has_more`); to continue the same
    run, call again with the returned `cursor` and `since` (= the first call's
    `started_at`). Within a run, follow-ups that only became sendable because their
    previous step went out during the run are left to the scheduler.
    """
    return await service.send_now(current_user, campaign_id, limit=limit, cursor=cursor, since=since)


# =============================
# MANUAL EVENT TRACKING
# =============================

@router.post("/{campaign_id}/prospects/{prospect_id}/mark-replied", response_model=None)
async def mark_prospect_replied(
    campaign_id: str,
    prospect_id: str,
    tracking: CampaignTrackingService = Depends(campaign_tracking_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Manually mark a prospect as replied.

    Creates a REPLY event for the most recent sent message to this prospect.
    Updates campaign prospect status to REPLIED.
    """
    return tracking.mark_prospect_replied_for(current_user, campaign_id, prospect_id)


@router.post("/{campaign_id}/prospects/{prospect_id}/mark-opened", response_model=None)
async def mark_prospect_opened(
    campaign_id: str,
    prospect_id: str,
    tracking: CampaignTrackingService = Depends(campaign_tracking_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Manually mark a prospect as having opened the email.

    Creates an OPEN event for the most recent sent message to this prospect.
    """
    return tracking.mark_prospect_opened_for(current_user, campaign_id, prospect_id)


@router.post("/{campaign_id}/messages/{message_id}/mark-replied", response_model=None)
async def mark_message_replied(
    campaign_id: str,
    message_id: str,
    tracking: CampaignTrackingService = Depends(campaign_tracking_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Mark a specific message as replied.

    Alternative to prospect-based endpoint when you know the exact message ID.
    """
    return tracking.mark_message_replied_for(current_user, campaign_id, message_id)


# =============================
# ANALYTICS SCHEMAS
# =============================

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


class RegenerateRequest(BaseModel):
    # max_length matches email_templates.tone (String(50)): longer values were a 500 (DataError 1406)
    tone: Optional[str] = Field("professional", max_length=50, description="Tone: professional, casual, formal")
    regenerate_subject: bool = True
    regenerate_body: bool = True
    creative_email: bool = False
    custom_instruction: Optional[str] = None


# =============================
# ANALYTICS ENDPOINTS
# =============================

@router.get("/{campaign_id}/analytics", response_model=CampaignAnalyticsResponse)
def get_campaign_analytics(
    campaign_id: str,
    days: int = Query(7, ge=1, le=365, description="Number of days for bar chart"),
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> CampaignAnalyticsResponse:
    """
    Get detailed analytics for a campaign.

    Returns daily bar chart data, per-sequence step metrics, and lead status breakdown.
    """
    return CampaignAnalyticsResponse(**queries.analytics_for(current_user, campaign_id, days))


# =============================
# CAMPAIGN TEMPLATES
# =============================

@router.get("/{campaign_id}/templates", response_model=CampaignTemplatesResponse)
async def get_campaign_templates(
    campaign_id: str,
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> CampaignTemplatesResponse:
    """Get all templates for a campaign."""
    templates = [TemplateInfo(**t) for t in queries.templates_for(current_user, campaign_id)]
    return CampaignTemplatesResponse(campaign_id=campaign_id, templates=templates, total=len(templates))


@router.post("/{campaign_id}/templates/{template_id}/regenerate", response_model=RegenerateResponse)
async def regenerate_template(
    campaign_id: str,
    template_id: str,
    request: RegenerateRequest,
    templates: CampaignTemplateService = Depends(campaign_template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> RegenerateResponse:
    """Regenerate template content using AI."""
    template = templates.regenerate_for(current_user, campaign_id, template_id, Regeneration(
        tone=request.tone, regenerate_subject=request.regenerate_subject, regenerate_body=request.regenerate_body,
        creative_email=request.creative_email, custom_instruction=request.custom_instruction))
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
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Get the email template and any related prospect replies for a specific sequence step."""
    return queries.step_emails_for(current_user, campaign_id, step)


@router.get("/{campaign_id}/schedule", response_model=CampaignScheduleResponse)
def get_campaign_schedule(
    campaign_id: str,
    queries: CampaignQueryService = Depends(get_query_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> CampaignScheduleResponse:
    """Get schedule timeline for a campaign."""
    return CampaignScheduleResponse(campaign_id=campaign_id, **queries.schedule_for(current_user, campaign_id))


# =============================
# SCHEDULE MANAGEMENT
# =============================

@router.patch("/{campaign_id}/messages/{message_id}/reschedule", response_model=None)
async def reschedule_message(
    campaign_id: str,
    message_id: str,
    request: RescheduleRequest,
    schedule: CampaignScheduleService = Depends(campaign_schedule_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Update the scheduled_at date/time of a QUEUED or SCHEDULED email message.

    - Cannot reschedule messages that are already SENT or FAILED.
    - Accepts a UTC datetime for new_scheduled_at.
    - Returns the updated schedule info.
    """
    return schedule.reschedule_message_for(current_user, campaign_id, message_id, request.new_scheduled_at,
                                           request.reason)


# =============================
# BULK DATE RESCHEDULE
# =============================

@router.post("/{campaign_id}/schedule/reschedule-date", response_model=None)
async def reschedule_date(
    campaign_id: str,
    request: RescheduleDateRequest,
    schedule: CampaignScheduleService = Depends(campaign_schedule_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
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
    return schedule.reschedule_date_for(current_user, campaign_id, date=request.date, step_number=request.step_number,
                                        new_scheduled_at=request.new_scheduled_at, reason=request.reason)


# =============================
# TEMPLATE APPROVAL
# =============================

@router.post("/{campaign_id}/templates/{template_id}/approve", response_model=ApproveResponse)
async def approve_template(
    campaign_id: str,
    template_id: str,
    templates: CampaignTemplateService = Depends(campaign_template_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
) -> ApproveResponse:
    """Approve a template for use in campaigns."""
    template = templates.approve_for(current_user, campaign_id, template_id)
    return ApproveResponse(template_id=template.template_id, is_approved=True, approved_at=template.approved_at)


@router.post("/{campaign_id}/templates/approve-all", response_model=ApproveAllResponse)
async def approve_all_templates(
    campaign_id: str,
    templates: CampaignTemplateService = Depends(campaign_template_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
) -> ApproveAllResponse:
    """Approve all templates for a campaign at once."""
    approved_ids = templates.approve_all_for(current_user, campaign_id)
    return ApproveAllResponse(approved_count=len(approved_ids), template_ids=approved_ids)


# ======================================================
# RECONNECT EMAILS — Bounce Re-validation
# (ADMIN / OWNER only)
# ======================================================

@router.get("/bounced-prospects/revalidation-candidates", response_model=None)
def list_bounced_revalidation_candidates(
    revalidation: ProspectRevalidationService = Depends(revalidation_service),
    current_user: Any = Depends(require_role(*ADMIN_ROLES)),
) -> Dict[str, Any]:
    """
    ADMIN-ONLY. List all prospects who hard-bounced and are candidates for
    re-validation (i.e., their email can be updated and suppression cleared).

    Voluntary unsubscribes are NEVER included in this list.
    """
    return {"candidates": revalidation.list_bounced_prospects(current_user.tenant_id)}


@router.patch("/prospects/{prospect_id}/revalidate", response_model=None)
def revalidate_bounced_prospect(
    prospect_id: str,
    request: RevalidateProspectRequest,
    revalidation: ProspectRevalidationService = Depends(revalidation_service),
    current_user: Any = Depends(require_role(*ADMIN_ROLES)),
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
        return revalidation.revalidate_prospect(
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
