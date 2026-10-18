# app/routers/campaign_wizard_router.py
"""
Campaign Wizard API endpoints.
Multi-step campaign creation with list selection, segmentation, and bulk enrollment.

Thin HTTP layer over app/services/campaign_wizard_service.py (the wizard flow),
app/services/batch_processor_service.py (classification + AI templates) and
app/services/campaign_wizard_template_service.py (template edits). Service domain errors
(app/core/errors.py) are turned into HTTP responses centrally.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.concurrency import if_match_version, resolve_expected
from app.core.database import get_db
from app.models.user import User
from app.services import campaign_wizard_service as wizard
from app.services.batch_processor_service import process_prospect_list
from app.services.campaign_wizard_service import SegmentationRules
from app.services.campaign_wizard_template_service import CampaignWizardTemplateService

logger = logging.getLogger(__name__)

# No prefix: paths are resource-based (/campaigns/..., /prospect-lists/..., /templates/...)
router = APIRouter(tags=["Campaign Wizard"])

ANY_ROLE = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
MANAGER_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER")
PATH_PAYLOAD_MISMATCH = "campaign_id in path and payload must match"


# ============================================================
# Request/Response Models
# ============================================================

class CreateCampaignRequest(BaseModel):
    campaign_name: str
    send_start_hour: int = Field(9, ge=0, le=23)
    send_end_hour: int = Field(17, ge=0, le=23)
    timezone: str = "Asia/Kolkata"
    cta_link: Optional[str] = None
    inbox_ids: List[str] = []


class ProcessListRequest(BaseModel):
    """Request to auto-classify prospects and generate templates."""
    campaign_id: str
    list_id: str
    product_name: str = "your solution"
    company_name: str = "your company"
    campaign_description: str = ""  # Description for LLM context
    sequence_steps: Optional[List[dict]] = None  # User-configured sequence timing
    include_first_name_in_subject: bool = False  # Include {{first_name}} in subject
    creative_email: bool = False  # Apply creative writing style instructions
    excluded_prospect_ids: Optional[List[str]] = None  # Contacts deselected in the wizard


class GeneratePersonaTemplateRequest(BaseModel):
    """Request to generate (or on-demand regenerate) a single persona's template."""
    list_id: str
    product_name: str = "your solution"
    company_name: str = "your company"
    campaign_description: str = ""
    sequence_steps: Optional[List[dict]] = None
    excluded_prospect_ids: Optional[List[str]] = None
    include_first_name_in_subject: bool = False
    creative_email: bool = False


class SegmentationRequest(BaseModel):
    list_id: str
    persona_types: Optional[List[str]] = None
    email_types: Optional[List[str]] = ["BUSINESS"]
    industries: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    min_confidence: float = 0.0


class EnrollRequest(BaseModel):
    campaign_id: str
    list_id: str
    persona_types: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    cool_off_days: int = Field(7, ge=0, le=3650)
    excluded_prospect_ids: Optional[List[str]] = None


class ActivateRequest(BaseModel):
    campaign_id: str
    expected_version: Optional[int] = Field(None, ge=1)  # the campaign's row_version the wizard loaded (T-3)


class WizardRequest(BaseModel):
    campaign_name: str
    list_id: str
    persona_types: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    cool_off_days: int = Field(7, ge=0, le=3650)


class RegenerateRequest(BaseModel):
    """Request to regenerate a specific email field (subject or body)."""
    template_id: str
    field: str  # 'subject' or 'body'
    campaign_description: str = ""
    persona_type: str = ""
    product_name: str = "your solution"
    cta_link: Optional[str] = None
    custom_instruction: Optional[str] = None  # User-provided instruction
    creative_email: bool = False  # Apply creative writing style instructions
    step_number: int = 1  # Email step in sequence (1=intro, 2=followup_1, etc.)


class SequenceStepInput(BaseModel):
    """Single sequence step for schedule preview."""
    step_number: int
    wait_days: int


class SaveTemplateRequest(BaseModel):
    """Request to manually update a template's subject or body."""
    template_id: str
    subject: Optional[str] = None
    body: Optional[str] = None


class SchedulePreviewRequest(BaseModel):
    """Request to preview schedule dates for sequence steps."""
    sequence_steps: List[SequenceStepInput]
    state: Optional[str] = "NY"  # US state code for timezone (default: New York)


