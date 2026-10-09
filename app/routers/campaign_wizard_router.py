# app/routers/campaign_wizard_router.py
"""
Campaign Wizard API endpoints.
Multi-step campaign creation with list selection, segmentation, and bulk enrollment.
"""

import re
from tempfile import template
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.core.database import get_db
from app.models import ProspectList, Campaign, EmailTemplate, EmailSequence
from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
from app.services.campaign_wizard_service import (
    SegmentationRules,
    step_1_create_campaign,
    step_2_get_prospects_from_list,
    step_3_apply_segmentation,
    step_4_run_safety_checks,
    step_5_bulk_enroll,
    step_6_create_email_sequences,
    step_7_activate_campaign,
    run_campaign_wizard,
)
from app.services.batch_processor_service import (
    process_prospect_list,
    batch_classify_prospects,
    generate_template_for_persona,
)
from app.utils.email_utils import (
    normalize_unsubscribe_footer,
    strip_cta_content_no_link,
    ensure_core_personalization_tokens,
    normalize_cta_link,
    normalize_paragraph_spacing,
    has_effective_cta_link,
    finalize_email_body,
)
from app.utils.retry_utils import retry_with_backoff
from app.prompts.email_examples import (
    get_capability_pool_block,
    get_step_tone_block,
    get_conference_step_tone_block,
)
from app.utils.email_context_detector import detect_email_context, EMAIL_CONTEXT_CONFERENCE

logger = logging.getLogger(__name__)


# Kartik has changed this: Removed prefix to support cleaner domain-based paths
router = APIRouter(tags=["Campaign Wizard"])


from app.core.auth import require_role
from app.models.user import User


# ============================================================
# Request/Response Models
# ============================================================

class CreateCampaignRequest(BaseModel):
    campaign_name: str
    send_start_hour: int = 9
    send_end_hour: int = 17
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
    cool_off_days: int = 7
    excluded_prospect_ids: Optional[List[str]] = None


class ActivateRequest(BaseModel):
    campaign_id: str


class WizardRequest(BaseModel):
    campaign_name: str
    list_id: str
    persona_types: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    cool_off_days: int = 7


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


# ============================================================
# Schedule Preview Endpoint
# ============================================================

