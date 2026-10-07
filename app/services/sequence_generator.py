# app/services/sequence_generator.py
"""
AI-Powered Multi-Step Email Sequence Generator.
Creates unique content for 3-email follow-up sequences:
  - Email 1 (Day 1): Introduction
  - Email 2 (Day 3): Reminder with social proof
  - Email 3 (Day 7): Social proof + respectful close

Prompts are loaded from: app/prompts/
"""

from typing import Dict, List, Optional
from sqlalchemy.orm import Session
import json
import random
import re
import logging

from openai import OpenAI

from app.core.config import settings
from app.models import PersonaBlueprint, Prospect
from app.services.ai_email_service import classify_prospect, openai_client
from app.prompts.llm_system_prompts import get_sequence_generation_system_prompt
from app.prompts.email_examples import (
    get_few_shot_examples_block,
    is_healthcare_pharma,
    build_persona_intelligence_block,
    get_capability_pool_block,
    get_sequence_tone_block,
    get_conference_few_shot_examples_block,
    get_conference_sequence_tone_block,
)
from app.utils.email_context_detector import detect_email_context, EMAIL_CONTEXT_CONFERENCE
from app.utils.email_utils import (
    normalize_unsubscribe_footer,
    strip_cta_content_no_link,
    ensure_core_personalization_tokens,
    normalize_cta_link,
    has_effective_cta_link,
    normalize_paragraph_spacing,
    finalize_email_body,
)
from app.utils.retry_utils import retry_with_backoff

logger = logging.getLogger(__name__)


# ============================================================
# SEQUENCE TEMPLATES (Fallback when LLM unavailable)
# ============================================================

SEQUENCE_TEMPLATES = {
    "TECHNOLOGY_DATA_DIGITAL": {
        "email_1": {
            "subject_prefix": "{{company_name}}'s",
            "purpose": "intro",
            "angle": "workflow_observation"
        },
        "email_2": {
            "subject_prefix": "one more angle —",
            "purpose": "new_angle",
            "angle": "hidden_cost"
        },
        "email_3": {
            "subject_prefix": "closing the loop,",
            "purpose": "respectful_close",
            "angle": "clean_exit"
        }
    },
    "MARKET_ACCESS": {
        "email_1": {
            "subject_prefix": "{{company_name}}'s",
            "purpose": "intro",
            "angle": "workflow_observation"
        },
        "email_2": {
            "subject_prefix": "one more angle —",
            "purpose": "new_angle",
            "angle": "social_proof"
        },
        "email_3": {
            "subject_prefix": "not the right time,",
            "purpose": "respectful_close",
            "angle": "clean_exit"
        }
    }
}


# ============================================================
# CTA VARIATIONS (7 per context)
# ============================================================

CTA_VARIATIONS = {
    # Research: interest CTAs convert 2x better than booking asks on first touch (Gong)
    # Under 6 words, single ask, no pressure
    "intro": [
        "Worth exploring?",
        "Is this on your radar?",
        "Relevant to what you're working on?",
        "Worth a look?",
        "Does this resonate?",
        "Open to a quick exchange?",
        "Curious if this fits?"
    ],
    "reminder": [
        "Still relevant for your team?",
        "Want me to send a short one-pager?",
        "Worth a 10-minute call?",
        "Does this apply to your situation?",
        "Any questions I can answer?",
        "Worth revisiting?",
        "Open to hearing more?"
    ],
    # Breakup emails: loss aversion + clean exit (Instantly data: 33% response rate)
    "last_chance": [
        "Should I close this out?",
        "Should I circle back next quarter?",
        "Not the right time — or not relevant?",
        "Would you prefer I stop following up?",
        "Worth one more look, or should I close the loop?",
        "Is the timing off, or is this not a fit?",
        "Should I reach out again later?"
    ]
}

# ============================================================
# SEQUENCE GENERATION WITH LLM
# ============================================================

