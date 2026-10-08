# app/routers/ai_email_router.py
"""
AI Email Generation API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import require_role
from app.models import Prospect, PersonaBlueprint, ProspectPersona
from app.models.user import User
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
from app.services.ai_email_service import (
    classify_prospect,
    classify_prospects_batch,
    get_or_create_prospect_persona,
    generate_email_for_prospect,
    PERSONA_DISPLAY_NAMES,
)
from app.services.sequence_generator import (
    SequenceGeneratorService,
    get_cta_options,
)
from app.services.compliance_service import (
    quick_compliance_check,
    is_business_email,
)
from app.utils.email_utils import normalize_cta_link
from app.utils.error_utils import handle_route_error
import logging

logger = logging.getLogger(__name__)


# Kartik has changed this: Removed prefix to allow explicit noun-based resource paths
router = APIRouter(tags=["AI Email Generation"])


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

# Kartik has changed this: Converted to noun-based REST resource
@router.post("/prospects/classifications/test", response_model=ClassifyTestResponse)
def test_classify(
    request: ClassifyProspectRequest,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Test classification without saving to database.
    Useful for testing the classifier.
    """
    try:
        persona_type, confidence = classify_prospect(
            designation=request.designation,
            company_name=request.company_name
        )
        return {
            "designation": request.designation,
            "persona_type": persona_type,
            "confidence_score": confidence
        }
    except Exception as e:
        handle_route_error(e, context="test_classify")


# Kartik has changed this: Converted to noun-based REST resource
@router.post("/prospects/classifications/batch", response_model=List[ClassificationResult])
def classify_batch(
    request: ClassifyBatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Classify a batch of prospects by their IDs.
    Saves classification to database.
    """
    try:
        results = []
        for prospect_id in request.prospect_ids:
            prospect = db.query(Prospect).filter(
                Prospect.prospect_id == prospect_id,
                Prospect.tenant_id == current_user.tenant_id,
            ).first()
            if not prospect:
                continue
            persona = get_or_create_prospect_persona(db, prospect)
            results.append({
                "prospect_id": prospect_id,
                "persona_type": persona.persona_type,
                "confidence_score": persona.confidence_score,
                "classification_method": persona.classification_method
            })
        return results
    except Exception as e:
        handle_route_error(e, context="classify_batch")


# ============================================================
# Email Generation Endpoints
# ============================================================

# Kartik has changed this: Converted action to noun-based resource
@router.post("/emails/generations", response_model=GeneratedEmail)
def generate_email(
    request: GenerateEmailRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Generate an email for a prospect using their persona blueprint.
    """
    try:
        prospect = db.query(Prospect).filter(
            Prospect.prospect_id == request.prospect_id,
            Prospect.tenant_id == current_user.tenant_id,
        ).first()
        if not prospect:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Prospect {request.prospect_id} not found"
            )
        cta_link = normalize_cta_link(request.cta_link)
        result = generate_email_for_prospect(db, prospect, request.product_name, cta_link)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No blueprint available for this persona type"
            )
        return result
    except Exception as e:
        handle_route_error(e, context="generate_email")


# Kartik has changed this: Converted action to noun-based resource
@router.post("/emails/sequences/generations")
def generate_sequence(
    request: GenerateSequenceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
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
        prospect = db.query(Prospect).filter(
            Prospect.prospect_id == request.prospect_id,
            Prospect.tenant_id == current_user.tenant_id,
        ).first()
        if not prospect:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Prospect {request.prospect_id} not found"
            )
        service = SequenceGeneratorService(db)
        result = service.generate_email_sequence(
            prospect_id=request.prospect_id,
            product_name=request.product_name,
            product_description=request.product_description,
            use_llm=request.use_llm,
            cta_link=normalize_cta_link(request.cta_link)
        )
        if "error" in result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result["error"]
            )
        return result
    except Exception as e:
        handle_route_error(e, context="generate_sequence")


# ============================================================
# Compliance Endpoints
# ============================================================

# Kartik has changed this: Converted action to noun-based resource
@router.post("/emails/compliance-checks")
def check_compliance(
    request: ComplianceCheckRequest,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
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
        return quick_compliance_check(request.subject, request.body)
    except Exception as e:
        handle_route_error(e, context="check_compliance")


# Kartik has changed this: Converted action to noun-based resource
@router.post("/emails/validations")
def validate_email(
    request: EmailValidationRequest,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Dict[str, Any]:
    """
    Validate if an email is a business email (not personal like gmail, yahoo).
    
    Returns:
    - email: The email checked
    - is_business: Boolean
    - domain: Email domain
    """
    domain = request.email.split("@")[-1] if "@" in request.email else ""
    
    return {
        "email": request.email,
        "is_business": is_business_email(request.email),
        "domain": domain
    }


# Kartik has changed this: Moved under emails resource
@router.get("/emails/cta-options/{purpose}")
def get_cta_variations(
    purpose: str,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Dict[str, Any]:
    """
    Get 7 CTA (Call-to-Action) options for a specific email purpose.
    
    Purpose options:
    - intro: First email (introduction)
    - reminder: Follow-up email
    - last_chance: Final outreach
    """
    valid_purposes = ["intro", "reminder", "last_chance"]
    
    if purpose not in valid_purposes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid purpose. Use: {', '.join(valid_purposes)}"
        )
    
    return {
        "purpose": purpose,
        "options": get_cta_options(purpose)
    }


# ============================================================
# Blueprint Management Endpoints
# ============================================================

# Kartik has changed this: Updated to explicit plural resource
@router.get("/personas")
def list_personas(
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    List the canonical, user-selectable personas (excludes OTHER, which is an
    auto-classification fallback bucket, not a deliberate targeting choice).
    Used by the campaign wizard to always show all personas, regardless of
    whether any uploaded prospect has been classified into them yet.
    """
    return [
        {"persona_type": persona_type, "persona_label": label}
        for persona_type, label in PERSONA_DISPLAY_NAMES.items()
        if persona_type != "OTHER"
    ]


@router.get("/persona-blueprints", response_model=List[BlueprintResponse])
def list_blueprints(
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    List all persona blueprints.
    """
    try:
        query = db.query(PersonaBlueprint)
        if active_only:
            query = query.filter(PersonaBlueprint.is_active == True)
        return query.all()
    except Exception as e:
        handle_route_error(e, context="list_blueprints")


# Kartik has changed this: Updated to explicit plural resource
@router.post("/persona-blueprints", response_model=BlueprintResponse, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    request: CreateBlueprintRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
):
    """
    Create a new persona blueprint.

    Blueprints are global (no tenant_id) and used for every tenant's AI
    generation, so only platform admins may create them.
    """
    try:
        existing = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == request.persona_type
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Blueprint for {request.persona_type} already exists"
            )
        blueprint = PersonaBlueprint(
            persona_type=request.persona_type,
            openers=request.openers,
            value_angles=request.value_angles,
            ctas=request.ctas,
            proof_points=request.proof_points,
            tone_rules=request.tone_rules
        )
        db.add(blueprint)
        db.commit()
        db.refresh(blueprint)
        return blueprint
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="create_blueprint")


# Kartik has changed this: Updated to explicit plural resource
@router.get("/persona-blueprints/{persona_type}", response_model=BlueprintResponse)
def get_blueprint(
    persona_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get a specific persona blueprint.
    """
    blueprint = db.query(PersonaBlueprint).filter(
        PersonaBlueprint.persona_type == persona_type.upper()
    ).first()
    
    if not blueprint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint for {persona_type} not found"
        )
    
    return blueprint

