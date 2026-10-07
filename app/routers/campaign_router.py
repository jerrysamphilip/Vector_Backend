# app/routers/campaign_router.py
"""
Campaign Management API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pydantic import BaseModel, Field

from app.core.auth import require_role, require_permission
from app.core.database import get_db
from app.models.user import User
from app.models.email_template import EmailTemplate
from app.models.email_sequence import EmailSequence
from app.models.email_message import EmailMessage, EmailEvent
from app.models.campaign import Campaign, CampaignProspect
from app.models.prospect import Prospect
from app.models.persona_blueprint import PersonaBlueprint
from app.services.ai_email_service import generate_email_with_llm, classify_prospect
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignUpdate,
    CampaignFilter,
    CampaignStatus,
    CampaignResponse,
    CampaignListResponse,
    CampaignStateChange,
    CampaignEnrollmentRequest,
)
from app.schemas.sequence_schema import (
    SequenceStepCreate,
    SequenceStepUpdate,
    SequenceStepResponse,
    SequenceResponse,
)
from app.services.campaign_service import CampaignService
from app.services.sequence_service import SequenceService
from app.services.audit_service import AuditService
from app.utils.email_utils import normalize_unsubscribe_footer
from app.utils.email_utils import normalize_cta_link, has_effective_cta_link
from app.utils.error_utils import handle_route_error
from app.utils.email_utils import finalize_email_body
from app.utils.campaign_prospect_status import set_prospect_status
import logging

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/campaigns", tags=["Campaigns"])


# =============================
# DEPENDENCIES
# =============================

def get_campaign_service(db: Session = Depends(get_db)) -> CampaignService:
    return CampaignService(db)


def get_audit_service(db: Session = Depends(get_db)) -> AuditService:
    return AuditService(db)


def get_sequence_service(db: Session = Depends(get_db)) -> SequenceService:
    return SequenceService(db)


# =============================
# AUTH (real auth via JWT)
# =============================
def _get_campaign_for_user(
    service: CampaignService,
    campaign_id: str,
    current_user: User,
) -> Campaign:
    """
    Load campaign and ensure it belongs to the authenticated user's tenant.
    Return 404 for both not-found and cross-tenant access.
    """
    campaign = service.get(campaign_id)
    if not campaign or campaign.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


# =============================
# CAMPAIGN CRUD ENDPOINTS
# =============================

@router.post("", response_model=CampaignResponse, status_code=201, dependencies=[Depends(require_permission("manage_campaigns"))])
async def create_campaign(
    data: CampaignCreate,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Create a new campaign.

    Creates a campaign in DRAFT status. Use `/campaigns/{id}/launch` to activate.
    """
    try:
        campaign = service.create(
            tenant_id=current_user.tenant_id,
            user_id=current_user.user_id,
            data=data,
        )
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
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
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
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get campaign details including metrics.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        campaign = service.get_with_details(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return campaign
    except Exception as e:
        handle_route_error(e, context="get_campaign")


@router.put("/{campaign_id}", response_model=CampaignResponse, dependencies=[Depends(require_permission("manage_campaigns"))])
async def update_campaign(
    campaign_id: str,
    data: CampaignUpdate,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Update campaign settings.

    Only updates provided fields. Cannot change status directly - use pause/resume/launch.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        campaign = service.update(campaign_id, current_user.user_id, data)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        db.commit()
        return service.get_with_details(campaign_id)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="update_campaign")


@router.post("/{campaign_id}/sequences", response_model=SequenceStepResponse, status_code=201, dependencies=[Depends(require_permission("manage_campaigns"))])
async def add_sequence_step(
    campaign_id: str,
    data: SequenceStepCreate,
    seq_service: SequenceService = Depends(get_sequence_service),
    camp_service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Add a sequence step to a campaign.

    Maximum 7 steps allowed per campaign.
    """
    _get_campaign_for_user(camp_service, campaign_id, current_user)

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
    camp_service: CampaignService = Depends(get_campaign_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get all sequence steps for a campaign.

    Returns steps ordered by step_number with template info if attached.
    """
    try:
        _get_campaign_for_user(camp_service, campaign_id, current_user)
        steps = seq_service.get_campaign_sequences(campaign_id)
        return SequenceResponse(
            campaign_id=campaign_id,
            steps=[seq_service.to_response(s) for s in steps],
            total_steps=len(steps),
            total_wait_days=seq_service.get_total_wait_days(campaign_id),
        )
    except Exception as e:
        handle_route_error(e, context="get_campaign_sequences")


@router.put("/sequences/{sequence_id}", response_model=SequenceStepResponse, dependencies=[Depends(require_permission("manage_campaigns"))])
async def update_sequence_step(
# def update_sequence_step(
    sequence_id: str,
    data: SequenceStepUpdate,
    seq_service: SequenceService = Depends(get_sequence_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Update a sequence step.

    Cannot change step_number. To reorder, delete and recreate.
    """
    try:
        scoped_step = (
            db.query(EmailSequence)
            .join(Campaign, Campaign.campaign_id == EmailSequence.campaign_id)
            .filter(
                EmailSequence.sequence_id == sequence_id,
                Campaign.tenant_id == current_user.tenant_id,
            )
            .first()
        )
        if not scoped_step:
            raise HTTPException(status_code=404, detail="Sequence step not found")
        step = seq_service.update_step(sequence_id, data)
        if not step:
            raise HTTPException(status_code=404, detail="Sequence step not found")
        db.commit()
        return seq_service.to_response(step)
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="update_sequence_step")


@router.delete("/sequences/{sequence_id}", status_code=204, dependencies=[Depends(require_permission("manage_campaigns"))])
async def delete_sequence_step(
    sequence_id: str,
    seq_service: SequenceService = Depends(get_sequence_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Delete a sequence step.
    """
    try:
        scoped_step = (
            db.query(EmailSequence)
            .join(Campaign, Campaign.campaign_id == EmailSequence.campaign_id)
            .filter(
                EmailSequence.sequence_id == sequence_id,
                Campaign.tenant_id == current_user.tenant_id,
            )
            .first()
        )
        if not scoped_step:
            raise HTTPException(status_code=404, detail="Sequence step not found")
        deleted = seq_service.delete_step(sequence_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Sequence step not found")
        db.commit()
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="delete_sequence_step")


@router.get("/{campaign_id}/prospects", status_code=200)
async def get_enrolled_prospects(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get all prospects enrolled in a campaign.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        prospects = service.get_enrolled_prospects(campaign_id)
        return [
            {
                "id": p.prospect_id if hasattr(p, 'prospect_id') else str(p.id),
                "prospect_id": p.prospect_id if hasattr(p, 'prospect_id') else str(p.id),
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
            for p in prospects
        ]
    except Exception as e:
        handle_route_error(e, context="get_enrolled_prospects")


@router.post("/{campaign_id}/prospects", status_code=200, dependencies=[Depends(require_permission("manage_campaigns"))])
async def enroll_prospects(
    campaign_id: str,
    request: CampaignEnrollmentRequest,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Enroll prospects into the campaign.

    Checks for:
    - Global unsubscribe
    - Valid email
    - Consent status (must be OPT_IN)

    Returns the count of newly enrolled prospects and a reason for each one not enrolled.
    """
    from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
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
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{campaign_id}/prospects/{prospect_id}", status_code=200, dependencies=[Depends(require_permission("manage_campaigns"))])
async def remove_prospect(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Remove a prospect from the campaign.

    Works after the campaign has been scheduled: cancels any not-yet-sent
    emails for this prospect and stops the sequence for them. Already-sent
    emails are left untouched.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        removed = service.remove_prospect(
            campaign_id=campaign_id,
            prospect_id=prospect_id,
            user_id=current_user.user_id,
        )
        if not removed:
            raise HTTPException(status_code=404, detail="Prospect not enrolled in this campaign")
        db.commit()
        return {"removed": True}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{campaign_id}/content/stub", status_code=200)
async def generate_stub_content(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Stage 4 Stub: Generate placeholder templates.
    required for Stage 6 validation.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        count = service.generate_stub_content(campaign_id, current_user.user_id)
        db.commit()
        return {"generated_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{campaign_id}", status_code=204, dependencies=[Depends(require_permission("manage_campaigns"))])
async def delete_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """
    Delete a campaign.
    
    This permanently removes the campaign and all associated data.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        deleted = service.delete(campaign_id, current_user.user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Campaign not found")
        db.commit()
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="delete_campaign")


# =============================
# STATE MANAGEMENT ENDPOINTS
# =============================

@router.post("/{campaign_id}/launch", response_model=CampaignStateChange, dependencies=[Depends(require_permission("manage_campaigns"))])
async def launch_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Launch a draft campaign.

    Changes status from DRAFT to ACTIVE and starts the email sending process.
    """
    try:
        campaign = _get_campaign_for_user(service, campaign_id, current_user)
        old_status = campaign.status
        campaign = service.launch(campaign_id, current_user.user_id)
        db.commit()
        return CampaignStateChange(
            campaign_id=campaign_id,
            previous_status=CampaignStatus(old_status),
            new_status=CampaignStatus(campaign.status),
            changed_at=datetime.utcnow(),
        )
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="launch_campaign")


@router.post("/{campaign_id}/pause", response_model=CampaignStateChange, dependencies=[Depends(require_permission("manage_campaigns"))])
async def pause_campaign(
    campaign_id: str,
    reason: Optional[str] = Query(None, description="Reason for pausing"),
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Pause an active campaign.

    Stops sending emails until resumed. Currently queued emails are not affected.
    """
    try:
        campaign = _get_campaign_for_user(service, campaign_id, current_user)
        old_status = campaign.status
        campaign = service.pause(campaign_id, current_user.user_id, reason)
        db.commit()
        return CampaignStateChange(
            campaign_id=campaign_id,
            previous_status=CampaignStatus(old_status),
            new_status=CampaignStatus(campaign.status),
            reason=reason,
            changed_at=datetime.utcnow(),
        )
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="pause_campaign")


@router.post("/{campaign_id}/resume", response_model=CampaignStateChange, dependencies=[Depends(require_permission("manage_campaigns"))])
async def resume_campaign(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Resume a paused campaign.

    Continues sending emails from where it was paused.
    """
    try:
        campaign = _get_campaign_for_user(service, campaign_id, current_user)
        old_status = campaign.status
        campaign = service.resume(campaign_id, current_user.user_id)
        db.commit()
        return CampaignStateChange(
            campaign_id=campaign_id,
            previous_status=CampaignStatus(old_status),
            new_status=CampaignStatus(campaign.status),
            changed_at=datetime.utcnow(),
        )
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="resume_campaign")


# =============================
# METRICS & AUDIT ENDPOINTS
# =============================

@router.get("/{campaign_id}/metrics")
async def get_campaign_metrics(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get real-time campaign metrics.
    
    Returns: sent_count, opened_count, replied_count, bounced_count, and rates.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        return service.metrics.get_metrics_dict(campaign_id)
    except Exception as e:
        handle_route_error(e, context="get_campaign_metrics")


@router.get("/{campaign_id}/audit")
async def get_campaign_audit_trail(
    campaign_id: str,
    limit: int = Query(20, ge=1, le=100, description="Maximum records"),
    service: CampaignService = Depends(get_campaign_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get audit trail for a campaign.
    
    Shows who created, updated, paused, resumed, or launched the campaign.
    """
    try:
        _get_campaign_for_user(service, campaign_id, current_user)
        logs = service.audit.get_campaign_history(campaign_id, limit)
        return [
            {
                "log_id": log.log_id,
                "action": log.action,
                "user_id": log.user_id,
                "created_at": log.created_at,
            }
            for log in logs
        ]
    except Exception as e:
        handle_route_error(e, context="get_campaign_audit_trail")


@router.post("/{campaign_id}/send-now", dependencies=[Depends(require_permission("manage_campaigns"))])
async def send_campaign_now(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Immediately send FIRST STEP emails only for this campaign.
    Only sends Step 1 (first email) for each prospect.
    """
    from app.models import EmailMessage, EmailTemplate, Prospect, EmailEvent, EmailSequence, Campaign
    from app.services.email_sender_service import email_sender, TransientEmailFailure, PermanentEmailFailure
    from app.services.email_scheduler_service import email_scheduler
    from app.models.sending_inbox import SendingInbox
    
    campaign = _get_campaign_for_user(service, campaign_id, current_user)
    
    if campaign.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Campaign must be ACTIVE to send emails")
    
    # Get all queued/scheduled emails for this campaign, ordered by sequence step
    queued_emails = db.query(EmailMessage).join(EmailSequence).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.status.in_(["QUEUED", "SCHEDULED"])
    ).order_by(EmailSequence.step_number).all()
    
    if not queued_emails:
        return {"status": "no_emails", "message": "No queued emails to send", "sent": 0, "failed": 0}
    
    sent_count = 0
    failed_count = 0
    errors = []
    
    import asyncio

    # Get sender name from campaign creator
    from app.models.user import User
    
    sender_name = None
    campaign_data = db.query(Campaign, User).join(
        User, Campaign.created_by == User.user_id
    ).filter(
        Campaign.campaign_id == campaign_id
    ).first()
    
    if campaign_data:
        campaign, user = campaign_data
        sender_name = campaign.sender_name or f"{user.first_name} {user.last_name}"
    
    for email_msg in queued_emails:
        try:
            # Atomic Check-and-Set: Update status to SENDING only if it is QUEUED/SCHEDULED.
            # This prevents race conditions with the background scheduler.
            rows_updated = db.query(EmailMessage).filter(
                EmailMessage.message_id == email_msg.message_id,
                EmailMessage.status.in_(["QUEUED", "SCHEDULED"])
            ).update({"status": "SENDING"}, synchronize_session=False)
            
            db.commit() # Commit the lock acquisition immediately
            
            if rows_updated == 0:
                logger.info(f"[SendNow] Skipping email {email_msg.message_id}, not in QUEUED state (likely picked by scheduler).")
                continue

            # Refresh object after commit (it's expired)
            # Fetch fresh template and prospect data
            template = db.query(EmailTemplate).filter(
                EmailTemplate.template_id == email_msg.template_id
            ).first()
            
            prospect = db.query(Prospect).filter(
                Prospect.prospect_id == email_msg.prospect_id
            ).first()
            
            if not template or not prospect:
                # Revert to FAILED
                email_msg.status = "FAILED"
                email_msg.failure_reason = "Template or prospect not found"
                db.add(email_msg)
                db.commit()
                failed_count += 1
                continue

            from_email_address = email_scheduler._resolve_sender_for_message(
                email_msg=email_msg,
                campaign=campaign,
                db=db,
            )
            
            # Send via SES
            result = await email_sender.send_with_retry(
                email_message=email_msg,
                email_template=template,
                prospect=prospect,
                max_retries=1,  # Only 1 retry for immediate send
                sender_name=sender_name,
                from_email_address=from_email_address,
            )
            
            if result["success"]:
                email_msg.status = "SENT"
                email_msg.sent_at = datetime.utcnow()
                email_msg.provider_message_id = result.get("ses_message_id")
                email_msg.from_email = from_email_address

                if email_msg.inbox_id:
                    sent_inbox = db.query(SendingInbox).filter(
                        SendingInbox.inbox_id == email_msg.inbox_id
                    ).first()
                    if sent_inbox:
                        sent_inbox.emails_sent_today = (sent_inbox.emails_sent_today or 0) + 1
                        sent_inbox.last_sent_at = datetime.utcnow()
                
                # Create sent event
                event = EmailEvent(
                    message_id=email_msg.message_id,
                    event_type=EmailEvent.EVENT_SENT,
                    event_time=datetime.utcnow()
                )
                db.add(event)
                sent_count += 1

                # ── Advance CampaignProspect state (mirrors execution_service.py) ──
                # Find the sequence step this email belongs to
                from app.models.email_sequence import EmailSequence
                seq_step = db.query(EmailSequence).filter(
                    EmailSequence.sequence_id == email_msg.sequence_id
                ).first()

                if seq_step:
                    cp = db.query(CampaignProspect).filter(
                        CampaignProspect.campaign_id == email_msg.campaign_id,
                        CampaignProspect.prospect_id == email_msg.prospect_id
                    ).first()

                    if cp and cp.status == "ACTIVE":
                        next_step_number = seq_step.step_number + 1
                        next_step = db.query(EmailSequence).filter(
                            EmailSequence.campaign_id == email_msg.campaign_id,
                            EmailSequence.step_number == next_step_number
                        ).first()

                        if next_step:
                            # More steps remain — advance to next step
                            cp.current_step = next_step_number
                            cp.next_scheduled_at = datetime.utcnow() + timedelta(days=next_step.wait_days)
                        else:
                            # No more steps — prospect has completed the sequence
                            cp.status = "COMPLETED"
                            cp.next_scheduled_at = None
            else:
                email_msg.status = "FAILED"
                email_msg.failure_reason = result.get("error", "Unknown error")
                failed_count += 1
                errors.append(f"{prospect.email}: {result.get('error')}")

            db.commit() # Commit the result of sending + prospect state advance
                
        except Exception as e:
            logger.error(f"[SendNow] Error processing email {email_msg.message_id}: {e}")
            # Try to fail the email if possible
            try:
                email_msg.status = "FAILED"
                email_msg.failure_reason = str(e)[:200]
                db.add(email_msg)
                db.commit()
            except:
                db.rollback()
            
            failed_count += 1
            errors.append(str(e)[:100])
    
    return {
        "status": "completed",
        "sent": sent_count,
        "failed": failed_count,
        "total": len(queued_emails),
        "errors": errors[:5] if errors else []  # Return first 5 errors
    }


# =============================
# MANUAL EVENT TRACKING
# =============================

@router.post("/{campaign_id}/prospects/{prospect_id}/mark-replied")
async def mark_prospect_replied(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Manually mark a prospect as replied.

    Creates a REPLY event for the most recent sent message to this prospect.
    Updates campaign prospect status to REPLIED.
    """
    from app.models import EmailMessage, EmailEvent, CampaignProspect
    
    # Verify campaign exists
    _get_campaign_for_user(service, campaign_id, current_user)
    
    # Find the most recent SENT message for this prospect in this campaign
    message = db.query(EmailMessage).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.prospect_id == prospect_id,
        EmailMessage.status == "SENT"
    ).order_by(EmailMessage.sent_at.desc()).first()
    
    if not message:
        raise HTTPException(status_code=404, detail="No sent message found for this prospect")
    
    # Check if already marked as replied
    existing_reply = db.query(EmailEvent).filter(
        EmailEvent.message_id == message.message_id,
        EmailEvent.event_type == EmailEvent.EVENT_REPLY
    ).first()
    
    if existing_reply:
        return {
            "status": "already_marked",
            "message_id": message.message_id,
            "replied_at": existing_reply.event_time
        }
    
    # Create reply event
    event = EmailEvent(
        message_id=message.message_id,
        event_type=EmailEvent.EVENT_REPLY,
        event_time=datetime.utcnow(),
        event_metadata={"source": "manual", "marked_by": current_user.user_id}
    )
    db.add(event)
    
    # Update campaign prospect status
    campaign_prospect = db.query(CampaignProspect).filter(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.prospect_id == prospect_id
    ).first()
    if campaign_prospect:
        set_prospect_status(campaign_prospect, "REPLIED")

    # Update metrics
    service.metrics.process_event(campaign_id, EmailEvent.EVENT_REPLY)
    
    db.commit()
    
    logger.info(f"[Reply] Manually marked prospect {prospect_id} as replied in campaign {campaign_id}")
    
    return {
        "status": "success",
        "message_id": message.message_id,
        "prospect_id": prospect_id,
        "replied_at": event.event_time
    }


@router.post("/{campaign_id}/prospects/{prospect_id}/mark-opened")
async def mark_prospect_opened(
    campaign_id: str,
    prospect_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Manually mark a prospect as having opened the email.
    
    Creates an OPEN event for the most recent sent message to this prospect.
    """
    from app.models import EmailMessage, EmailEvent
    
    # Verify campaign exists
    _get_campaign_for_user(service, campaign_id, current_user)
    
    # Find the most recent SENT message for this prospect
    message = db.query(EmailMessage).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.prospect_id == prospect_id,
        EmailMessage.status == "SENT"
    ).order_by(EmailMessage.sent_at.desc()).first()
    
    if not message:
        raise HTTPException(status_code=404, detail="No sent message found for this prospect")
    
    # Create open event (allow multiple opens)
    event = EmailEvent(
        message_id=message.message_id,
        event_type=EmailEvent.EVENT_OPEN,
        event_time=datetime.utcnow(),
        event_metadata={"source": "manual", "marked_by": current_user.user_id}
    )
    db.add(event)
    
    # Update prospect status to OPENED if it was ACTIVE
    from app.models.campaign import CampaignProspect
    cp = db.query(CampaignProspect).filter(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.prospect_id == prospect_id
    ).first()
    if cp:
        set_prospect_status(cp, "OPENED")
    
    # Update metrics
    service.metrics.process_event(campaign_id, EmailEvent.EVENT_OPEN)
    
    db.commit()
    
    logger.info(f"[Open] Manually marked prospect {prospect_id} as opened in campaign {campaign_id}")
    
    return {
        "status": "success",
        "message_id": message.message_id,
        "prospect_id": prospect_id,
        "opened_at": event.event_time
    }


@router.post("/{campaign_id}/messages/{message_id}/mark-replied")
async def mark_message_replied(
    campaign_id: str,
    message_id: str,
    db: Session = Depends(get_db),
    service: CampaignService = Depends(get_campaign_service),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Mark a specific message as replied.
    
    Alternative to prospect-based endpoint when you know the exact message ID.
    """
    from app.models import EmailMessage, EmailEvent, CampaignProspect
    
    _get_campaign_for_user(service, campaign_id, current_user)

    # Find the message
    message = db.query(EmailMessage).filter(
        EmailMessage.message_id == message_id,
        EmailMessage.campaign_id == campaign_id
    ).first()
    
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    
    # Check if already replied
    existing = db.query(EmailEvent).filter(
        EmailEvent.message_id == message_id,
        EmailEvent.event_type == EmailEvent.EVENT_REPLY
    ).first()
    
    if existing:
        return {"status": "already_marked", "replied_at": existing.event_time}
    
    # Create reply event
    event = EmailEvent(
        message_id=message_id,
        event_type=EmailEvent.EVENT_REPLY,
        event_time=datetime.utcnow(),
        event_metadata={"source": "manual", "marked_by": current_user.user_id}
    )
    db.add(event)
    
    # Update prospect status
    cp = db.query(CampaignProspect).filter(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.prospect_id == message.prospect_id
    ).first()
    if cp:
        set_prospect_status(cp, "REPLIED")

    db.commit()

    return {"status": "success", "message_id": message_id, "replied_at": event.event_time}


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
    tone: Optional[str] = Field("professional", description="Tone: professional, casual, formal")
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
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get detailed analytics for a campaign.

    Returns daily bar chart data, per-sequence step metrics, and lead status breakdown.
    """
    _get_campaign_for_user(service, campaign_id, current_user)

    # 1. Build daily metrics for bar chart
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days - 1)

    sent_results = db.query(
        func.date(EmailMessage.sent_at).label('date'),
        func.count(EmailMessage.message_id).label('count')
    ).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.sent_at >= start_date,
        EmailMessage.status == "SENT"
    ).group_by(func.date(EmailMessage.sent_at)).all()

    sent_map = {row.date: row.count for row in sent_results}

    event_results = db.query(
        func.date(EmailEvent.event_time).label('date'),
        EmailEvent.event_type.label('type'),
        func.count(EmailEvent.event_id).label('count')
    ).join(
        EmailMessage, EmailMessage.message_id == EmailEvent.message_id
    ).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailEvent.event_time >= start_date,
        EmailEvent.event_type.in_([
            EmailEvent.EVENT_OPEN, 
            EmailEvent.EVENT_REPLY,
            EmailEvent.EVENT_BOUNCE,
            EmailEvent.EVENT_SENDER_BOUNCE,
            EmailEvent.EVENT_POSITIVE_REPLY,
            EmailEvent.EVENT_REPLY_OOO
        ])
    ).group_by(func.date(EmailEvent.event_time), EmailEvent.event_type).all()

    open_map = {}
    reply_map = {}
    bounce_map = {}
    sender_bounce_map = {}
    positive_map = {}
    ooo_map = {}

    for row in event_results:
        if row.type == EmailEvent.EVENT_OPEN:
            open_map[row.date] = row.count
        elif row.type == EmailEvent.EVENT_REPLY:
            reply_map[row.date] = row.count
        elif row.type == EmailEvent.EVENT_BOUNCE:
            bounce_map[row.date] = row.count
        elif row.type == EmailEvent.EVENT_SENDER_BOUNCE:
            sender_bounce_map[row.date] = row.count
        elif row.type == EmailEvent.EVENT_POSITIVE_REPLY:
            positive_map[row.date] = row.count
        elif row.type == EmailEvent.EVENT_REPLY_OOO:
            ooo_map[row.date] = row.count

    bar_data = []
    current_date = start_date
    while current_date <= end_date:
        d_sent = sent_map.get(current_date, 0)
        d_opened = open_map.get(current_date, 0)
        d_replied = reply_map.get(current_date, 0)

        if current_date == end_date:
            opened_status_count = db.query(func.count(CampaignProspect.id)).filter(
                CampaignProspect.campaign_id == campaign_id,
                CampaignProspect.status.in_(["OPENED", "REPLIED"])
            ).scalar() or 0
            d_opened = max(d_opened, opened_status_count)

            replied_status_count = db.query(func.count(CampaignProspect.id)).filter(
                CampaignProspect.campaign_id == campaign_id,
                CampaignProspect.status == "REPLIED"
            ).scalar() or 0
            d_replied = max(d_replied, replied_status_count)

        bar_data.append(DailyMetrics(
            date=current_date.isoformat(),
            sent=d_sent,
            opened=d_opened,
            replied=d_replied,
            bounced=bounce_map.get(current_date, 0),
            sender_bounced=sender_bounce_map.get(current_date, 0),
            positive_replied=positive_map.get(current_date, 0),
            ooo=ooo_map.get(current_date, 0)
        ))
        current_date += timedelta(days=1)

    # 2. Per-sequence step metrics
    sequences = db.query(EmailSequence).filter(
        EmailSequence.campaign_id == campaign_id
    ).order_by(EmailSequence.step_number).all()

    sequence_ids = [s.sequence_id for s in sequences]

    sent_counts = db.query(
        EmailMessage.sequence_id,
        func.count(EmailMessage.message_id)
    ).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.status == "SENT",
        EmailMessage.sequence_id.in_(sequence_ids)
    ).group_by(EmailMessage.sequence_id).all()

    seq_sent_map = {row[0]: row[1] for row in sent_counts}

    event_types = [
        EmailEvent.EVENT_OPEN,
        EmailEvent.EVENT_CLICK,
        EmailEvent.EVENT_REPLY,
        EmailEvent.EVENT_BOUNCE,
        EmailEvent.EVENT_SENDER_BOUNCE,
        EmailEvent.EVENT_POSITIVE_REPLY,
        EmailEvent.EVENT_REPLY_OOO
    ]

    event_counts = db.query(
        EmailMessage.sequence_id,
        EmailEvent.event_type,
        func.count(func.distinct(EmailEvent.message_id))
    ).join(
        EmailEvent, EmailEvent.message_id == EmailMessage.message_id
    ).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.sequence_id.in_(sequence_ids),
        EmailEvent.event_type.in_(event_types)
    ).group_by(
        EmailMessage.sequence_id, EmailEvent.event_type
    ).all()

    event_map = {}
    for seq_id, e_type, count in event_counts:
        if seq_id not in event_map:
            event_map[seq_id] = {}
        event_map[seq_id][e_type] = count

    sequences_data = []

    # Orphaned sent messages (manual/other) are ignored here to prevent "Email 0 - Manual/Other"
    # from showing up as an independent sequence step, as requested.

    for seq in sequences:
        s_id = seq.sequence_id
        sent_count = seq_sent_map.get(s_id, 0)

        e_counts = event_map.get(s_id, {})
        opened_count = e_counts.get(EmailEvent.EVENT_OPEN, 0)
        clicked_count = e_counts.get(EmailEvent.EVENT_CLICK, 0)
        replied_count = e_counts.get(EmailEvent.EVENT_REPLY, 0)
        bounced_count = e_counts.get(EmailEvent.EVENT_BOUNCE, 0)
        sender_bounced_count = e_counts.get(EmailEvent.EVENT_SENDER_BOUNCE, 0)
        positive_replied_count = e_counts.get(EmailEvent.EVENT_POSITIVE_REPLY, 0)
        ooo_count = e_counts.get(EmailEvent.EVENT_REPLY_OOO, 0)

        if seq.step_number == 1:
            replied_status_count = db.query(func.count(CampaignProspect.id)).filter(
                CampaignProspect.campaign_id == campaign_id,
                CampaignProspect.status == "REPLIED"
            ).scalar() or 0
            replied_count = max(replied_count, replied_status_count)

        open_rate = (opened_count / sent_count * 100) if sent_count > 0 else 0.0
        click_rate = (clicked_count / opened_count * 100) if opened_count > 0 else 0.0
        reply_rate = (replied_count / sent_count * 100) if sent_count > 0 else 0.0
        bounce_rate = (bounced_count / sent_count * 100) if sent_count > 0 else 0.0

        step_type = "Initial Email" if seq.step_number == 1 else f"Follow-up {seq.step_number - 1}"

        sequences_data.append(SequenceMetrics(
            step=seq.step_number,
            type=step_type,
            sent=sent_count,
            opened_count=opened_count,
            replied_count=replied_count,
            bounced_count=bounced_count,
            open_rate=round(open_rate, 2),
            click_rate=round(click_rate, 2),
            reply_rate=round(reply_rate, 2),
            bounce_rate=round(bounce_rate, 2),
            positive_replied_count=positive_replied_count,
            ooo_count=ooo_count,
            sender_bounced_count=sender_bounced_count
        ))

    sequences_data.sort(key=lambda x: (x.step if x.step > 0 else 99))

    # 3. Lead stats
    total_prospects = db.query(func.count(CampaignProspect.id)).filter(
        CampaignProspect.campaign_id == campaign_id
    ).scalar() or 0

    statuses = ["ACTIVE", "REPLIED", "BOUNCED", "UNSUBSCRIBED", "COMPLETED"]
    stats_data = []

    # Optimize: Single query with GROUP BY instead of 5 separate queries
    status_counts = db.query(
        CampaignProspect.status, 
        func.count(CampaignProspect.id)
    ).filter(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.status.in_(statuses)
    ).group_by(CampaignProspect.status).all()
    
    status_map = {row[0]: row[1] for row in status_counts}

    for status in statuses:
        count = status_map.get(status, 0)
        if count > 0:
            stats_data.append(LeadStatsItem(name=status.title(), value=count))

    known_count = sum(item.value for item in stats_data)
    if known_count < total_prospects:
        stats_data.append(LeadStatsItem(name="Other", value=total_prospects - known_count))

    lead_stats = LeadStats(total=total_prospects, data=stats_data)

    # Lifetime aggregate metrics
    total_sent = db.query(func.count(EmailMessage.message_id)).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.status == "SENT"
    ).scalar() or 0

    all_msg_ids = db.query(EmailMessage.message_id).filter(
        EmailMessage.campaign_id == campaign_id
    )

    total_opened = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_OPEN
    ).scalar() or 0

    opened_status_count = db.query(func.count(CampaignProspect.id)).filter(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.status.in_(["OPENED", "REPLIED"])
    ).scalar() or 0
    total_opened = max(total_opened, opened_status_count)

    total_replied_events = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_REPLY
    ).scalar() or 0

    replied_status_count = db.query(func.count(CampaignProspect.id)).filter(
        CampaignProspect.campaign_id == campaign_id,
        CampaignProspect.status == "REPLIED"
    ).scalar() or 0
    total_replied = max(total_replied_events, replied_status_count)

    total_bounced = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_BOUNCE
    ).scalar() or 0

    total_clicked = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_CLICK
    ).scalar() or 0

    total_unsubscribed = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_UNSUBSCRIBE
    ).scalar() or 0

    total_positive = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_POSITIVE_REPLY
    ).scalar() or 0

    total_ooo = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_REPLY_OOO
    ).scalar() or 0

    total_sender_bounced = db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
        EmailEvent.message_id.in_(all_msg_ids),
        EmailEvent.event_type == EmailEvent.EVENT_SENDER_BOUNCE
    ).scalar() or 0

    metrics = {
        "sent_count": total_sent,
        "opened_count": total_opened,
        "clicked_count": total_clicked,
        "replied_count": total_replied,
        "positive_replied_count": total_positive,
        "ooo_count": total_ooo,
        "bounced_count": total_bounced,
        "sender_bounced_count": total_sender_bounced,
        "unsubscribed_count": total_unsubscribed,
        "open_rate": round((total_opened / total_sent * 100), 2) if total_sent > 0 else 0.0,
        "click_rate": round((total_clicked / total_opened * 100), 2) if total_opened > 0 else 0.0,
        "reply_rate": round((total_replied / total_sent * 100), 2) if total_sent > 0 else 0.0,
        "bounce_rate": round((total_bounced / total_sent * 100), 2) if total_sent > 0 else 0.0,
    }

    return CampaignAnalyticsResponse(
        campaign_id=campaign_id,
        bar_data=bar_data,
        sequences=sequences_data,
        lead_stats=lead_stats,
        total_messages_sent=total_sent,
        metrics=metrics
    )


# =============================
# CAMPAIGN TEMPLATES
# =============================

@router.get("/{campaign_id}/templates", response_model=CampaignTemplatesResponse)
async def get_campaign_templates(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get all templates for a campaign."""
    _get_campaign_for_user(service, campaign_id, current_user)

    templates = db.query(EmailTemplate).filter(
        EmailTemplate.campaign_id == campaign_id
    ).all()

    sequences = {s.sequence_id: s.step_number for s in db.query(EmailSequence).filter(
        EmailSequence.campaign_id == campaign_id
    ).all()}

    template_list = []
    for tmpl in templates:
        template_list.append(TemplateInfo(
            template_id=tmpl.template_id,
            sequence_id=tmpl.sequence_id,
            step_number=sequences.get(tmpl.sequence_id),
            designation=tmpl.designation,
            subject=tmpl.subject,
            body=tmpl.body,
            tone=tmpl.tone,
            ai_model=tmpl.ai_model,
            is_ai_generated=tmpl.is_ai_generated or False,
            is_approved=tmpl.approved_by is not None,
            approved_at=tmpl.approved_at,
            created_at=tmpl.created_at
        ))

    template_list.sort(key=lambda x: (x.step_number or 0, x.designation or ""))

    return CampaignTemplatesResponse(
        campaign_id=campaign_id,
        templates=template_list,
        total=len(template_list)
    )


@router.post("/{campaign_id}/templates/{template_id}/regenerate", response_model=RegenerateResponse)
async def regenerate_template(
    campaign_id: str,
    template_id: str,
    request: RegenerateRequest,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Regenerate template content using AI."""
    campaign = _get_campaign_for_user(service, campaign_id, current_user)

    template = db.query(EmailTemplate).filter(
        EmailTemplate.template_id == template_id,
        EmailTemplate.campaign_id == campaign_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    step_number = 1
    if template.sequence_id:
        seq = db.query(EmailSequence).filter(
            EmailSequence.sequence_id == template.sequence_id
        ).first()
        if seq:
            step_number = seq.step_number

    designation = template.designation or "General"

    try:
        persona_type, _ = classify_prospect(designation)

        blueprint = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == persona_type
        ).first()

        if not blueprint:
            blueprint = db.query(PersonaBlueprint).filter(
                PersonaBlueprint.persona_type == "OTHER"
            ).first()

        if not blueprint:
            raise HTTPException(status_code=500, detail="No persona blueprint found")

        prospect_data = {
            "first_name": "{{first_name}}",
            "last_name": "{{last_name}}",
            "designation": designation,
            "company_name": "{{company_name}}",
            "industry": "{{industry}}",
            "city": "{{city}}",
            "state": "{{state}}",
            "linkedin_url": "{{linkedin_url}}",
        }

        normalized_cta_link = normalize_cta_link(campaign.cta_link)

        result = generate_email_with_llm(
            prospect_data=prospect_data,
            blueprint=blueprint,
            product_name=campaign.campaign_name,
            product_description=(campaign.campaign_description or f"Email campaign: {campaign.campaign_name}"),
            cta_link=normalized_cta_link,
            creative_email=request.creative_email,
            custom_instruction=request.custom_instruction,
        )

        if not result:
            fallback_cta_line = ""
            if has_effective_cta_link(normalized_cta_link):
                fallback_cta_line = f"\n\nWould you be open to a quick call?: {normalized_cta_link}"
            result = {
                "subject": f"[Step {step_number}] {designation} Outreach",
                "body": f"Hi {{{{first_name}}}},\n\nI noticed your role as {designation} and wanted to reach out.{fallback_cta_line}\n\nRegards,\n{{{{signature_block}}}}",
                "model_used": "fallback"
            }

        if request.regenerate_subject:
            template.subject = result.get("subject", template.subject)
        if request.regenerate_body:
            # template.body = normalize_unsubscribe_footer(result.get("body", template.body))
            template.body = finalize_email_body(result.get("body", template.body))

        template.tone = request.tone
        template.ai_model = result.get("model_used", "gpt-4o-mini")
        template.is_ai_generated = True
        template.generated_at = datetime.utcnow()

        template.approved_by = None
        template.approved_at = None

        db.commit()

        return RegenerateResponse(
            template_id=template.template_id,
            subject=template.subject,
            body=template.body,
            ai_model=template.ai_model or "gpt-4o-mini",
            regenerated_at=template.generated_at
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")


@router.get("/{campaign_id}/sequences/{step}/emails")
async def get_sequence_step_emails(
    campaign_id: str,
    step: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT"))
):
    """Get the email template and any related prospect replies for a specific sequence step."""
    # Find the sequence
    seq = db.query(EmailSequence).filter(
        EmailSequence.campaign_id == campaign_id,
        EmailSequence.step_number == step
    ).first()

    if not seq:
        raise HTTPException(status_code=404, detail="Sequence not found")

    # Find the corresponding template
    template = db.query(EmailTemplate).filter(
        EmailTemplate.campaign_id == campaign_id,
        EmailTemplate.sequence_id == seq.sequence_id
    ).first()

    # Find replies to this step
    # We find inbound emails that belong to the same conversation as a sent message from this sequence step
    # Simplified query: Get EmailMessages with INBOUND direction that are replies to OUTBOUND messages of this sequence.
    sent_messages_sq = db.query(EmailMessage.conversation_id).filter(
        EmailMessage.sequence_id == seq.sequence_id,
        EmailMessage.direction == "OUTBOUND",
        EmailMessage.conversation_id.isnot(None)
    )

    replies = db.query(EmailMessage).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.direction == "INBOUND",
        EmailMessage.conversation_id.in_(sent_messages_sq)
    ).order_by(EmailMessage.sent_at.desc()).limit(15).all()

    replies_data = [{
        "from_email": r.from_email,
        "subject": r.subject or "No Subject",
        "body_text": r.body_text or "",
        "received_at": r.sent_at.isoformat() if r.sent_at else None
    } for r in replies]

    return {
        "step": step,
        "template": {
            "subject": template.subject if template else "No subject",
            "body": template.body if template else "No body content"
        },
        "recent_replies": replies_data
    }


@router.get("/{campaign_id}/schedule", response_model=CampaignScheduleResponse)
def get_campaign_schedule(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get schedule timeline for a campaign."""
    _get_campaign_for_user(service, campaign_id, current_user)

    messages = db.query(
        EmailMessage.message_id,
        EmailMessage.scheduled_at,
        EmailMessage.sent_at,
        EmailMessage.status,
        EmailMessage.sequence_id,
        EmailMessage.prospect_id,
        Prospect.first_name,
        Prospect.last_name,
        Prospect.email
    ).join(
        Prospect, Prospect.prospect_id == EmailMessage.prospect_id
    ).filter(
        EmailMessage.campaign_id == campaign_id
    ).all()

    sequences = {s.sequence_id: s.step_number for s in db.query(EmailSequence).filter(
        EmailSequence.campaign_id == campaign_id
    ).all()}

    schedule_map = {}
    total_sent = 0
    total_scheduled = 0

    for msg in messages:
        date_val = msg.sent_at or msg.scheduled_at
        if not date_val:
            continue

        date_str = date_val.date().isoformat()
        status = "SENT" if msg.status == "SENT" else "QUEUED"
        step = sequences.get(msg.sequence_id, 1)

        key = f"{date_str}_{step}_{status}"

        if key not in schedule_map:
            schedule_map[key] = {
                "date": date_str,
                "status": status,
                "step_number": step,
                "prospect_count": 0,
                "prospects": [],
                "seen_prospect_ids": set(),
            }

        # De-duplicate same prospect in same day/step/status bucket.
        if msg.prospect_id in schedule_map[key]["seen_prospect_ids"]:
            continue
        schedule_map[key]["seen_prospect_ids"].add(msg.prospect_id)
        schedule_map[key]["prospect_count"] += 1

        if len(schedule_map[key]["prospects"]) < 5:
            schedule_map[key]["prospects"].append({
                "name": f"{msg.first_name or ''} {msg.last_name or ''}".strip(),
                "email": msg.email,
                "message_id": msg.message_id,
                # Real per-message timestamps so the UI can confirm exactly
                # whether/when THIS prospect's email went out, instead of only
                # the day-level bucket it's grouped under.
                "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
                "scheduled_at": msg.scheduled_at.isoformat() if msg.scheduled_at else None,
                "message_status": msg.status,
            })

        if status == "SENT":
            total_sent += 1
        else:
            total_scheduled += 1

    schedule = [
        ScheduleItem(
            date=v["date"],
            status=v["status"],
            prospect_count=v["prospect_count"],
            step_number=v["step_number"],
            prospects=v["prospects"]
        )
        for v in schedule_map.values()
    ]
    schedule.sort(key=lambda x: (x.date, x.step_number))

    return CampaignScheduleResponse(
        campaign_id=campaign_id,
        schedule=schedule,
        total_scheduled=total_scheduled,
        total_sent=total_sent
    )


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


@router.patch("/{campaign_id}/messages/{message_id}/reschedule")
async def reschedule_message(
    campaign_id: str,
    message_id: str,
    request: RescheduleRequest,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Update the scheduled_at date/time of a QUEUED or SCHEDULED email message.

    - Cannot reschedule messages that are already SENT or FAILED.
    - Accepts a UTC datetime for new_scheduled_at.
    - Returns the updated schedule info.
    """
    from app.models.email_message import EmailMessage

    # Verify campaign exists
    _get_campaign_for_user(service, campaign_id, current_user)

    # Find the message
    message = db.query(EmailMessage).filter(
        EmailMessage.message_id == message_id,
        EmailMessage.campaign_id == campaign_id
    ).first()

    if not message:
        raise HTTPException(status_code=404, detail="Email message not found")

    # Reject already-sent or failed messages
    if message.status in ("SENT", "FAILED"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reschedule a message with status '{message.status}'"
        )

    old_scheduled_at = message.scheduled_at
    message.scheduled_at = request.new_scheduled_at

    db.commit()

    logger.info(
        f"[Reschedule] Message {message_id} rescheduled from {old_scheduled_at} "
        f"to {request.new_scheduled_at} in campaign {campaign_id}. "
        f"Reason: {request.reason or 'none'}"
    )

    return {
        "status": "rescheduled",
        "message_id": message_id,
        "campaign_id": campaign_id,
        "old_scheduled_at": old_scheduled_at.isoformat() if old_scheduled_at else None,
        "new_scheduled_at": request.new_scheduled_at.isoformat(),
        "reason": request.reason,
    }



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


@router.post("/{campaign_id}/schedule/reschedule-date")
async def reschedule_date(
    campaign_id: str,
    request: RescheduleDateRequest,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
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
    from app.models.email_message import EmailMessage
    from app.utils.business_calendar import (
        is_business_day,
        get_next_business_day,
    )
    from datetime import time as time_type, date as date_type
    from zoneinfo import ZoneInfo
    import random

    campaign = _get_campaign_for_user(service, campaign_id, current_user)

    # Parse the target date
    try:
        target_date = date_type.fromisoformat(request.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    # ── Campaign timezone + send-window ───────────────────────────────────────
    tz_str    = campaign.campaign_timezone or "UTC"
    win_start = campaign.send_window_start  or time_type(9, 0)   # default 09:00
    win_end   = campaign.send_window_end    or time_type(17, 0)  # default 17:00

    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")

    # ── Convert requested UTC time → campaign local ───────────────────────────
    requested_utc = request.new_scheduled_at
    if requested_utc.tzinfo is None:
        requested_utc = requested_utc.replace(tzinfo=ZoneInfo("UTC"))

    local_requested  = requested_utc.astimezone(tz)
    local_time_only  = local_requested.time()
    is_biz           = is_business_day(local_requested)

    # ── Snap to a valid send slot ─────────────────────────────────────────────
    if is_biz and win_start <= local_time_only <= win_end:
        # Already valid — keep user's exact minute
        snapped_local = local_requested.replace(second=0, microsecond=0)
        skip_reason   = None
    elif is_biz and local_time_only < win_start:
        # Too early today — snap to window open today
        snapped_local = local_requested.replace(
            hour=win_start.hour, minute=win_start.minute, second=0, microsecond=0
        )
        skip_reason = "before window"
    else:
        # After window close on a biz day → start search from tomorrow
        # Weekend / holiday → start search from today (get_next_business_day skips further)
        if is_biz and local_time_only > win_end:
            start_search = local_requested + timedelta(days=1)
        else:
            start_search = local_requested
        snapped_local, skip_reason = get_next_business_day(start_search)
        snapped_local = snapped_local.replace(
            hour=win_start.hour, minute=win_start.minute, second=0, microsecond=0
        )
        if skip_reason:
            logger.info(f"[RescheduleDate] Time adjusted: {skip_reason}")

    # Convert back to naive UTC for storage
    snapped_utc_base = snapped_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    adjusted = snapped_utc_base != request.new_scheduled_at.replace(tzinfo=None)

    # ── Resolve sequence IDs for this step ────────────────────────────────────
    step_sequence_ids = [
        s.sequence_id
        for s in db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == request.step_number
        ).all()
    ]
    if not step_sequence_ids:
        raise HTTPException(
            status_code=404,
            detail=f"No sequence found for step {request.step_number} in this campaign."
        )

    # ── Fetch matching QUEUED/PAUSED messages on that date ───────────────────────────
    messages = db.query(EmailMessage).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.sequence_id.in_(step_sequence_ids),
        EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]),
        func.date(EmailMessage.scheduled_at) == target_date
    ).all()

    if not messages:
        return {
            "status": "no_messages",
            "message": f"No queued messages found for date {request.date} and step {request.step_number}.",
            "updated_count": 0,
        }

    # ── Apply snapped time + per-message jitter ───────────────────────────────
    updated_ids = []
    for msg in messages:
        jitter_min = random.randint(0, 5)
        jitter_sec = random.randint(0, 59)
        base_min   = snapped_utc_base.minute + jitter_min
        final_time = snapped_utc_base.replace(
            hour=(snapped_utc_base.hour + base_min // 60) % 24,
            minute=base_min % 60,
            second=jitter_sec,
        )
        msg.scheduled_at = final_time
        updated_ids.append(msg.message_id)

    db.commit()

    logger.info(
        f"[RescheduleDate] {len(updated_ids)} messages rescheduled to "
        f"~{snapped_utc_base} UTC (tz={tz_str}, window={win_start}-{win_end}) "
        f"for campaign {campaign_id}, date={request.date}, step={request.step_number}. "
        f"Time adjusted={adjusted}."
    )

    # Build human-readable local time for the response
    month_name = snapped_local.strftime('%B')
    day_num    = str(snapped_local.day)           # cross-platform, no %-d
    hour_num   = str(snapped_local.hour % 12 or 12)  # cross-platform, no %-I
    am_pm      = 'AM' if snapped_local.hour < 12 else 'PM'
    minute_str = snapped_local.strftime('%M')
    weekday    = snapped_local.strftime('%A')

    return {
        "status": "rescheduled",
        "campaign_id": campaign_id,
        "date": request.date,
        "step_number": request.step_number,
        "requested_at": request.new_scheduled_at.isoformat(),
        "snapped_utc": snapped_utc_base.isoformat(),
        "snapped_local": snapped_local.strftime("%Y-%m-%d %H:%M") + f" {tz_str}",
        "time_adjusted": adjusted,
        "adjustment_reason": (
            f"Requested time was outside the safe sending window or fell on a weekend/holiday. "
            f"Emails rescheduled to {weekday}, {month_name} {day_num} at "
            f"{hour_num}:{minute_str} {am_pm} ({tz_str})."
        ) if adjusted else None,
        "timezone": tz_str,
        "send_window": f"{win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}",
        "updated_count": len(updated_ids),
        "updated_message_ids": updated_ids,
        "reason": request.reason,
    }


# =============================
# TEMPLATE APPROVAL
# =============================

@router.post("/{campaign_id}/templates/{template_id}/approve", response_model=ApproveResponse)
async def approve_template(
    campaign_id: str,
    template_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Approve a template for use in campaigns."""
    _get_campaign_for_user(service, campaign_id, current_user)

    template = db.query(EmailTemplate).filter(
        EmailTemplate.template_id == template_id,
        EmailTemplate.campaign_id == campaign_id
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    template.approved_by = current_user.user_id
    template.approved_at = datetime.utcnow()

    db.commit()

    return ApproveResponse(
        template_id=template.template_id,
        is_approved=True,
        approved_at=template.approved_at
    )


@router.post("/{campaign_id}/templates/approve-all", response_model=ApproveAllResponse)
async def approve_all_templates(
    campaign_id: str,
    service: CampaignService = Depends(get_campaign_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Approve all templates for a campaign at once."""
    _get_campaign_for_user(service, campaign_id, current_user)

    templates = db.query(EmailTemplate).filter(
        EmailTemplate.campaign_id == campaign_id,
        EmailTemplate.approved_by == None
    ).all()

    approved_ids = []
    for template in templates:
        template.approved_by = current_user.user_id
        template.approved_at = datetime.utcnow()
        approved_ids.append(template.template_id)

    db.commit()

    return ApproveAllResponse(
        approved_count=len(approved_ids),
        template_ids=approved_ids
    )


# ======================================================
# RECONNECT EMAILS — Bounce Re-validation
# (ADMIN / OWNER only)
# ======================================================

class RevalidateProspectRequest(BaseModel):
    new_email: str = Field(..., description="The prospect's updated, valid email address")
    reason: str = Field(..., min_length=5, description="Why was the email changed? (audit trail)")


@router.get("/bounced-prospects/revalidation-candidates")
def list_bounced_revalidation_candidates(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
):
    """
    ADMIN-ONLY. List all prospects who hard-bounced and are candidates for
    re-validation (i.e., their email can be updated and suppression cleared).

    Voluntary unsubscribes are NEVER included in this list.
    """
    from app.services.reconnect_service import ProspectRevalidationService

    svc = ProspectRevalidationService(db)
    return {"candidates": svc.list_bounced_prospects(current_user.tenant_id)}


@router.patch("/prospects/{prospect_id}/revalidate")
def revalidate_bounced_prospect(
    prospect_id: str,
    request: RevalidateProspectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
):
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
    from app.services.reconnect_service import ProspectRevalidationService

    try:
        svc = ProspectRevalidationService(db)
        result = svc.revalidate_prospect(
            tenant_id=current_user.tenant_id,
            prospect_id=prospect_id,
            new_email=request.new_email,
            reason=request.reason,
            performed_by=current_user.user_id,
        )
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
