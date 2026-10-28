# app/services/batch_processor_service.py
"""
Batch Processor Service.
Auto-classifies all prospects in a list and generates email templates per persona.

Flow:
1. Get all prospects from list
2. Classify each prospect by persona (using LLM or rules)
3. Group prospects by persona
4. Generate 1 email template per persona using LLM (prompts from app/prompts/)
5. Return summary with templates + sample prospects for preview

Prompts: app/prompts/llm_system_prompts.py
"""

from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import uuid

from app.core.clock import utcnow
from app.models import (
    Prospect,
    ProspectPersona,
    EmailTemplate,
    PersonaBlueprint,
)
from app.repositories.personas import Session, SqlAlchemyPersonaCampaignRepository
from app.services.ai_email_service import classify_prospect, PERSONA_DISPLAY_NAMES
from app.services.sender_identity import bind_tenant
from app.services.sequence_generator import AI_FALLBACK_NOTICE, builtin_sequence_emails, generate_sequence_with_llm
from app.prompts.llm_system_prompts import get_sequence_generation_system_prompt
from app.utils.email_utils import normalize_cta_link, has_effective_cta_link, finalize_email_body
from app.utils.email_context_detector import detect_email_context, EMAIL_CONTEXT_CONFERENCE
import logging

logger = logging.getLogger(__name__)


# ============================================================
# BATCH CLASSIFICATION
# ============================================================

def _persona_of(prospect: Prospect, persona_by_prospect_id: Dict[str, ProspectPersona],
                new_personas: List[ProspectPersona]) -> Tuple[str, float]:
    """(persona type, confidence): the stored classification, else a new AUTO_BATCH one (staged in new_personas)."""
    existing = persona_by_prospect_id.get(prospect.prospect_id)
    if existing:
        return existing.persona_type, existing.confidence_score
    persona_type, confidence = classify_prospect(
        designation=prospect.designation or "",
        company_name=prospect.company_name or ""
    )
    new_persona = ProspectPersona(
        persona_id=str(uuid.uuid4()),
        prospect_id=prospect.prospect_id,
        persona_type=persona_type,
        confidence_score=confidence,
        classification_method="AUTO_BATCH",
    )
    new_personas.append(new_persona)
    persona_by_prospect_id[prospect.prospect_id] = new_persona
    return persona_type, confidence


def _preview_row(prospect: Prospect, confidence: float) -> Dict[str, Any]:
    """All of the contact's data, for the wizard's preview."""
    return {
        "prospect_id": prospect.prospect_id,
        "first_name": prospect.first_name or "",
        "last_name": prospect.last_name or "",
        "email": prospect.email,
        "company_name": prospect.company_name or "",
        "designation": prospect.designation or "",
        "industry": prospect.industry or "technology",
        "linkedin_url": prospect.linkedin_url or "",
        "poc_city": prospect.poc_city or "",
        "poc_state": prospect.poc_state or "",
        "poc_country": prospect.poc_country or "",
        "emp_band": prospect.emp_band or "",
        "timezone": prospect.timezone or "",
        "confidence": confidence,
    }


def batch_classify_prospects(
    db: Session,
    list_id: str,
    tenant_id: str,
    excluded_prospect_ids: List[str] | None = None,
) -> Dict[str, List[Dict]]:
    """
    Classify all prospects in a list by persona type.

    `excluded_prospect_ids`, if provided, drops those prospects from the list
    before classification — lets a user deselect specific contacts in the
    wizard rather than always processing the whole list.

    Returns dict grouped by persona type:
    {
        "PATIENT_SERVICES_HUB": [{"prospect_id": ..., "name": ..., "company": ...}, ...],
        "MARKET_ACCESS": [...],
        ...
    }
    """
    repo = SqlAlchemyPersonaCampaignRepository(db)
    prospects = repo.list_contacts(tenant_id, list_id, excluded_prospect_ids)

    if not prospects:
        return {}

    prospect_ids = [prospect.prospect_id for prospect in prospects]
    existing_personas = repo.personas_for(prospect_ids)
    persona_by_prospect_id = {
        persona.prospect_id: persona
        for persona in existing_personas
    }

    # Group by persona; contacts not yet classified are classified now (saved by the caller's commit)
    grouped = defaultdict(list)
    new_personas: List[ProspectPersona] = []
    for prospect in prospects:
        persona_type, confidence = _persona_of(prospect, persona_by_prospect_id, new_personas)
        grouped[persona_type].append(_preview_row(prospect, confidence))

    if new_personas:
        repo.add_all(new_personas)
    
    # NOTE: No db.commit() here — the parent function owns the transaction.
    return dict(grouped)