# Kartik has changed this: Renamed to noun-based REST path
@router.post("/campaigns/schedules/previews")
def get_schedule_preview(
    request: SchedulePreviewRequest,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Calculate timezone-aware schedule dates for sequence steps.
    
    Uses the business calendar logic to:
    - Schedule at 9 AM in the prospect's local timezone
    - Skip weekends (Saturday, Sunday)
    - Skip US federal holidays
    - Add jitter for anti-spam
    
    Returns formatted dates the frontend can display directly.
    """
    from datetime import datetime
    from app.utils.business_calendar import calculate_send_time
    
    base_time = datetime.utcnow()
    schedule = []
    cumulative_wait_days = 0
    
    for step in request.sequence_steps:
        # Accumulate wait_days for ALL steps (including step 1 if it has wait_days > 0)
        cumulative_wait_days += step.wait_days
        
        # Calculate timezone-aware send time
        send_datetime = calculate_send_time(
            base_time=base_time,
            wait_days=cumulative_wait_days,
            state=request.state,
            send_hour=9,
            send_minute=0,
            add_jitter=False  # Disable jitter for preview (consistent display)
        )
        
        # Format for display
        formatted = send_datetime.strftime("%a, %b %d")  # e.g., "Mon, Jan 20"
        
        schedule.append({
            "step_number": step.step_number,
            "send_date": send_datetime.isoformat() + "Z",
            "formatted": formatted,
            "wait_days": step.wait_days if step.step_number > 1 else 0
        })
    
    return {"schedule": schedule}


# ============================================================
# Step-by-Step Endpoints
# ============================================================

# Kartik has changed this: Grouped under campaigns resource
@router.get("/campaigns/lists")
def get_available_lists(db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Get all prospect lists available for campaign creation."""
    lists = db.query(ProspectList).filter(
        ProspectList.tenant_id == current_user.tenant_id
    ).all()
    
    return [
        {
            "list_id": l.list_id,
            "list_name": l.list_name,
            "prospect_count": l.prospect_count,
            "created_at": l.created_at,
        }
        for l in lists
    ]


# Kartik has changed this: Use domain entity resource creation
@router.post("/campaigns/wizard-creations")
def create_campaign(request: CreateCampaignRequest, db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Step 1: Create a new campaign with basic settings."""
    normalized_cta_link = normalize_cta_link(request.cta_link)
    campaign = step_1_create_campaign(
        db=db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
        campaign_name=request.campaign_name,
        send_start_hour=request.send_start_hour,
        send_end_hour=request.send_end_hour,
        timezone=request.timezone,
        cta_link=normalized_cta_link,
        inbox_ids=request.inbox_ids,
    )
    return {
        "campaign_id": campaign.campaign_id,
        "campaign_name": campaign.campaign_name,
        "status": campaign.status,
    }


# Kartik has changed this: Converted action to sub-resource
@router.post("/campaigns/{campaign_id}/generations")
def process_list_auto(campaign_id: str, request: ProcessListRequest, db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """
    Auto-classify all prospects in a list and generate email templates per persona.
    
    This endpoint:
    1. Classifies each prospect by persona type (using AI)
    2. Groups prospects by detected persona
    3. Generates one email template per persona using LLM
    4. Returns persona breakdown with generated templates
    """
    # Verify campaign exists
    if request.campaign_id != campaign_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="campaign_id in path and payload must match",
        )

    campaign = db.query(Campaign).filter(
        Campaign.campaign_id == campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found"
        )
    
    # Verify list exists
    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == request.list_id,
        ProspectList.tenant_id == current_user.tenant_id,
    ).first()
    
    if not prospect_list:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prospect list not found"
        )
    
    # Process the list
    result = process_prospect_list(
        db=db,
        list_id=request.list_id,
        campaign_id=campaign_id,
        tenant_id=current_user.tenant_id,
        product_name=request.product_name,
        company_name=request.company_name,
        campaign_description=request.campaign_description,
        sequence_steps=request.sequence_steps,  # Pass user-configured sequence timing
        include_first_name_in_subject=request.include_first_name_in_subject,  # Pass preference
        creative_email=request.creative_email,  # Pass creative toggle
        excluded_prospect_ids=request.excluded_prospect_ids,  # Contacts deselected in the wizard
    )

    return result


@router.post("/campaigns/{campaign_id}/personas/{persona_type}/generations")
def generate_persona_template(
    campaign_id: str,
    persona_type: str,
    request: GeneratePersonaTemplateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Generate (or on-demand regenerate) a single persona's email template.

    Unlike POST /campaigns/{campaign_id}/generations (which only generates for
    personas actually detected in the list), this generates for ANY of the
    canonical personas — including ones with 0 classified prospects so far —
    so the campaign wizard can let a user preview/prepare content for a persona
    before any matching prospect has been uploaded/classified.
    """
    campaign = db.query(Campaign).filter(
        Campaign.campaign_id == campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == request.list_id,
        ProspectList.tenant_id == current_user.tenant_id,
    ).first()
    if not prospect_list:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prospect list not found")

    try:
        email_context = detect_email_context(request.product_name, request.campaign_description)
        cta_link = normalize_cta_link(campaign.cta_link)
        cta_enabled = has_effective_cta_link(cta_link)

        sequences = db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).all()
        step_to_seq_id = {s.step_number: s.sequence_id for s in sequences}

        # Reuse (or create) classifications for this list; may yield 0 prospects for this persona.
        grouped = batch_classify_prospects(db, request.list_id, current_user.tenant_id, request.excluded_prospect_ids)
        prospects = grouped.get(persona_type, [])

        # Clear only this persona's prior template(s) for this campaign to avoid duplicates
        # on re-generation — other personas' templates are left untouched.
        db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.designation == persona_type,
        ).delete(synchronize_session=False)

        result = generate_template_for_persona(
            db, campaign_id, persona_type, prospects,
            request.product_name, request.campaign_description, request.sequence_steps,
            request.include_first_name_in_subject, request.creative_email, email_context,
            cta_link, cta_enabled, step_to_seq_id,
        )
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        raise


