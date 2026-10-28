# app/routers/campaign_wizard_router.py
"""
Campaign Wizard API endpoints.
Multi-step campaign creation with list selection, segmentation, and bulk enrollment.

Thin HTTP layer (ADR 0009, chunk A2) over app/services/campaign_wizard_service.py (the wizard flow,
classification + AI templates through batch_processor_service) and
app/services/campaign_wizard_template_service.py (template edits): one service call per route, the
service scopes everything to the caller's tenant and owns the unit of work. Service domain errors
(app/core/errors.py) are turned into HTTP responses centrally.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.concurrency import if_match_version, resolve_expected
from app.core.database import get_db
from app.schemas.campaign_wizard_schema import (
    ActivateRequest,
    CreateCampaignRequest,
    EnrollRequest,
    GeneratePersonaTemplateRequest,
    ProcessListRequest,
    SaveTemplateRequest,
    SchedulePreviewRequest,
    SegmentationRequest,
    WizardRequest,
)
from app.services import campaign_wizard_service as wizard_rules
from app.services.campaign_wizard_service import CampaignWizardService, PersonaGeneration, SegmentationRules
from app.services.campaign_wizard_template_service import CampaignWizardTemplateService

logger = logging.getLogger(__name__)

# No prefix: paths are resource-based (/campaigns/..., /prospect-lists/..., /templates/...)
router = APIRouter(tags=["Campaign Wizard"])

ANY_ROLE = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
MANAGER_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER")
PATH_PAYLOAD_MISMATCH = "campaign_id in path and payload must match"


def wizard_service(db=Depends(get_db)) -> CampaignWizardService:
    return CampaignWizardService.for_session(db)


def wizard_template_service(db=Depends(get_db)) -> CampaignWizardTemplateService:
    return CampaignWizardTemplateService.for_session(db)


# ============================================================
# Request/Response Models (the others: app/schemas/campaign_wizard_schema.py)
# ============================================================

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


def _require_matching_campaign_id(path_campaign_id: str, payload_campaign_id: str) -> None:
    if payload_campaign_id != path_campaign_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=PATH_PAYLOAD_MISMATCH)


# ============================================================
# Schedule Preview Endpoint
# ============================================================

@router.post("/campaigns/schedules/previews", response_model=None)
def get_schedule_preview(
    request: SchedulePreviewRequest,
    current_user: Any = Depends(require_role(*ANY_ROLE)),
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
    return {"schedule": wizard_rules.build_schedule_preview(steps, request.state)}


# ============================================================
# Step-by-Step Endpoints
# ============================================================

@router.get("/campaigns/lists", response_model=None)
def get_available_lists(
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> List[Dict[str, Any]]:
    """Get all prospect lists available for campaign creation."""
    return wizard.available_lists(current_user)


@router.post("/campaigns/wizard-creations", response_model=None)
def create_campaign(
    request: CreateCampaignRequest,
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Step 1: Create a new campaign with basic settings."""
    return wizard.create_campaign(current_user, request.campaign_name, request.send_start_hour,
                                  request.send_end_hour, request.cta_link, request.inbox_ids)


@router.post("/campaigns/{campaign_id}/generations", response_model=None)
def process_list_auto(
    campaign_id: str,
    request: ProcessListRequest,
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
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
    return wizard.process_list(
        current_user, campaign_id, request.list_id,
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
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Generate (or on-demand regenerate) a single persona's email template.

    Unlike POST /campaigns/{campaign_id}/generations (which only generates for
    personas actually detected in the list), this generates for ANY of the
    canonical personas — including ones with 0 classified prospects so far —
    so the campaign wizard can let a user preview/prepare content for a persona
    before any matching prospect has been uploaded/classified.
    """
    return wizard.generate_persona_template(current_user, campaign_id, PersonaGeneration(
        persona_type=persona_type, list_id=request.list_id, product_name=request.product_name,
        campaign_description=request.campaign_description, sequence_steps=request.sequence_steps,
        excluded_prospect_ids=request.excluded_prospect_ids,
        include_first_name_in_subject=request.include_first_name_in_subject, creative_email=request.creative_email,
    ))


@router.post("/prospect-lists/{list_id}/segmentations/previews", response_model=None)
def preview_segmentation(
    list_id: str,
    request: SegmentationRequest,
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Step 2 & 3: Preview prospect count after segmentation rules."""
    rules = SegmentationRules(
        persona_types=request.persona_types,
        email_types=request.email_types,
        industries=request.industries,
        exclude_personal_emails=request.exclude_personal_emails,
        min_confidence=request.min_confidence,
    )
    return wizard.preview_segmentation(current_user, list_id, rules)


@router.post("/campaigns/{campaign_id}/enrollments", response_model=None)
def enroll_prospects(
    campaign_id: str,
    request: EnrollRequest,
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """Step 4 & 5: Bulk enroll prospects after safety checks."""
    rules = SegmentationRules(
        persona_types=request.persona_types,
        exclude_personal_emails=request.exclude_personal_emails,
    )
    return wizard.enroll_list(current_user, campaign_id, request.list_id, rules, request.cool_off_days,
                              request.excluded_prospect_ids)


@router.post("/campaigns/{campaign_id}/activations", response_model=None)
def activate_campaign(
    campaign_id: str,
    request: ActivateRequest,
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*MANAGER_ROLES)),
    if_match: Optional[int] = Depends(if_match_version),
) -> Dict[str, Any]:
    """Step 6: Activate the campaign. Send its row_version as expected_version or If-Match (T-3)."""
    _require_matching_campaign_id(campaign_id, request.campaign_id)
    return wizard.activate(current_user, campaign_id, resolve_expected(request.expected_version, if_match))


# ============================================================
# Complete Wizard (All-in-One)
# ============================================================

@router.post("/campaigns/wizard-executions", response_model=None)
def run_complete_wizard(
    request: WizardRequest,
    wizard: CampaignWizardService = Depends(wizard_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Run the complete campaign wizard in one step.
    Creates campaign, segments, enrolls, and prepares for activation.
    """
    rules = SegmentationRules(
        persona_types=request.persona_types,
        exclude_personal_emails=request.exclude_personal_emails,
    )
    result = wizard.run_wizard(current_user, request.campaign_name, request.list_id, rules, request.cool_off_days)
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
    templates: CampaignWizardTemplateService = Depends(wizard_template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Regenerate a specific email field (subject or body) using LLM.
    """
    return templates.regenerate_field(
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
    templates: CampaignWizardTemplateService = Depends(wizard_template_service),
    current_user: Any = Depends(require_role(*ANY_ROLE)),
) -> Dict[str, Any]:
    """
    Manually update a template's subject or body after user edit.
    Propagates the change to ALL persona variants of the same sequence step
    so that every prospect in the campaign receives the edited content.
    """
    template = templates.save_manual_edit(current_user.tenant_id, request.template_id, request.subject, request.body)
    return {
        "template_id": template.template_id,
        "subject": template.subject,
        "body": template.body,
    }