# ============================================================
# TEMPLATE GENERATION PER PERSONA
# ============================================================

def generate_template_for_persona(
    db: Session,
    campaign_id: str,
    persona_type: str,
    prospects: List[Dict],
    product_name: str,
    campaign_description: str,
    sequence_steps: Optional[list],
    include_first_name_in_subject: bool,
    creative_email: bool,
    email_context: str,
    cta_link: str,
    cta_enabled: bool,
    step_to_seq_id: Dict[int, str],
) -> Dict:
    """
    Generate (or on-demand re-generate) the email template(s) for a single persona.

    `prospects` may be an empty list — prospect_data is always built from literal
    placeholder tokens regardless of real prospect count, so this works both for
    the normal batch path (detected personas) and for on-demand generation of a
    persona with zero classified prospects yet (see ai_email_router.py's
    POST /campaigns/{campaign_id}/personas/{persona_type}/generations).

    Returns a single result dict (LLM success or fallback — never None).
    """
    blueprint = load_persona_blueprint(db, campaign_id, persona_type)
    sequence, fallback_reason = generate_persona_sequence(
        persona_type, blueprint, product_name, campaign_description, sequence_steps,
        include_first_name_in_subject, creative_email, email_context, cta_link)
    return save_persona_templates(
        db, campaign_id, persona_type, prospects, sequence, fallback_reason, sequence_steps,
        include_first_name_in_subject, cta_link, cta_enabled, step_to_seq_id)


# IMPORTANT: the LLM gets ALL available placeholder tokens, so its templates carry every
# {{token}} that can be replaced later
_PLACEHOLDER_PROSPECT = {
    # Basic Info
    "first_name": "{{first_name}}",
    "last_name": "{{last_name}}",
    "full_name": "{{full_name}}",
    # Company Info
    "company_name": "{{company_name}}",
    "designation": "{{designation}}",
    "industry": "{{industry}}",
    # Location - IMPORTANT for local context
    "city": "{{city}}",
    "location": "{{location}}",
    "timezone": "{{timezone}}",
    # Social
    "linkedin_url": "{{linkedin_url}}",
    # Sender Info
    "our_company": "{{our_company}}",
    "your_name": "{{your_name}}",
    # CTA
    "calendar_link": "{{calendar_link}}",
}


def load_persona_blueprint(db: Session, campaign_id: str, persona_type: str) -> Optional[PersonaBlueprint]:
    """Binds the campaign's tenant for prompt branding (its own company name) and returns the
    persona's active blueprint, detached from the session so it stays usable after the read
    transaction ends: the LLM call must not run inside one (T-1)."""
    repo = SqlAlchemyPersonaCampaignRepository(db)
    bind_tenant(repo.campaign_tenant(campaign_id), db)
    return repo.detached_active_blueprint(persona_type)


def generate_persona_sequence(
    persona_type: str,
    blueprint: Optional[PersonaBlueprint],
    product_name: str,
    campaign_description: str,
    sequence_steps: Optional[list],
    include_first_name_in_subject: bool,
    creative_email: bool,
    email_context: str,
    cta_link: str,
) -> tuple:
    """The LLM call for one persona's sequence. Uses no database session (callers end their
    transaction first). Returns (sequence or None, fallback reason or None)."""
    if not blueprint:
        return None, "No persona blueprint for AI generation"
    try:
        sequence = generate_sequence_with_llm(
            prospect_data=dict(_PLACEHOLDER_PROSPECT),
            blueprint=blueprint,
            product_name=product_name,
            product_description=campaign_description,
            num_emails=len(sequence_steps) if sequence_steps else 4,  # Default to 4
            cta_link=cta_link,
            include_first_name_in_subject=include_first_name_in_subject,
            creative_email=creative_email,
            email_context=email_context,
        )
    except Exception as e:
        logger.exception("Error generating template for %s: %s", persona_type, e)
        return None, f"AI generation failed: {e}"
    return sequence, (None if sequence else "AI generation unavailable")