def generate_sequence_with_llm(
    prospect_data: Dict,
    blueprint: PersonaBlueprint,
    product_name: str,
    product_description: str = "",
    num_emails: int = 4,  # User-configured email count
    cta_link: str = "",
    include_first_name_in_subject: bool = False,  # Include {{first_name}} in subject
    creative_email: bool = False,  # Apply creative writing style instructions
    email_context: str = "COLD_OUTREACH",  # "COLD_OUTREACH" | "CONFERENCE_PREOUTREACH"
) -> Optional[Dict[str, Dict]]:
    """
    Generate email sequence using GPT-4o-mini.
    Uses prompts from app/prompts/llm_system_prompts.py with Neutrino-style examples.
    
    Args:
        num_emails: Number of emails to generate (1-7), based on user sequence config
        include_first_name_in_subject: If True, include {{first_name}} token in subject lines
    
    Returns:
        Dict with email_1, email_2, etc. keys, each containing subject, body, cta
    """
    if not openai_client:
        return None

    cta_link = normalize_cta_link(cta_link)
    cta_enabled = has_effective_cta_link(cta_link)

    tone = blueprint.tone_rules.get("style", "professional") if blueprint.tone_rules else "professional"
    avoid_list = blueprint.tone_rules.get("avoid", []) if blueprint.tone_rules else []
    persona_intelligence_block = build_persona_intelligence_block(blueprint)

    is_conference = (email_context == EMAIL_CONTEXT_CONFERENCE)
    healthcare_mode = True  # Apply Neutrino outreach style to all industries

    # Use the centralized prompt from app/prompts/
    system_prompt = get_sequence_generation_system_prompt(
        prospect_data=prospect_data,
        product_name=product_name,
        product_description=product_description,
        tone=tone,
        avoid_list=avoid_list,
        creative_email=creative_email,
        cta_enabled=cta_enabled,
        industry=prospect_data.get("industry", ""),
        email_context=email_context,
    )

    if is_conference:
        capability_pool_block = get_capability_pool_block()
        sequence_tone_block = get_conference_sequence_tone_block(num_emails)
        examples_block = get_conference_few_shot_examples_block(
            industry=prospect_data.get("industry", ""),
            limit=2,
        )
    else:
        capability_pool_block = get_capability_pool_block()
        sequence_tone_block = get_sequence_tone_block(num_emails)
        examples_block = None

    # Build dynamic JSON structure based on num_emails
    email_json_parts = ",\n    ".join([
        f'"email_{i}": {{"subject": "...", "body": "...", "cta": "..."}}'
        for i in range(1, num_emails + 1)
    ])
    
    # Build context-aware user prompt
    # Prioritize product_description for email content
    # Use blueprint for tone/style guidance ONLY (not specific text)
    context_hint = ""
    if product_description and product_description.strip():
        context_hint = f"""
=== CAMPAIGN CONTEXT (USE THIS TO WRITE UNIQUE CONTENT) ===
{product_description}

Base ALL emails on this context. Each email should have a unique angle but stay focused on this product/service."""
    
    # Extract tone guidance from blueprint (not specific openers)
    tone_style = "professional"
    avoid_items = []
    if blueprint.tone_rules:
        tone_style = blueprint.tone_rules.get("style", "professional")
        avoid_items = blueprint.tone_rules.get("avoid", [])
    
    avoid_str = ", ".join(avoid_items) if avoid_items else "generic phrases, spam triggers"
    
    # Subject line personalization instruction
    subject_instruction = ""
    if include_first_name_in_subject:
        subject_instruction = """
IMPORTANT: Include {{first_name}} in EVERY subject line.
   Examples: "{{first_name}}, {{company_name}}'s [specific process]", "{{first_name}}, quick thought on [topic]"
   NEVER: "Quick question, {{first_name}}" or "worth a look, {{first_name}}?" (spam triggers — banned)"""
    
    creative_block = ""
    if creative_email:
        creative_block = """
ADDITIONAL CREATIVE MODE RULES:
- Write with vivid, concrete language (not abstract buzzwords)
- Use one fresh, industry-relevant angle per email
- Avoid generic AI phrases and repetitive structures
- Keep it natural and professional (not poetic or dramatic)
"""

    creative_subject_instruction = ""
    if include_first_name_in_subject:
        creative_subject_instruction = """
- IMPORTANT: Insert {{first_name}} or {{company_name}} exactly once in the subject line (no raw merge tags)."""

    if is_conference:
        conf_examples = examples_block or ""
        user_prompt = f"""Generate a {num_emails}-email Neutrino Tech Systems conference/in-person outreach sequence.
{context_hint}

CRITICAL: This is a conference/in-person outreach sequence — NOT cold outreach.
Read the campaign description carefully to identify:
- Sub-type: Named event (Conference) OR rep visiting a city (In-Person Visit)
- Event name (ONLY if explicitly mentioned in the description) / visit city and state
- Event dates / visit date window
- Neutrino contact person: name and title

NEVER fabricate or invent a conference/event name. If no specific event is named in the description,
this is an In-Person Visit — use "In-Person Meeting" in subjects and reference the city/dates only.

Apply the PER-EMAIL TONE GUIDE below exactly for each step's opener, bullet style, and CTA.

Follow the CONFERENCE EMAIL STRUCTURE from the system prompt for every email:
1) OPENER — follow the opener style in the PER-EMAIL TONE GUIDE for this step
2) Company intro — "Neutrino Tech Systems" (NEVER use {{{{our_company}}}})
3) Capability bullets — follow the bullet style in the tone guide (themes vs service vs technical depth)
4) DUAL CTA — in-person first, virtual fallback. NEVER single-option.

CRITICAL QUALITY RULE:
- DO NOT copy the example openers, CTAs, or bullet text from the reference or tone guide below verbatim.
- Each email must have UNIQUE wording. The examples show STRUCTURE and TONE only.
- Write fresh, prospect-specific content — the reference shows what KIND of content to write, not the exact words.
- If the tone guide says "warm compliment opener" — write YOUR OWN compliment, not the example's.

Rules:
- Plain text only (no HTML tags)
- 150–250 words per email
- Subject lines: Lead with a VALUE PROPOSITION + event name. Title Case. Each email MUST have a DIFFERENT topic-specific subject. BANNED in subjects: "quick catch up", "let's catch up", "let's meet"
{"- Include {{first_name}}, prefix in EVERY subject line." if include_first_name_in_subject else "- Do NOT include {{first_name}} in subject lines."}
- NEVER use {{{{our_company}}}} — always write "Neutrino Tech Systems"
- "Following up", "would love to", "looping back" are ALLOWED in body
- Generate EXACTLY {num_emails} emails
- First line must be exactly: "Hi {{{{first_name}}}},"
- Do not include unsubscribe/footer text; system appends one standard line.

{conf_examples}

{persona_intelligence_block}

{capability_pool_block}

{sequence_tone_block}

Return JSON:
{{
    {email_json_parts}
}}"""
    elif creative_email:
        user_prompt = f"""Generate a {num_emails}-email Neutrino Tech Systems cold outreach sequence.
{context_hint}

CRITICAL: Apply the PER-EMAIL TONE GUIDE below exactly. Each email step has a DIFFERENT opener style:
- email_1 (intro): Open with a direct pain point question.
- email_2+ (follow-ups): Do NOT open with a question — use the urgency statement or follow-up phrase from the tone guide.

Follow the NEUTRINO EMAIL STRUCTURE from the system prompt for every email:
1) OPENER — follow the opener style in the PER-EMAIL TONE GUIDE for this step's position
2) Company intro — "Neutrino Tech Systems" with credibility markers (NEVER use {{{{our_company}}}})
3) Capability bullets (4–5 bullets, minimum 4, maximum 5) — use Neutrino's REAL services from the SERVICE REFERENCE below.
   DO NOT invent generic bullets like "optimize workflows" or "ensure data integrity".
4) CTA — follow the CTA style in the PER-EMAIL TONE GUIDE for this step's position

Rules:
- Plain text only (no HTML tags)
- 150–250 words per email
- Bullet lists required for capabilities section
- Use tokens: {{{{first_name}}}}, {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}, {{{{linkedin_url}}}}, {{{{your_name}}}}
- NEVER use {{{{our_company}}}} — always write "Neutrino Tech Systems"
- Include at least THREE tokens naturally: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}
- Never write metadata-like lines such as "Prospect context used:".
- NEVER use: "many teams face", "many {{{{industry}}}} teams", "in today's landscape", "leverage", "synergy", "AI-powered", "cutting-edge", "streamline operations", "I'd love to", "excited to share"
- Generate EXACTLY {num_emails} emails
- {"CTA link: " + cta_link if cta_enabled else "No CTA link. Do NOT include any CTA sentence, meeting ask, booking link, or URL."}
- First line must be exactly: "Hi {{{{first_name}}}},"
- Do not include unsubscribe/footer text; system appends one standard line.
{creative_subject_instruction}

{persona_intelligence_block}

{capability_pool_block}

{sequence_tone_block}

Return JSON:
{{
    {email_json_parts}
}}"""
    else:
        user_prompt = f"""Generate a {num_emails}-email Neutrino Tech Systems cold outreach sequence.
{context_hint}

Target persona: {blueprint.persona_type}
Tone: {tone_style}

CRITICAL: Apply the PER-EMAIL TONE GUIDE below exactly. Each email step has a DIFFERENT opener style:
- email_1 (intro): Open with a direct pain point question.
- email_2+ (follow-ups): Do NOT open with a question — use the urgency statement or follow-up phrase from the tone guide.

Follow the NEUTRINO EMAIL STRUCTURE from the system prompt for every email:
1) OPENER — follow the opener style in the PER-EMAIL TONE GUIDE for this step's position
2) Company intro — "Neutrino Tech Systems" with credibility markers (NEVER use {{{{our_company}}}})
3) Capability bullets (4–5 bullets, minimum 4, maximum 5) — use Neutrino's REAL services from the SERVICE REFERENCE below.
   DO NOT invent generic bullets like "optimize workflows" or "ensure data integrity".
4) CTA — follow the CTA style in the PER-EMAIL TONE GUIDE for this step's position

=== REQUIREMENTS ===
1. Plain text only (no HTML tags).
2. 150–250 words per email.
3. Bullet lists required for capabilities section.
4. Each email: DIFFERENT opener angle — follow the tone guide, do NOT repeat opening styles.
5. Generate EXACTLY {num_emails} emails.
6. {"CTA Link to use: " + cta_link + " — end each email with a direct meeting/call ask." if cta_enabled else "No CTA link. Do NOT include any CTA sentence, meeting ask, booking link, or URL."}
7. NEVER use {{{{our_company}}}} — always write "Neutrino Tech Systems".
8. Include tokens naturally: {{{{first_name}}}}, {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}, {{{{your_name}}}}
9. Include at least THREE tokens per email: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}
10. Never write metadata-like lines such as "Prospect context used:".
11. Subject lines: 3–7 words, descriptive, avoid spam triggers.
12. First line of every email must be exactly: "Hi {{{{first_name}}}},"
13. Do NOT include unsubscribe/footer text; system appends it automatically.
{subject_instruction}

{persona_intelligence_block}

{capability_pool_block}

{sequence_tone_block}

Return JSON:
{{
    {email_json_parts}
}}"""

    temperature = 0.85 if creative_email else 0.7

    def _call_openai():
        return openai_client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temperature,
            max_tokens=3000 if healthcare_mode else 2000,
            response_format={"type": "json_object"}
        )

    try:
        response = retry_with_backoff(
            _call_openai,
            max_attempts=3,
            base_delay=1.0,
            max_delay=30.0,
        )

        if response is None:
            logger.error("[generate_email_sequence_with_llm] All retry attempts exhausted. Returning None.")
            return None
        
        result = json.loads(response.choices[0].message.content)
        
        # Post-process: Append signatures to each email
        for i in range(1, num_emails + 1):
            key = f"email_{i}"
            if key in result:
                subject = (result[key].get("subject") or "").strip()
                if include_first_name_in_subject:
                    if "{{first_name}}" not in subject and "{first_name}" not in subject:
                        subject = f"{{{{first_name}}}}, {subject}" if subject else "{{{{company_name}}}}'s workflow"
                result[key]["subject"] = subject

                body = result[key].get("body", "")
                closing = result[key].get("closing_style", "Best")
                
                # Clean up any LLM-hallucinated signatures before appending our official one
                body = re.sub(r'(?i)(<p>|^\s*|<br\s*/?>)*\s*(Best(\s+Regards)?|Sincerely|Regards|Thanks|Cheers)?,?(\s*<br\s*/?>)?\s*(\{|\[)*your_name(\}|\])*(<\/p>)?\s*$', '', body).strip()
                body = re.sub(r'(?i)\n+\s*(Best(\s+Regards)?|Sincerely|Regards|Thanks|Cheers),?\s*$', '', body).strip()

                # Remove spurious extra greeting lines the LLM sometimes inserts right
                # after the opening 'Hi {{first_name}},' line, e.g. 'hy there', 'Hey,'.
                # These are short standalone lines (<= 6 words) starting with a greeting word.
                body = re.sub(
                    r'(?m)^(Hi \{\{first_name\}\},)\n+((?:[ \t]*(hey|hi|hello|howdy|hy|greetings)[^\n]{0,35}\n+)+)',
                    r'\1\n\n',
                    body,
                    flags=re.IGNORECASE,
                )
                
                # CTA Stitcher: Ensure the CTA link is present
                # Skip for conference emails — dual CTA format (in-person + virtual) is handled
                # by the prompt, and appending "CTA text: URL" breaks the dual CTA structure.
                link_to_check = cta_link if cta_enabled else ""

                # Sanitize CTA text (LLM sometimes puts HTML here)
                cta_text_raw = result[key].get("cta", "Schedule a quick call")
                cta_text_clean = re.sub(r'<[^>]+>', '', cta_text_raw).strip()
                result[key]["cta"] = cta_text_clean if cta_enabled else ""

                # The LLM already writes a CTA sentence in the body — only append the link, never repeat the CTA text.
                if not is_conference and cta_enabled and link_to_check not in body and "{{calendar_link}}" not in body and "{calendar_link}" not in body:
                    body = f"{body}\n\nLink to find time: {link_to_check}"
                elif not cta_enabled:
                    body = strip_cta_content_no_link(body)

                # body = ensure_core_personalization_tokens(body)

                # Enforce paragraph spacing (blank lines between paragraphs,
                # no blank lines between bullet point items).
                # body = normalize_paragraph_spacing(body)

                # Unconditionally re-append the pristine signature
                signoff_word = closing if closing else "Regards"
                if not signoff_word.endswith(","):
                    signoff_word += ","
                result[key]["body"] = f"{body}\n\n{signoff_word}\n{{{{signature_block}}}}"

                # Normalize unsubscribe/footer text so creative and generic outputs are consistent.
                # result[key]["body"] = normalize_unsubscribe_footer(result[key]["body"])
                result[key]["body"] = finalize_email_body(result[key]["body"])
        
        # Add metadata
        result["model_used"] = settings.OPENAI_MODEL
        result["tokens_used"] = response.usage.total_tokens if response.usage else 0
        result["generation_method"] = "LLM"
        result["persona_type"] = blueprint.persona_type
        
        return result
        
    except Exception as e:
        logger.error("[generate_email_sequence_with_llm] Unexpected error: %s", e)
        return None