# Kartik has changed this: Use domain entity resource
@router.post("/prospect-lists/{list_id}/segmentations/previews")
def preview_segmentation(
    list_id: str,
    request: SegmentationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Step 2 & 3: Preview prospect count after segmentation rules."""
    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == list_id,
        ProspectList.tenant_id == current_user.tenant_id,
    ).first()
    if not prospect_list:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prospect list not found",
        )

    # Get all prospects from list
    all_prospects = step_2_get_prospects_from_list(db, list_id)
    
    if not all_prospects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No prospects found in this list"
        )
    
    # Apply segmentation
    rules = SegmentationRules(
        persona_types=request.persona_types,
        email_types=request.email_types,
        industries=request.industries,
        exclude_personal_emails=request.exclude_personal_emails,
        min_confidence=request.min_confidence,
    )
    
    segment_rejections = []
    segmented = step_3_apply_segmentation(db, all_prospects, rules, segment_rejections)
    
    # Run safety checks
    safety = step_4_run_safety_checks(db, current_user.tenant_id, segmented)
    rejections = segment_rejections + safety["rejections"]
    
    return {
        "list_total": len(all_prospects),
        "after_segmentation": len(segmented),
        "after_safety_checks": safety["safe_count"],
        "breakdown": {
            "unsubscribed": safety["unsubscribed_count"],
            "invalid_emails": safety["invalid_count"],
            "cool_off_excluded": safety["cool_off_count"],
        },
        "ready_to_enroll": safety["safe_count"],
        "rejected_count": len(rejections),
        "rejected_summary": summarize(rejections),
        "rejected": rejections[:MAX_REJECTIONS_RETURNED],
    }


# Kartik has changed this: Converted to sub-resource enrollment
@router.post("/campaigns/{campaign_id}/enrollments")
def enroll_prospects(
    campaign_id: str,
    request: EnrollRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Step 4 & 5: Bulk enroll prospects after safety checks."""
    import logging
    logger = logging.getLogger(__name__)
    
    # Verify campaign exists
    campaign = db.query(Campaign).filter(
        Campaign.campaign_id == campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found"
        )
    
    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == request.list_id,
        ProspectList.tenant_id == current_user.tenant_id,
    ).first()
    if not prospect_list:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prospect list not found",
        )

    # Get prospects from list
    all_prospects = step_2_get_prospects_from_list(db, request.list_id, request.excluded_prospect_ids)
    logger.warning(f"[ENROLL] Step 2 - Prospects from list '{request.list_id}': {len(all_prospects)}")
    
    # Segmentation
    rules = SegmentationRules(
        persona_types=request.persona_types,
        exclude_personal_emails=request.exclude_personal_emails,
    )
    logger.warning(f"[ENROLL] Step 3 - Segmentation rules: exclude_personal={rules.exclude_personal_emails}, persona_types={rules.persona_types}")
    
    segment_rejections = []
    segmented = step_3_apply_segmentation(db, all_prospects, rules, segment_rejections)
    logger.warning(f"[ENROLL] Step 3 - After segmentation: {len(segmented)} (dropped {len(all_prospects) - len(segmented)})")
    
    # Safety checks
    safety = step_4_run_safety_checks(
        db, current_user.tenant_id, segmented, request.cool_off_days, campaign_id=campaign_id
    )
    rejections = segment_rejections + safety["rejections"]
    logger.warning(f"[ENROLL] Step 4 - Safety: safe={safety['safe_count']}, unsub={safety['unsubscribed_count']}, invalid={safety['invalid_count']}, cool_off={safety['cool_off_count']}")
    
    # Enroll
    enrolled = step_5_bulk_enroll(db, campaign_id, safety["safe_prospects"])
    logger.warning(f"[ENROLL] Step 5 - Enrolled: {enrolled}")
    
    return {
        "campaign_id": campaign_id,
        "enrolled_count": enrolled,
        "sequences_created": 0,
        "status": "READY_TO_ACTIVATE",
        "rejected_count": len(rejections),
        "rejected_summary": summarize(rejections),
        "rejected": rejections[:MAX_REJECTIONS_RETURNED],
        "_debug": {
            "prospects_in_list": len(all_prospects),
            "after_segmentation": len(segmented),
            "safe_count": safety["safe_count"],
            "unsubscribed": safety["unsubscribed_count"],
            "invalid": safety["invalid_count"],
            "cool_off": safety["cool_off_count"],
        }
    }