def save_persona_templates(
    db: Session,
    campaign_id: str,
    persona_type: str,
    prospects: List[Dict],
    sequence: Optional[dict],
    fallback_reason: Optional[str],
    sequence_steps: Optional[list],
    include_first_name_in_subject: bool,
    cta_link: str,
    cta_enabled: bool,
    step_to_seq_id: Dict[int, str],
) -> Dict:
    """Adds (does not commit) one template per step from the generated sequence and returns the
    persona's result dict."""
    num_emails = len(sequence_steps) if sequence_steps else 4  # Default to 4
    # Every configured step gets a template: the AI's copy where it produced one, otherwise a
    # built-in starter template (D1: launch needs one template per step).
    builtin = None
    all_emails = []
    fallback_steps = []
    for i in range(1, num_emails + 1):  # email_1 through email_N
        email_data = (sequence or {}).get(f"email_{i}") if isinstance(sequence, dict) else None
        ai_written = isinstance(email_data, dict) and bool(email_data.get("subject") or email_data.get("body"))
        if not ai_written:
            if builtin is None:
                builtin = builtin_sequence_emails(num_emails, cta_enabled, include_first_name_in_subject)
            email_data = builtin[i - 1]
            fallback_steps.append(i)

        # Save each email as a template with step number
        template = EmailTemplate(
            template_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            sequence_id=step_to_seq_id.get(i),  # Link to sequence
            designation=persona_type,
            subject=email_data.get("subject", ""),  # type: ignore[union-attr]  # set on both branches above
            body=finalize_email_body(email_data.get("body", "")),  # type: ignore[union-attr]  # set on both branches above
            cta_text=email_data.get("cta", ""),  # type: ignore[union-attr]  # set on both branches above
            cta_link=cta_link,
            is_ai_generated=ai_written,
            ai_model="gpt-4o-mini" if ai_written else None,
            model_settings={
                "persona": persona_type,
                "method": "LLM_AUTO" if ai_written else "BUILTIN_FALLBACK",
                "step_number": i,
                "schedule_day": sequence_steps[i-1]["wait_days"] if sequence_steps and len(sequence_steps) >= i else (i-1)*2
            },
            generated_at=utcnow(),
        )
        SqlAlchemyPersonaCampaignRepository(db).add(template)

        # Calculate cumulative day (Day 1, then add wait_days for each subsequent step)
        # wait_days = delay after previous email, so we need to sum all previous wait_days
        cumulative_day = 1  # Start at Day 1
        if sequence_steps and len(sequence_steps) >= i:
            for step_idx in range(i):  # Sum wait_days from step 0 to current step
                cumulative_day += sequence_steps[step_idx].get("wait_days", 0)
        else:
            cumulative_day = 1 + (i-1) * 2  # Default: Day 1, 3, 5, 7

        all_emails.append({
            "step_number": i,
            "template_id": template.template_id,
            "subject": email_data.get("subject", ""),  # type: ignore[union-attr]  # set on both branches above
            "body": email_data.get("body", ""),  # type: ignore[union-attr]  # set on both branches above
            "cta": email_data.get("cta", ""),  # type: ignore[union-attr]  # set on both branches above
            "cta_link": cta_link,
            "schedule": f"Day {cumulative_day}",
            "is_ai_generated": ai_written,
        })

    result = {
        "persona_type": persona_type,
        "persona_label": PERSONA_DISPLAY_NAMES.get(persona_type, persona_type),
        "prospect_count": len(prospects),
        "template_id": all_emails[0]["template_id"],
        "template": all_emails[0],  # First email for backwards compatibility
        "all_emails": all_emails,   # All emails in sequence
        "sequence_count": len(all_emails),
        "fallback_used": bool(fallback_steps),
        "fallback_steps": fallback_steps,
    }
    if fallback_steps:
        result["notice"] = AI_FALLBACK_NOTICE
        result["error"] = fallback_reason or "AI generation returned no email for some steps"
    return result