def generate_sequence_from_blueprint(
    prospect_data: Dict,
    blueprint: PersonaBlueprint,
    product_name: str,
    cta_link: str = "",
    is_conference: bool = False,
) -> Dict[str, Dict]:
    """
    Generate a 3-email sequence using blueprint templates (no LLM).
    """
    first_name = prospect_data.get('first_name', 'there')
    company = prospect_data.get('company_name', 'your company')
    
    # Get random options from blueprint
    openers = blueprint.openers or ["Hello,"]
    value_angles = blueprint.value_angles or ["improve efficiency"]
    proof_points = blueprint.proof_points or []
    
    cta_link = normalize_cta_link(cta_link)
    cta_enabled = has_effective_cta_link(cta_link)

    # Never expose raw campaign/product name in email copy — use {{our_company}} token instead
    safe_product_ref = "{{{{our_company}}}}"

    if is_conference:
        # Conference blueprint fallback: event-aware CTAs, dual in-person + virtual
        cta_1 = "Please let me know your availability for an In-Person meeting at the event. If not attending, we could also connect virtually at your convenient time." if cta_enabled else ""
        cta_2 = "Looking forward to catching up at the event or virtually. Please direct to the relevant stakeholder if you won't be attending." if cta_enabled else ""
        cta_3 = "Happy to catch up for a quick coffee at the event or connect virtually — whichever's easier for you." if cta_enabled else ""

        email_1 = {
            "subject": f"Accelerating Innovation for {{{{company_name}}}} — Meet at the Event",
            "body": f"""Hi {{{{first_name}}}},

Really impressive how {{{{company_name}}}} has been advancing patient access across specialty markets!

I am reaching out as our team will be attending the upcoming event and would love to connect. As a way of introduction — we at Neutrino Tech Systems are an AI-first healthcare tech company based in the U.S., with teams in Costa Rica and India. We help Pharma Hubs and Specialty Pharmacies like {{{{company_name}}}} through Automation, AI, Data Engineering, Cloud, DevOps, Quality Engineering, Custom App Development, and Salesforce solutions.

Would love to catch up at the event or virtually.

{cta_1}

Regards,
{{{{signature_block}}}}""",
            "cta": cta_1
        }

        email_2 = {
            "subject": f"Enhancing Patient Experience — Connect at the Event",
            "body": f"""Hi {{{{first_name}}}},

Just following up to see if your plans have been firmed to be at the event.

Neutrino Tech Systems' US Healthcare Capabilities that could add value for {{{{company_name}}}}:
    • Patient Enrollment/Intake
    • Benefits Investigation (BI)
    • Prior Authorization (PA)
    • Pharmacy Triage and Dispensing
    • Hub Portal/CRM Integration

{cta_2}

Regards,
{{{{signature_block}}}}""",
            "cta": cta_2
        }

        email_3 = {
            "subject": f"Compliance & Data Security — Connect at the Event",
            "body": f"""Hi {{{{first_name}}}},

Just looping back quickly — last chance to catch up at the event.

At Neutrino Tech Systems, we help healthcare teams put AI to work — from improving care and streamlining operations to driving smarter R&D. Our Cloud and Quality Engineering services keep systems secure, audit-ready, and built for healthcare compliance like HIPAA and GDPR.

{cta_3}

Regards,
{{{{signature_block}}}}""",
            "cta": cta_3
        }
    else:
        # Standard cold outreach blueprint fallback
        cta_1 = random.choice(CTA_VARIATIONS['intro']) if cta_enabled else ""
        if cta_enabled:
            cta_1 = f"{cta_1}: {cta_link}"

        email_1 = {
            "subject": f"something for {{{{company_name}}}}",
            "body": f"""Hi {{{{first_name}}}},

{random.choice(openers)}

At {safe_product_ref}, we help {{{{designation}}}} teams {random.choice(value_angles)}.

{cta_1 if cta_enabled else ""}

Regards,
{{{{signature_block}}}}""",
            "cta": cta_1 if cta_enabled else ""
        }

        # Email 2: New angle (not a "follow-up" — adds fresh value)
        proof = random.choice(proof_points) if proof_points else f"Teams like {{{{company_name}}}} see measurable results within the first month."
        cta_2 = random.choice(CTA_VARIATIONS['reminder']) if cta_enabled else ""
        if cta_enabled:
            cta_2 = f"{cta_2}: {cta_link}"

        email_2 = {
            "subject": f"one more angle for {{{{company_name}}}}",
            "body": f"""Hi {{{{first_name}}}},

{proof}

{cta_2 if cta_enabled else ""}

Regards,
{{{{signature_block}}}}""",
            "cta": cta_2 if cta_enabled else ""
        }

        # Email 3: Respectful close (no urgency, clean exit)
        cta_3 = random.choice(CTA_VARIATIONS['last_chance']) if cta_enabled else ""
        if cta_enabled:
            cta_3 = f"{cta_3}: {cta_link}"

        email_3 = {
            "subject": f"not the right time, {{{{first_name}}}}?",
            "body": f"""Hi {{{{first_name}}}},

If this isn't relevant for {{{{company_name}}}} right now, completely understood.

{cta_3 if cta_enabled else "No worries either way — happy to reconnect when the timing is better."}

Regards,
{{{{signature_block}}}}""",
            "cta": cta_3 if cta_enabled else ""
        }
    
    return {
        "email_1": email_1,
        "email_2": email_2,
        "email_3": email_3,
        "generation_method": "BLUEPRINT",
        "persona_type": blueprint.persona_type
    }