def _require_matching_campaign_id(path_campaign_id: str, payload_campaign_id: str) -> None:
    if payload_campaign_id != path_campaign_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=PATH_PAYLOAD_MISMATCH)


# ============================================================
# Schedule Preview Endpoint
# ============================================================

@router.post("/campaigns/schedules/previews", response_model=None)
def get_schedule_preview(
    request: SchedulePreviewRequest,
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Calculate timezone-aware schedule dates for sequence steps.

    Uses the business calendar logic to:
    - Schedule at 9 AM in the prospect's local timezone
    - Skip weekends (Saturday, Sunday)
    - Skip US federal holidays
    - Add jitter for anti-spam

    Returns formatted dates the frontend can display directly.
    """
    steps = [(s.step_number, s.wait_days) for s in request.sequence_steps]
    return {"schedule": wizard.build_schedule_preview(steps, request.state)}


# ============================================================
# Step-by-Step Endpoints
# ============================================================

@router.get("/campaigns/lists", response_model=None)
def get_available_lists(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> List[Dict[str, Any]]:
    """Get all prospect lists available for campaign creation."""
    return wizard.list_available_lists(db, current_user.tenant_id)


@router.post("/campaigns/wizard-creations", response_model=None)
def create_campaign(
    request: CreateCampaignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Step 1: Create a new campaign with basic settings."""
    campaign = wizard.create_campaign_for_tenant(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        campaign_name=request.campaign_name,
        send_start_hour=request.send_start_hour,
        send_end_hour=request.send_end_hour,
        timezone=request.timezone,
        cta_link=request.cta_link,
        inbox_ids=request.inbox_ids,
    )
    return {
        "campaign_id": campaign.campaign_id,
        "campaign_name": campaign.campaign_name,
        "status": campaign.status,
    }


@router.post("/campaigns/{campaign_id}/generations", response_model=None)
def process_list_auto(
    campaign_id: str,
    request: ProcessListRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Auto-classify all prospects in a list and generate email templates per persona.

    This endpoint:
    1. Classifies each prospect by persona type (using AI)
    2. Groups prospects by detected persona
    3. Generates one email template per persona using LLM
    4. Returns persona breakdown with generated templates
    """
    _require_matching_campaign_id(campaign_id, request.campaign_id)
    wizard.get_campaign_for_tenant(db, campaign_id, current_user.tenant_id)
    wizard.get_list_for_tenant(db, request.list_id, current_user.tenant_id)
    return process_prospect_list(
        db=db,
        list_id=request.list_id,
        campaign_id=campaign_id,
        tenant_id=current_user.tenant_id,
        product_name=request.product_name,
        company_name=request.company_name,
        campaign_description=request.campaign_description,
        sequence_steps=request.sequence_steps,
        include_first_name_in_subject=request.include_first_name_in_subject,
        creative_email=request.creative_email,
        excluded_prospect_ids=request.excluded_prospect_ids,
    )


@router.post("/campaigns/{campaign_id}/personas/{persona_type}/generations", response_model=None)
def generate_persona_template(
    campaign_id: str,
    persona_type: str,
    request: GeneratePersonaTemplateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Generate (or on-demand regenerate) a single persona's email template.

    Unlike POST /campaigns/{campaign_id}/generations (which only generates for
    personas actually detected in the list), this generates for ANY of the
    canonical personas — including ones with 0 classified prospects so far —
    so the campaign wizard can let a user preview/prepare content for a persona
    before any matching prospect has been uploaded/classified.
    """
    campaign = wizard.get_campaign_for_tenant(db, campaign_id, current_user.tenant_id)
    wizard.get_list_for_tenant(db, request.list_id, current_user.tenant_id)
    return wizard.generate_persona_template(
        db,
        campaign,
        tenant_id=current_user.tenant_id,
        persona_type=persona_type,
        list_id=request.list_id,
        product_name=request.product_name,
        campaign_description=request.campaign_description,
        sequence_steps=request.sequence_steps,
        excluded_prospect_ids=request.excluded_prospect_ids,
        include_first_name_in_subject=request.include_first_name_in_subject,
        creative_email=request.creative_email,
    )


@router.post("/prospect-lists/{list_id}/segmentations/previews", response_model=None)
def preview_segmentation(
    list_id: str,
    request: SegmentationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Step 2 & 3: Preview prospect count after segmentation rules."""
    rules = SegmentationRules(
        persona_types=request.persona_types,
        email_types=request.email_types,
        industries=request.industries,
        exclude_personal_emails=request.exclude_personal_emails,
        min_confidence=request.min_confidence,
    )
    return wizard.preview_list_segmentation(db, current_user.tenant_id, list_id, rules)


@router.post("/campaigns/{campaign_id}/enrollments", response_model=None)
def enroll_prospects(
    campaign_id: str,
    request: EnrollRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Step 4 & 5: Bulk enroll prospects after safety checks."""
    rules = SegmentationRules(
        persona_types=request.persona_types,
        exclude_personal_emails=request.exclude_personal_emails,
    )
    return wizard.enroll_list_into_campaign(
        db,
        tenant_id=current_user.tenant_id,
        campaign_id=campaign_id,
        list_id=request.list_id,
        rules=rules,
        cool_off_days=request.cool_off_days,
        excluded_prospect_ids=request.excluded_prospect_ids,
    )


@router.post("/campaigns/{campaign_id}/activations", response_model=None)
def activate_campaign(
    campaign_id: str,
    request: ActivateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> Dict[str, Any]:
    """Step 6: Activate the campaign. Send its row_version as expected_version or If-Match (T-3)."""
    _require_matching_campaign_id(campaign_id, request.campaign_id)
    campaign = wizard.activate_campaign_for_tenant(db, current_user.tenant_id, campaign_id, current_user.user_id,
                                                   resolve_expected(request.expected_version, if_match))
    return {
        "campaign_id": campaign.campaign_id,
        "campaign_name": campaign.campaign_name,
        "status": campaign.status,
        "launched_at": campaign.launched_at,
        "row_version": campaign.row_version,
    }


# ============================================================
# Complete Wizard (All-in-One)
# ============================================================

@router.post("/campaigns/wizard-executions", response_model=None)
def run_complete_wizard(
    request: WizardRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Run the complete campaign wizard in one step.
    Creates campaign, segments, enrolls, and prepares for activation.
    """
    rules = SegmentationRules(
        persona_types=request.persona_types,
        exclude_personal_emails=request.exclude_personal_emails,
    )
    result = wizard.run_campaign_wizard_for_tenant(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        campaign_name=request.campaign_name,
        list_id=request.list_id,
        rules=rules,
        cool_off_days=request.cool_off_days,
    )
    return {
        "campaign_id": result["campaign"].campaign_id,
        "campaign_name": result["campaign"].campaign_name,
        "stats": {
            "list_total": result["list_total"],
            "after_segmentation": result["after_segmentation"],
            "unsubscribed": result["unsubscribed"],
            "invalid_emails": result["invalid_emails"],
            "cool_off_excluded": result["cool_off_excluded"],
            "enrolled": result["enrolled"],
        },
        "sequences_created": result["sequences_created"],
        "status": result["status"],
        "next_step": "Call /campaign-wizard/step-4/activate to launch",
    }


# ============================================================
# Template editing
# ============================================================

@router.post("/templates/{template_id}/regenerations", response_model=None)
def regenerate_email_part(
    template_id: str,
    request: RegenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Regenerate a specific email field (subject or body) using LLM.
    """
    return CampaignWizardTemplateService(db).regenerate_field(
        current_user.tenant_id,
        template_id,
        field=request.field,
        campaign_description=request.campaign_description,
        persona_type=request.persona_type,
        product_name=request.product_name,
        cta_link=request.cta_link,
        custom_instruction=request.custom_instruction,
        creative_email=request.creative_email,
        step_number=request.step_number,
    )


@router.post("/campaign-wizard/update-template", response_model=None)
def update_template_manual(
    request: SaveTemplateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Manually update a template's subject or body after user edit.
    Propagates the change to ALL persona variants of the same sequence step
    so that every prospect in the campaign receives the edited content.
    """
    template = CampaignWizardTemplateService(db).save_manual_edit(
        current_user.tenant_id, request.template_id, request.subject, request.body)
    return {
        "template_id": template.template_id,
        "subject": template.subject,
        "body": template.body,
    }