def generate_templates_per_persona(
    db: Session,
    campaign_id: str,
    tenant_id: str,
    grouped_prospects: Dict[str, List[Dict]],
    product_name: str = "your solution",
    company_name: str = "your company",
    campaign_description: str = "",
    sequence_steps: list | None = None,  # User-configured sequence timing
    include_first_name_in_subject: bool = False,  # Include {{first_name}} in subject
    creative_email: bool = False,  # Apply creative writing style instructions
    email_context: str = "COLD_OUTREACH",  # Auto-detected if not passed
) -> List[Dict]:
    """
    Generate one email template per persona type using LLM.

    Returns list of generated templates:
    [
        {
            "persona_type": "PATIENT_SERVICES_HUB",
            "prospect_count": 127,
            "template": {
                "subject": "...",
                "body": "...",
                "cta": "..."
            }
        },
        ...
    ]
    """
    # Auto-detect email context from campaign name + description if not explicitly provided
    if email_context == "COLD_OUTREACH":
        email_context = detect_email_context(product_name, campaign_description)

    # Clear any existing templates for this campaign to prevent duplicates on re-generation.
    # Do not commit here — the caller owns the transaction and will roll back everything
    # if generation fails later in the flow.
    repo = SqlAlchemyPersonaCampaignRepository(db)
    repo.delete_campaign_templates(campaign_id)

    # Fetch campaign and existing sequences to link templates
    campaign = repo.campaign(campaign_id)
    cta_link = normalize_cta_link(campaign.cta_link if campaign else None)
    cta_enabled = has_effective_cta_link(cta_link)

    sequences = repo.sequences(campaign_id)
    step_to_seq_id = {s.step_number: s.sequence_id for s in sequences}

    results = []
    for persona_type, prospects in grouped_prospects.items():
        if len(prospects) == 0:
            continue
        result = generate_template_for_persona(
            db, campaign_id, persona_type, prospects,
            product_name, campaign_description, sequence_steps,
            include_first_name_in_subject, creative_email, email_context,
            cta_link, cta_enabled, step_to_seq_id,
        )
        results.append(result)

    # NOTE: No db.commit() here — the parent function owns the transaction.
    return results


# ============================================================
# FULL PROCESS FLOW
# ============================================================

def process_prospect_list(
    db: Session,
    list_id: str,
    campaign_id: str,
    tenant_id: str,
    product_name: str = "your solution",
    company_name: str = "your company",
    campaign_description: str = "",
    sequence_steps: list | None = None,  # User-configured sequence timing
    include_first_name_in_subject: bool = False,  # Include {{first_name}} in subject
    creative_email: bool = False,  # Apply creative writing style instructions
    excluded_prospect_ids: list | None = None,  # Contacts deselected in the wizard
) -> Dict:
    """
    Full processing flow (ATOMIC — all-or-nothing):
    1. Classify all prospects
    2. Generate templates per persona
    3. Commit everything in one transaction
    
    If any step fails, the entire batch is rolled back so the
    campaign is never left in a half-processed state.
    """
    # Default sequence if not provided: Day 0, 2, 4, 6 (cumulative wait days)
    if not sequence_steps:
        sequence_steps = [
            {"step_number": 1, "wait_days": 0, "type": "Initial Email"},
            {"step_number": 2, "wait_days": 2, "type": "Follow-up 1"},
            {"step_number": 3, "wait_days": 4, "type": "Follow-up 2"},
            {"step_number": 4, "wait_days": 6, "type": "Follow-up 3"},
        ]
    
    try:
        # Step 1: Batch classify (adds ProspectPersona rows — no commit yet)
        grouped = batch_classify_prospects(db, list_id, tenant_id, excluded_prospect_ids)
        
        # Step 2: Generate templates (adds EmailTemplate rows — no commit yet)
        templates = generate_templates_per_persona(
            db=db,
            campaign_id=campaign_id,
            tenant_id=tenant_id,
            grouped_prospects=grouped,
            product_name=product_name,
            company_name=company_name,
            campaign_description=campaign_description,
            sequence_steps=sequence_steps,
            include_first_name_in_subject=include_first_name_in_subject,
            creative_email=creative_email,
        )
        
        # All steps succeeded — commit the entire batch atomically
        db.commit()
        
    except Exception as e:
        # Something failed — roll back everything (classifications + templates)
        db.rollback()
        logger.exception("Batch processing failed, rolled back all changes: %s", e)
        raise
    
    # Build summary (after successful commit)
    total_prospects = sum(len(p) for p in grouped.values())
    personas_detected = list(grouped.keys())
    
    # Include sample prospects for each persona (for preview)
    sample_prospects = {}
    for persona_type, prospects in grouped.items():
        # Take first 3 prospects as samples for preview
        sample_prospects[persona_type] = prospects[:3]
    
    return {
        "list_id": list_id,
        "campaign_id": campaign_id,
        "total_prospects": total_prospects,
        "personas_detected": personas_detected,
        "persona_breakdown": {k: len(v) for k, v in grouped.items()},
        "templates_generated": templates,
        "sample_prospects": sample_prospects,
        "creative_email_used": creative_email,
        # D1: tell the wizard when starter templates stood in for AI copy
        "ai_fallback_used": any(t.get("fallback_used") for t in templates),
        "notice": AI_FALLBACK_NOTICE if any(t.get("fallback_used") for t in templates) else None,
    }