class SequenceGeneratorService:
    def __init__(self, db: Session):
        self.db = db
        pass

    def generate_email_sequence(
        self,
        prospect_id: str,
        product_name: str,
        product_description: str = "",
        use_llm: bool = True,
        cta_link: str = "",
        email_context: str = "COLD_OUTREACH",
    ) -> Dict:
        """
        Generate a complete 3-email follow-up sequence for a prospect.
        Raises ValueError if prospect is not found.
        
        Args:
            prospect_id: Prospect ID
            product_name: Product being sold
            product_description: Brief product description
            use_llm: Whether to use LLM (falls back to blueprint if unavailable)
        
        Returns:
            Dict with email_1, email_2, email_3, and metadata
        """
        prospect = self.db.query(Prospect).filter(Prospect.prospect_id == prospect_id).first()
        if not prospect:
            raise ValueError(f"Prospect {prospect_id} not found")

        # Classify prospect
        persona_type, confidence = classify_prospect(
            designation=prospect.designation or "",
            company_name=prospect.company_name
        )
        
        # Get blueprint
        blueprint = self.db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == persona_type,
            PersonaBlueprint.is_active == True
        ).first()
        
        if not blueprint:
            blueprint = self.db.query(PersonaBlueprint).filter(
                PersonaBlueprint.persona_type == "OTHER",
                PersonaBlueprint.is_active == True
            ).first()
        
        if not blueprint:
            return {"error": "No blueprint found"}
        
        # Prepare prospect data
        prospect_data = {
            "first_name": prospect.first_name,
            "designation": prospect.designation,
            "company_name": prospect.company_name,
            "industry": prospect.industry
        }
        
        # Auto-detect email context from product name + description if not explicitly set
        if email_context == "COLD_OUTREACH":
            email_context = detect_email_context(product_name, product_description)

        is_conference = (email_context == EMAIL_CONTEXT_CONFERENCE)

        # Try LLM generation first
        if use_llm and openai_client:
            result = generate_sequence_with_llm(
                prospect_data=prospect_data,
                blueprint=blueprint,
                product_name=product_name,
                product_description=product_description,
                cta_link=cta_link,
                email_context=email_context,
            )

            if result:
                result["schedule"] = {
                    "email_1": {"day": 1, "purpose": "Introduction"},
                    "email_2": {"day": 3, "purpose": "Reminder"},
                    "email_3": {"day": 7, "purpose": "Last Chance"},
                }
                result["email_context"] = email_context
                return result

        # Fallback to blueprint
        result = generate_sequence_from_blueprint(
            prospect_data=prospect_data,
            blueprint=blueprint,
            product_name=product_name,
            cta_link=cta_link,
            is_conference=is_conference,
        )

        result["schedule"] = {
            "email_1": {"day": 1, "purpose": "Introduction"},
            "email_2": {"day": 3, "purpose": "Reminder"},
            "email_3": {"day": 7, "purpose": "Last Chance"},
        }
        result["email_context"] = email_context

        return result


def get_cta_options(purpose: str = "intro") -> List[str]:
    """Get 7 CTA options for a specific email purpose."""
    return CTA_VARIATIONS.get(purpose, CTA_VARIATIONS["intro"])
