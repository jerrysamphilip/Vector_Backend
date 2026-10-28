# app/routers/ai_email_router.py
"""
AI Email Generation API endpoints.
"""

from fastapi import APIRouter, Depends, status
from typing import List, Dict, Any
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import require_role
from app.schemas.ai_email_schema import (
    ClassifyProspectRequest,
    ClassifyBatchRequest,
    GenerateEmailRequest,
    CreateBlueprintRequest,
    ClassificationResult,
    GeneratedEmail,
    BlueprintResponse,
    ClassifyTestResponse,
)
from app.services.ai_generation_service import AiGenerationService
from app.utils.error_utils import handle_route_error
import logging

logger = logging.getLogger(__name__)


# No router prefix: every route spells out its full, noun-based path
router = APIRouter(tags=["AI Email Generation"])


def ai_generation_service(db=Depends(get_db)) -> AiGenerationService:
    return AiGenerationService.for_session(db)


Service = Depends(ai_generation_service)


# ============================================================
# Request/Response Models for New Endpoints
# ============================================================

class GenerateSequenceRequest(BaseModel):
    prospect_id: str
    product_name: str
    product_description: str = ""
    use_llm: bool = True
    cta_link: str = ""


class ComplianceCheckRequest(BaseModel):
    subject: str
    body: str


class EmailValidationRequest(BaseModel):
    email: str


# ============================================================
# Classification Endpoints
# ============================================================

@router.post("/prospects/classifications/test", response_model=ClassifyTestResponse)
def test_classify(
    request: ClassifyProspectRequest,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Test classification without saving to database.
    Useful for testing the classifier.
    """
    try:
        return AiGenerationService.classify_test(request.designation, request.company_name)
    except Exception as e:
        handle_route_error(e, context="test_classify")


@router.post("/prospects/classifications/batch", response_model=List[ClassificationResult])
def classify_batch(
    request: ClassifyBatchRequest,
    service: AiGenerationService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Classify a batch of prospects by their IDs.
    Saves classification to database.
    """
    try:
        return service.classify_batch(request.prospect_ids, current_user)
    except Exception as e:
        handle_route_error(e, context="classify_batch")


@router.post("/emails/generations", response_model=GeneratedEmail)
def generate_email(
    request: GenerateEmailRequest,
    service: AiGenerationService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Generate an email for a prospect using their persona blueprint.
    """
    try:
        return service.generate_email(request.prospect_id, request.product_name, request.cta_link, current_user)
    except Exception as e:
        handle_route_error(e, context="generate_email")


@router.post("/emails/sequences/generations")
def generate_sequence(
    request: GenerateSequenceRequest,
    service: AiGenerationService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Dict[str, Any]:
    """
    Generate a 3-email follow-up sequence for a prospect.
    
    Returns:
    - email_1: Introduction (Day 1)
    - email_2: Reminder (Day 3)
    - email_3: Last Chance (Day 7)
    - schedule: Timing info for each email
    - generation_method: LLM or BLUEPRINT
    """
    try:
        return service.generate_sequence(request.prospect_id, request.product_name, request.product_description,
                                         request.use_llm, request.cta_link, current_user)
    except Exception as e:
        handle_route_error(e, context="generate_sequence")


@router.post("/emails/compliance-checks")
def check_compliance(
    request: ComplianceCheckRequest,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Dict[str, Any]:
    """
    Check email content for spam triggers and compliance issues.
    
    Returns:
    - status: passed, warning, or failed
    - spam_score: 0-100 score
    - spam_triggers: List of flagged words/phrases
    - has_unsubscribe_link: Boolean
    - has_disallowed_phrases: Boolean
    """
    try:
        return AiGenerationService.compliance_check(request.subject, request.body)
    except Exception as e:
        handle_route_error(e, context="check_compliance")


@router.post("/emails/validations")
def validate_email(
    request: EmailValidationRequest,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Dict[str, Any]:
    """
    Validate if an email is a business email (not personal like gmail, yahoo).
    
    Returns:
    - email: The email checked
    - is_business: Boolean
    - domain: Email domain
    """
    return AiGenerationService.validate_email(request.email)


@router.get("/emails/cta-options/{purpose}")
def get_cta_variations(
    purpose: str,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Dict[str, Any]:
    """
    Get 7 CTA (Call-to-Action) options for a specific email purpose.
    
    Purpose options:
    - intro: First email (introduction)
    - reminder: Follow-up email
    - last_chance: Final outreach
    """
    return AiGenerationService.cta_options(purpose)


@router.get("/personas")
def list_personas(
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    List the canonical, user-selectable personas (excludes OTHER, which is an
    auto-classification fallback bucket, not a deliberate targeting choice).
    Used by the campaign wizard to always show all personas, regardless of
    whether any uploaded prospect has been classified into them yet.
    """
    return AiGenerationService.personas()


@router.get("/persona-blueprints", response_model=List[BlueprintResponse])
def list_blueprints(
    active_only: bool = True,
    service: AiGenerationService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    List all persona blueprints.
    """
    try:
        return service.blueprints(active_only)
    except Exception as e:
        handle_route_error(e, context="list_blueprints")


@router.post("/persona-blueprints", response_model=BlueprintResponse, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    request: CreateBlueprintRequest,
    service: AiGenerationService = Service,
    current_user: Any = Depends(require_role("PLATFORM_ADMIN")),
):
    """
    Create a new persona blueprint.

    Blueprints are global (no tenant_id) and used for every tenant's AI
    generation, so only platform admins may create them.
    """
    try:
        return service.create_blueprint(request)
    except Exception as e:
        handle_route_error(e, context="create_blueprint")


@router.get("/persona-blueprints/{persona_type}", response_model=BlueprintResponse)
def get_blueprint(
    persona_type: str,
    service: AiGenerationService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get a specific persona blueprint.
    """
    return service.blueprint(persona_type)

