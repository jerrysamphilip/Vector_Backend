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

from typing import Dict, List
from collections import defaultdict
from datetime import datetime
from sqlalchemy.orm import Session
import uuid

from app.models import (
    Prospect,
    ProspectListMember,
    ProspectPersona,
    EmailTemplate,
    EmailSequence,
    PersonaBlueprint,
    Campaign,
)
from app.services.ai_email_service import classify_prospect, PERSONA_DISPLAY_NAMES
from app.services.sequence_generator import generate_sequence_with_llm
from app.prompts.llm_system_prompts import get_sequence_generation_system_prompt
from app.utils.email_utils import normalize_cta_link, has_effective_cta_link, finalize_email_body
from app.utils.email_context_detector import detect_email_context, EMAIL_CONTEXT_CONFERENCE


# ============================================================
# BATCH CLASSIFICATION
# ============================================================

def batch_classify_prospects(
    db: Session,
    list_id: str,
    tenant_id: str,
    excluded_prospect_ids: List[str] = None,
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
    query = (
        db.query(Prospect)
        .join(ProspectListMember, ProspectListMember.prospect_id == Prospect.prospect_id)
        .filter(
            ProspectListMember.list_id == list_id,
            Prospect.tenant_id == tenant_id,
        )
    )
    if excluded_prospect_ids:
        query = query.filter(Prospect.prospect_id.notin_(excluded_prospect_ids))
    prospects = query.all()

    if not prospects:
        return {}

    prospect_ids = [prospect.prospect_id for prospect in prospects]
    existing_personas = (
        db.query(ProspectPersona)
        .filter(ProspectPersona.prospect_id.in_(prospect_ids))
        .all()
    )
    persona_by_prospect_id = {
        persona.prospect_id: persona
        for persona in existing_personas
    }

    # Group by persona
    grouped = defaultdict(list)
    new_personas = []
    
    for prospect in prospects:
        # Check if already classified
        existing_persona = persona_by_prospect_id.get(prospect.prospect_id)
        
        if existing_persona:
            persona_type = existing_persona.persona_type
            confidence = existing_persona.confidence_score
        else:
            # Classify using AI
            persona_type, confidence = classify_prospect(
                designation=prospect.designation or "",
                company_name=prospect.company_name or ""
            )
            
            # Save classification
            new_persona = ProspectPersona(
                persona_id=str(uuid.uuid4()),
                prospect_id=prospect.prospect_id,
                persona_type=persona_type,
                confidence_score=confidence,
                classification_method="AUTO_BATCH",
            )
            new_personas.append(new_persona)
            persona_by_prospect_id[prospect.prospect_id] = new_persona
        
        # Include ALL prospect data for preview functionality
        grouped[persona_type].append({
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
        })

    if new_personas:
        db.add_all(new_personas)
    
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
    sequence_steps: list,
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
    # Get blueprint for this persona
    blueprint = db.query(PersonaBlueprint).filter(
        PersonaBlueprint.persona_type == persona_type,
        PersonaBlueprint.is_active == True
    ).first()

    # Generate template using LLM
    try:
        # IMPORTANT: Pass ALL available placeholder tokens
        # This ensures the LLM outputs templates with ALL {{tokens}} that can be replaced later
        prospect_data = {
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

        # Call LLM if blueprint exists
        num_emails = len(sequence_steps) if sequence_steps else 4  # Default to 4
        sequence = None
        if blueprint:
            sequence = generate_sequence_with_llm(
                prospect_data=prospect_data,
                blueprint=blueprint,
                product_name=product_name,
                product_description=campaign_description,
                num_emails=num_emails,
                cta_link=cta_link,
                include_first_name_in_subject=include_first_name_in_subject,
                creative_email=creative_email,
                email_context=email_context,
            )

        # Process emails from sequence - only as many as user configured
        all_emails = []
        if sequence:
            for i in range(1, num_emails + 1):  # email_1 through email_N
                email_key = f"email_{i}"
                if email_key in sequence:
                    email_data = sequence[email_key]

                    # Save each email as a template with step number
                    template = EmailTemplate(
                        template_id=str(uuid.uuid4()),
                        campaign_id=campaign_id,
                        sequence_id=step_to_seq_id.get(i), # Link to sequence
                        designation=persona_type,
                        subject=email_data.get("subject", ""),
                        body=finalize_email_body(email_data.get("body", "")),
                        cta_text=email_data.get("cta", ""),
                        cta_link=cta_link,
                        is_ai_generated=True,
                        ai_model="gpt-4o-mini",
                        model_settings={
                            "persona": persona_type,
                            "method": "LLM_AUTO",
                            "step_number": i,
                            "schedule_day": sequence_steps[i-1]["wait_days"] if sequence_steps and len(sequence_steps) >= i else (i-1)*2
                        },
                        generated_at=datetime.utcnow(),
                    )
                    db.add(template)

                    # Calculate cumulative day (Day 1, then add wait_days for each subsequent step)
                    # wait_days = delay after previous email, so we need to sum all previous wait_days
                    cumulative_day = 1  # Start at Day 1
                    if sequence_steps and len(sequence_steps) >= i:
                        for step_idx in range(i):  # Sum wait_days from step 0 to current step
                            cumulative_day += sequence_steps[step_idx].get("wait_days", 0)
                    else:
                        cumulative_day = 1 + (i-1) * 2  # Default: Day 1, 3, 5, 7

                    schedule_label = f"Day {cumulative_day}"

                    all_emails.append({
                        "step_number": i,
                        "template_id": template.template_id,
                        "subject": email_data.get("subject", ""),
                        "body": email_data.get("body", ""),
                        "cta": email_data.get("cta", ""),
                        "cta_link": cta_link,
                        "schedule": schedule_label
                    })

        if all_emails:
            return {
                "persona_type": persona_type,
                "persona_label": PERSONA_DISPLAY_NAMES.get(persona_type, persona_type),
                "prospect_count": len(prospects),
                "template": all_emails[0],  # First email for backwards compatibility
                "all_emails": all_emails,   # All emails in sequence
                "sequence_count": len(all_emails)
            }
        else:
            # No LLM result - use fallback
            raise Exception("No LLM result available")

    except Exception as e:
        print(f"Error generating template for {persona_type}: {e}")
        # Fallback - use generic template
        fallback_subject = f"Question for {{{{first_name}}}}"
        fallback_body = f"""Hi {{{{first_name}}}},
I noticed your role at {{{{company_name}}}} and thought you might be interested in {product_name}.
"""
        fallback_cta_text = ""
        if cta_enabled:
            fallback_cta_text = "Would you be open to a quick chat?"
            fallback_body = f"{fallback_body}\n{fallback_cta_text}: {cta_link}"
        fallback_body = f"{fallback_body}\n\nRegards,\n{{{{signature_block}}}}"

        fallback_template = EmailTemplate(
            template_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            sequence_id=step_to_seq_id.get(1), # Link to first step sequence
            designation=persona_type,
            subject=fallback_subject,
            body=finalize_email_body(fallback_body),
            cta_text=fallback_cta_text,
            cta_link=cta_link,
            is_ai_generated=False,
            model_settings={"persona": persona_type, "method": "FALLBACK"},
            generated_at=datetime.utcnow(),
        )
        db.add(fallback_template)

        fallback_email = {
            "step_number": 1,
            "template_id": fallback_template.template_id,
            "subject": fallback_subject,
            "body": fallback_body,
            "cta": fallback_cta_text,
            "cta_link": cta_link,
            "schedule": "Day 1",
        }
        return {
            "persona_type": persona_type,
            "persona_label": PERSONA_DISPLAY_NAMES.get(persona_type, persona_type),
            "prospect_count": len(prospects),
            "template_id": fallback_template.template_id,
            "template": fallback_email,  # kept for backwards compatibility
            "all_emails": [fallback_email],
            "sequence_count": 1,
            "error": str(e)
        }


def generate_templates_per_persona(
    db: Session,
    campaign_id: str,
    tenant_id: str,
    grouped_prospects: Dict[str, List[Dict]],
    product_name: str = "your solution",
    company_name: str = "your company",
    campaign_description: str = "",
    sequence_steps: list = None,  # User-configured sequence timing
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
    db.query(EmailTemplate).filter(
        EmailTemplate.campaign_id == campaign_id
    ).delete(synchronize_session=False)

    # Fetch campaign and existing sequences to link templates
    campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    cta_link = normalize_cta_link(campaign.cta_link if campaign else None)
    cta_enabled = has_effective_cta_link(cta_link)

    sequences = db.query(EmailSequence).filter(
        EmailSequence.campaign_id == campaign_id
    ).all()
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
    sequence_steps: list = None,  # User-configured sequence timing
    include_first_name_in_subject: bool = False,  # Include {{first_name}} in subject
    creative_email: bool = False,  # Apply creative writing style instructions
    excluded_prospect_ids: list = None,  # Contacts deselected in the wizard
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
        print(f"Batch processing failed, rolled back all changes: {e}")
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
    }