# Kartik has changed this: Converted workflow step into sub-resource operation
@router.post("/campaigns/{campaign_id}/activations")
def activate_campaign(
    campaign_id: str,
    request: ActivateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Step 6: Activate the campaign."""
    if request.campaign_id != campaign_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="campaign_id in path and payload must match",
        )

    scoped_campaign = db.query(Campaign).filter(
        Campaign.campaign_id == campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not scoped_campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    from app.services.sender_identity import SenderIdentityMissing
    try:
        campaign = step_7_activate_campaign(db, campaign_id, current_user.user_id)
    except SenderIdentityMissing as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found"
        )
    
    return {
        "campaign_id": campaign.campaign_id,
        "campaign_name": campaign.campaign_name,
        "status": campaign.status,
        "launched_at": campaign.launched_at,
    }


# ============================================================
# Complete Wizard (All-in-One)
# ============================================================

# Kartik has changed this: Converted to noun-based REST resource
@router.post("/campaigns/wizard-executions")
def run_complete_wizard(
    request: WizardRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Run the complete campaign wizard in one step.
    Creates campaign, segments, enrolls, and prepares for activation.
    """
    rules = SegmentationRules(
        persona_types=request.persona_types,
        exclude_personal_emails=request.exclude_personal_emails,
    )
    
    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == request.list_id,
        ProspectList.tenant_id == current_user.tenant_id,
    ).first()
    if not prospect_list:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prospect list not found",
        )

    result = run_campaign_wizard(
        db=db,
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


# Kartik has changed this: Modeled as template sub-resource
@router.post("/templates/{template_id}/regenerations")
def regenerate_email_part(
    template_id: str,
    request: RegenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Regenerate a specific email field (subject or body) using LLM.
    """
    from app.services.sender_identity import bind_tenant
    bind_tenant(current_user.tenant_id, db)
    from app.services.sequence_generator import openai_client
    from app.core.config import settings
    
    # Find the template
    template = db.query(EmailTemplate).join(
        Campaign, Campaign.campaign_id == EmailTemplate.campaign_id
    ).filter(
        EmailTemplate.template_id == template_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found"
        )
    
    if request.field not in ['subject', 'body']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Field must be 'subject' or 'body'"
        )
    
    # Detect email context from campaign description
    email_context = detect_email_context(request.product_name, request.campaign_description)
    is_conference = (email_context == EMAIL_CONTEXT_CONFERENCE)

    # Generate new content using LLM
    if request.field == 'subject':
        if is_conference:
            prompt = f"""Generate a new subject line for a <<SENDER_COMPANY>> conference/in-person outreach email.
Context: {request.campaign_description or 'Conference or in-person outreach at <<SENDER_COMPANY>>'}
Target persona: {request.persona_type or 'professional'}
Email step: {request.step_number}

Current subject: {template.subject}

Rules (conference edition — different from cold outreach):
- ALWAYS start with: {{{{first_name}}}}, [rest of subject]
- If a specific event is named in the context: include that event name in the subject
- If NO event is named: use "In-Person Meeting" — NEVER fabricate a conference name
- Title Case throughout (NOT all-lowercase)
- Lead with a VALUE PROPOSITION or specific topic — NOT just "catch up" or "let's meet"
  GOOD: "{{{{first_name}}}}, Accelerating Hub Performance — Meet at HLTH 25"
  GOOD: "{{{{first_name}}}}, In-Person Meeting: Enhancing Patient Experience and Pharma Innovation"
  GOOD: "{{{{first_name}}}}, Compliance & Data Security — Connect at AXS26"
  BAD: "{{{{first_name}}}}, Quick Catch Up at HLTH 25" (too generic)
- BANNED in subject: "quick catch up", "let's catch up", "let's meet", "following up", "checking in", "looping back", "Transform", "AI"
- Do NOT use actual names, use {{{{tokens}}}} only
- Must be DIFFERENT from the current subject — generate a fresh topic angle

Return ONLY the new subject line, nothing else."""
        else:
            prompt = f"""Generate a new subject line for a <<SENDER_COMPANY>> cold outreach email.
Context: {request.campaign_description or 'B2B sales outreach at <<SENDER_COMPANY>>'}
Target persona: {request.persona_type or 'professional'}
Product: {request.product_name}

Current subject: {template.subject}

Rules:
- 3–7 words, descriptive, not clickbait
- Reference the prospect's industry, role, or a specific challenge
- Use tokens: {{{{company_name}}}}, {{{{first_name}}}}, {{{{industry}}}}
- BANNED: "Transform", "AI", "Improving outcomes", "idea for [name]", "quick question", "quick thought"
- BANNED: Urgency language ("Act now", "Last chance"), negative framing ("Why does {{{{company_name}}}} fail")
- Do NOT use actual names, use {{{{tokens}}}} only

Return ONLY the new subject line, nothing else."""
    else:
        normalized_cta_link = normalize_cta_link(request.cta_link)
        cta_enabled = has_effective_cta_link(normalized_cta_link)

        capability_pool_block = get_capability_pool_block()

        if is_conference:
            _step_to_conf_email_type = {
                1: "conference_intro", 2: "conference_fup1", 3: "conference_fup2",
                4: "conference_fup3", 5: "conference_fup4", 6: "conference_fup5", 7: "conference_fup6",
            }
            email_type_for_step = _step_to_conf_email_type.get(request.step_number, "conference_intro")
            step_tone_block = get_conference_step_tone_block(request.step_number)

            cta_rule = (
                "DUAL CTA REQUIRED — in-person meeting ask first, virtual fallback second. "
                "GOOD: 'Please let me know your availability for an In-Person meeting at the event. "
                "If not at the event, we could also connect virtually.' "
                "BAD: 'Up for a quick Zoom next week?' (virtual only — banned)"
            ) if cta_enabled else "Close naturally. No meeting ask, no URL."

            prompt = f"""Write a <<SENDER_COMPANY>> conference/in-person outreach email body.

Context: {request.campaign_description or 'In-person outreach at <<SENDER_COMPANY>>'}
Target persona: {request.persona_type or 'professional'}
Email step: {request.step_number} ({email_type_for_step})

=== CONFERENCE EMAIL STRUCTURE (follow exactly — NOT cold outreach structure) ===

Read the context above to identify: event name (ONLY if explicitly mentioned), event dates/visit window, location, and <<SENDER_COMPANY>> contact person.
CRITICAL: If no specific conference/event name is in the context, do NOT invent one. Use "In-Person Meeting" and reference the city/dates instead.

Follow the PER-EMAIL TONE GUIDE below for this step's opener and CTA style exactly.

Structure for every email:
1. OPENER — use the opener style from the tone guide for this step (NOT a cold pain-point question)
2. COMPANY INTRO — "<<SENDER_COMPANY>>" with credibility markers (NEVER use {{{{our_company}}}})
3. CAPABILITY BULLETS — use services from the reference pool below (follow bullet style from tone guide)
4. DUAL CTA — {cta_rule}

=== FORMAT RULES ===
- 150–250 words
- First line: "Hi {{{{first_name}}}},"
- Write "<<SENDER_COMPANY>>" — never use {{{{our_company}}}} token
- No signature lines (sender signs off separately)
- Plain text only (no HTML tags)
- Tokens: {{{{first_name}}}}, {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}

=== ALLOWED IN THIS CONTEXT (different from cold outreach) ===
- "Following up", "would love to", "looping back" — ALLOWED in body
- Naming the <<SENDER_COMPANY>> rep from the campaign description — ALLOWED and encouraged in email 1
- Temporal urgency with event dates — ALLOWED

=== HARD BANS ===
Single-option virtual CTA (must always offer both in-person and virtual).
Cold openers: "Are manual processes, data silos, and compliance hurdles slowing your growth?" — BANNED here
"I noticed", "I came across", "as a key decision-maker", "many teams face"
"leverage", "synergy", "AI-powered", "cutting-edge", {{{{our_company}}}} token
DO NOT copy opener text, CTA text, or bullet topics from examples or tone guide verbatim.
Write UNIQUE, prospect-specific content — examples show the STRUCTURE, not the exact words.

{capability_pool_block}

{step_tone_block}

{"CTA: " + normalized_cta_link if cta_enabled else "No CTA link. Do NOT include a booking link or URL."}

Return ONLY the email body text, nothing else."""

        else:
            _step_to_email_type = {1: "intro", 2: "followup_1", 3: "followup_2", 4: "followup_3", 5: "followup_4", 6: "followup_5"}
            email_type_for_step = _step_to_email_type.get(request.step_number, "intro")
            step_tone_block = get_step_tone_block(request.step_number)

            cta_rule = (
                "End with a direct meeting or call ask. "
                "GOOD: 'Up for a quick 15–20 min virtual chat next week?' / "
                "'Let's connect for a quick, no-pressure call.' / "
                "'Happy to jump on a quick call or send a short overview — whichever\\'s easier.'"
            ) if cta_enabled else "Close naturally. No meeting ask, no URL."

            prompt = f"""Write a <<SENDER_COMPANY>> cold outreach email body.

Context: {request.campaign_description or 'B2B sales outreach at <<SENDER_COMPANY>>'}
Target persona: {request.persona_type or 'professional'}
Product: {request.product_name}
Email step: {request.step_number} ({email_type_for_step})

=== HOUSE EMAIL STRUCTURE (follow exactly) ===

1. PAIN POINT HOOK — Open with a direct, UNIQUE question about the prospect's operational challenge.
   Use tokens: {{{{first_name}}}}, {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}
   Write a question specific to THIS prospect's role and industry. Here are diverse examples for inspiration (do NOT copy any of these — write your own):
   - "Is {{{{company_name}}}}'s patient onboarding still running on spreadsheets and manual handoffs?"
   - "How much time is {{{{company_name}}}}'s {{{{designation}}}} team losing to prior auth bottlenecks?"
   - "Are reconciliation gaps across your pharmacy systems creating downstream billing errors?"
   - "Is your {{{{industry}}}} team still stitching together data from disconnected platforms?"

2. COMPANY INTRO — Introduce <<SENDER_COMPANY>> with credibility markers.
   REQUIRED: Always write "<<SENDER_COMPANY>>" — do NOT use {{{{our_company}}}} token.

3. CAPABILITY BULLETS (4–5 bullets, minimum 4, maximum 5) — Use <<SENDER_COMPANY>>'s REAL services from the reference example below.
   DO NOT invent generic bullets like "optimize workflows" or "ensure data integrity".
   Format: "    •" (4 spaces + bullet)

4. DIRECT CTA — {cta_rule}

=== FORMAT RULES ===
- 150–250 words
- Bullet lists required
- First line: "Hi {{{{first_name}}}},"
- Write "<<SENDER_COMPANY>>" — never use {{{{our_company}}}} token
- No signature lines (sender signs off separately)
- Plain text only (no HTML tags)
- Include at least THREE tokens: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}

=== HARD BANS ===
BANNED OPENER: "Are manual processes, data silos, and compliance hurdles slowing your growth?" — NEVER use this sentence or close variations.
Never use: "many teams face", "many {{{{industry}}}} teams", "in today's landscape",
"leverage", "synergy", "AI-powered", "cutting-edge", "streamline operations",
"I'd love to", "excited to share", {{{{our_company}}}} token.

{capability_pool_block}

{step_tone_block}

{"CTA Link: " + normalized_cta_link if cta_enabled else "No CTA link. Do NOT include a meeting ask, booking link, or URL."}

Return ONLY the email body text, nothing else."""

    # Append custom instruction if provided
    if request.custom_instruction:
        prompt += f"\n\nUSER_FEEDBACK: The user has requested: '{request.custom_instruction}'. Apply this preference to this specific rewrite if reasonable."
    
    if request.creative_email and request.field == "subject":
        prompt += """

CREATIVE_EMAIL_MODE:
- Avoid generic subject patterns ("Quick question for you", "Idea for", "Quick thought for")
- NEVER use these exact phrases: "quick thought", "quick question", "unlock growth", "boost efficiency", "trends", "ideas for"
- Keep it specific, concrete, operational, and role-relevant (e.g. "Clinical workflow notes" or "Data entry turnaround")
"""
    
    def _call_openai():
        return openai_client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a B2B email copywriter. Generate only what is asked, no explanations."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=500,
        )
    
    try:
        response = retry_with_backoff(
            _call_openai,
            max_attempts=3,
            base_delay=1.0,
            max_delay=30.0,
        )

        if response is None:
            logger.error("[regenerate_email_part] All retry attempts exhausted.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI service is temporarily unavailable. Please try again in a few moments."
            )
        
        new_content = response.choices[0].message.content.strip()
        
        # Update the template
        if request.field == 'subject':
            template.subject = new_content
        else:
            if not has_effective_cta_link(request.cta_link):
                new_content = strip_cta_content_no_link(new_content)
            # strip signatures (keep this)
            ...
            # append signature
            new_content = f"{new_content}\n\nRegards,\n{{{{your_name}}}}"

            # ✅ single formatting entry point
            template.body = finalize_email_body(new_content)
            
            # Unconditionally strip ALL trailing signature lines from the LLM output
            while True:
                prev_len = len(new_content)
                new_content = re.sub(r'(?i)(<p>|^\s*|<br\s*/?>)*\s*(Best(\s+Regards)?|Sincerely|Regards|Thanks|Cheers)?,?(\s*<br\s*/?>)?\s*(\{|\[)*your_name(\}|\])*(<\/p>)?\s*$', '', new_content).strip()
                new_content = re.sub(r'(?i)\n+\s*(Best(\s+Regards)?|Sincerely|Regards|Thanks|Cheers),?\s*$', '', new_content).strip()
                if len(new_content) == prev_len:
                    break
            
            # Always forcefully append the formatted signature
            new_content = f"{new_content}\n\nRegards,\n{{{{your_name}}}}"
                
            # template.body = normalize_unsubscribe_footer(new_content)
            template.body = finalize_email_body(new_content)
        
        db.commit()
        
        return {
            "template_id": template.template_id,
            "field": request.field,
            "new_content": new_content,
            "subject": template.subject,
            "body": template.body,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[regenerate_email_part] Unexpected error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to regenerate: {str(e)}"
        )


@router.post("/campaign-wizard/update-template")
def update_template_manual(
    request: SaveTemplateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Manually update a template's subject or body after user edit.
    Propagates the change to ALL persona variants of the same sequence step
    so that every prospect in the campaign receives the edited content.
    """
    template = db.query(EmailTemplate).join(
        Campaign, Campaign.campaign_id == EmailTemplate.campaign_id
    ).filter(
        EmailTemplate.template_id == request.template_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found"
        )
        
    if request.subject is not None:
        template.subject = request.subject
    if request.body is not None:

        # template.body = body
        template.body = finalize_email_body(request.body)

    # ── PROPAGATE EDIT TO ALL SIBLING PERSONA TEMPLATES ──────────────────
    # Each sequence step can have one EmailTemplate per persona group
    # (e.g. MARKET_ACCESS, TECHNOLOGY_DATA_DIGITAL, OTHER).  The UI only exposes one
    # template_id to the user, so a manual edit that targets one persona would
    # silently leave every other persona with stale content.  We fix this by
    # copying the edited subject/body to every other template that belongs to
    # the same campaign + sequence step.
    if template.sequence_id:
        siblings = db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == template.campaign_id,
            EmailTemplate.sequence_id == template.sequence_id,
            EmailTemplate.template_id != template.template_id,
        ).all()

        for sibling in siblings:
            if request.subject is not None:
                sibling.subject = request.subject
            if request.body is not None:
                # sibling.body = normalize_paragraph_spacing(request.body)
                sibling.body = template.body

    db.commit()
    
    return {
        "template_id": template.template_id,
        "subject": template.subject,
        "body": template.body,
    }
