# app/services/ai_email_service.py
"""
AI Email Generation Service.
Implements 3-layer hybrid architecture:
  Layer 1: Rule-based persona classification (FREE)
  Layer 2: Persona blueprints for email generation
  Layer 3: LLM-powered micro-variation (OpenAI GPT-4o-mini)
"""

from typing import Optional, Dict, List, Tuple
from sqlalchemy.orm import Session
import random
import json
import logging
import re

from openai import OpenAI

from app.core.config import settings
from app.models import Prospect, ProspectPersona, PersonaBlueprint
from app.prompts import get_system_prompt, get_email_generation_prompt, get_few_shot_examples_block, is_healthcare_pharma, build_persona_intelligence_block, get_capability_pool_block, get_step_tone_block
from app.utils.email_context_detector import detect_email_context, EMAIL_CONTEXT_CONFERENCE
from app.prompts.llm_system_prompts import get_conference_email_system_prompt
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

# Initialize OpenAI client
openai_client = None
if settings.OPENAI_API_KEY:
    openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)


# ============================================================
# LAYER 1: Rule-based Persona Classification (FREE)
# ============================================================

# Weighted keyword mappings for each persona type.
# Format: (keyword, weight)
# Weight scale:
#   10 = unambiguous compound phrase unique to one persona ("market access", "prior authorization")
#    9 = definitive seniority/C-suite title unique to one persona ("chief pharmacy officer")
#    8 = strong function word unique (or near-unique) to one persona ("salesforce", "data science")
#    7 = moderate function word ("intake", "engineering", "innovation")
#    6 = weaker single-word signal ("operations", "digital", "product")
#    5 = term the source spec explicitly cross-lists under multiple personas (e.g. "VP Data & Analytics")
#        — kept at a shared low weight in each bucket it appears under; a stronger unique
#        signal elsewhere will outweigh it
#    4 = last-resort ambiguous single word — only meaningfully fires alongside another match
#
# Generic seniority-only words (Director, VP, Head, Chief, etc. with no domain qualifier) are
# deliberately NOT scored — they're identical across all 5 personas per the source spec, so
# they'd add the same constant to every score and never change the winner. A title with no
# domain-specific signal correctly falls through to the OTHER bucket (best_score == 0).
#
# Short keywords in _WORD_BOUNDARY_KEYWORDS use \b regex to prevent false substring hits
# (e.g., "cto" must not match inside an unrelated word).

PERSONA_KEYWORD_WEIGHTS: Dict[str, List[Tuple[str, int]]] = {
    "PATIENT_SERVICES_HUB": [
        ("patient access", 10), ("patient services", 10), ("hub operations", 10),
        ("access and reimbursement", 10), ("field reimbursement", 10), ("benefits verification", 10),
        ("patient support", 9), ("case management", 9), ("case manager", 8), ("pharma hub", 8),
        ("program manager", 6), ("commercial operations", 5),
        ("data & analytics", 5), ("data and analytics", 5),
        ("patient", 4),
    ],
    "MARKET_ACCESS": [
        ("market access", 10), ("payer strategy", 10), ("value and access", 10), ("value & access", 10),
        ("trade and channel", 10), ("trade & channel", 10), ("chief commercial officer", 10),
        ("chief ai officer", 9), ("reimbursement", 8), ("chief digital", 7),
        ("commercial operations", 6), ("data & analytics", 5), ("data and analytics", 5),
        ("patient services", 5), ("innovation", 4),
    ],
    "OPERATIONS_PHARMACY": [
        ("pharmacy operations", 10), ("specialty pharmacy", 10), ("prior authorization", 10),
        ("revenue cycle", 10), ("chief pharmacy officer", 10), ("director of pharmacy", 10),
        ("intake coordinator", 9), ("clinical operations", 9), ("specialty pharmacist", 9),
        ("pharmacy technician", 9), ("reimbursement specialist", 8), ("intake", 7),
        ("operations", 6), ("salesforce", 6), ("crm", 6),
        ("data & analytics", 5), ("data and analytics", 5), ("patient services", 5),
    ],
    "TECHNOLOGY_DATA_DIGITAL": [
        ("information technology", 9), ("chief digital", 9), ("chief data", 9), ("chief ai", 9),
        ("data science", 9), ("interoperability", 9), ("cio", 9), ("cto", 9),  # word-boundary required
        ("data & analytics", 8), ("data and analytics", 8), ("salesforce", 8),
        ("engineering", 7), ("digital", 6),
    ],
    "INNOVATION_STRATEGY_PRODUCT": [
        ("corporate development", 10), ("emerging technology", 10), ("chief product officer", 10),
        ("chief innovation officer", 10), ("business development", 9), ("solutions architecture", 9),
        ("solutions architect", 9), ("vp product", 9), ("vp engineering", 8), ("cto", 8),  # word-boundary required
        ("innovation", 7), ("product", 6), ("strategy", 6),
    ],
}

# Human-readable labels for UI display — the classifier/blueprint keys above stay
# SCREAMING_SNAKE_CASE for DB/API stability; this is the single place that maps
# them to the friendly names shown in the campaign wizard.
PERSONA_DISPLAY_NAMES: Dict[str, str] = {
    "PATIENT_SERVICES_HUB": "Patient Services / Hub",
    "MARKET_ACCESS": "Market Access",
    "OPERATIONS_PHARMACY": "Operations / Pharmacy",
    "TECHNOLOGY_DATA_DIGITAL": "Technology / Data / Digital",
    "INNOVATION_STRATEGY_PRODUCT": "Innovation / Strategy / Product",
    "OTHER": "Other",
}

# Short keywords that require word-boundary matching to prevent false hits
# e.g. "cio" must not match a substring inside an unrelated word
_WORD_BOUNDARY_KEYWORDS = frozenset({
    "cio", "cto",
})


def classify_prospect(designation: str, company_name: str = None) -> Tuple[str, float]:
    """
    Classify a prospect into a persona type using a weighted scoring system.

    Scores ALL persona types simultaneously and returns the highest scorer,
    eliminating the first-match-wins ordering bug of the previous approach.

    Returns:
        Tuple of (persona_type, confidence_score 0.0–0.95)
    """
    if not designation:
        return ("OTHER", 0.5)

    designation_lower = designation.lower()
    scores: Dict[str, float] = {p: 0.0 for p in PERSONA_KEYWORD_WEIGHTS}

    for persona_type, keywords in PERSONA_KEYWORD_WEIGHTS.items():
        for keyword, weight in keywords:
            if keyword in _WORD_BOUNDARY_KEYWORDS:
                if re.search(r'\b' + re.escape(keyword) + r'\b', designation_lower):
                    scores[persona_type] += weight
            else:
                if keyword in designation_lower:
                    scores[persona_type] += weight

    best_score = max(scores.values())
    if best_score == 0:
        return ("OTHER", 0.5)

    # All personas that tied for the top score — ties resolve to whichever persona
    # is listed first in PERSONA_KEYWORD_WEIGHTS (dict insertion order).
    top_personas = [p for p, s in scores.items() if s == best_score]
    best_persona = top_personas[0]
    # Confidence: score of 10 → ~0.90, score of 7 → ~0.78, score of 3 → ~0.62
    confidence = min(0.95, 0.5 + (best_score / 25))
    return (best_persona, confidence)


def classify_prospects_batch(prospects: List[Prospect]) -> List[Dict]:
    """
    Classify a batch of prospects.
    
    Returns:
        List of dicts with prospect_id, persona_type, confidence_score
    """
    results = []
    for prospect in prospects:
        persona_type, confidence = classify_prospect(
            designation=prospect.designation or "",
            company_name=prospect.company_name
        )
        results.append({
            "prospect_id": prospect.prospect_id,
            "persona_type": persona_type,
            "confidence_score": confidence,
            "classification_method": "RULE_BASED"
        })
    return results


# ============================================================
# LAYER 2: Email Generation from Blueprints
# ============================================================

def generate_email_from_blueprint(
    blueprint: PersonaBlueprint,
    prospect: Prospect,
    product_name: str = "Our Product",
    cta_link: str = ""
) -> Dict[str, str]:
    """
    Generate a Neutrino-style structured email using persona blueprint data.
    Used as fallback when LLM is unavailable.

    Structure:
        Hi {{first_name}},
        [Pain point hook from openers]
        Neutrino intro sentence
        Capability bullets from value_angles
        CTA from ctas
    """
    cta_link = normalize_cta_link(cta_link)
    cta_enabled = has_effective_cta_link(cta_link)

    first_name = prospect.first_name or "there"
    company = prospect.company_name or "your company"
    industry = prospect.industry or "your industry"
    designation = prospect.designation or "your role"

    # Pick a pain point hook (opener = role frustration description)
    pain_point = random.choice(blueprint.openers) if blueprint.openers else ""

    # Pick capability bullets (up to 4 value angles)
    capabilities = blueprint.value_angles[:4] if blueprint.value_angles else []

    # Pick CTA
    cta_text = random.choice(blueprint.ctas) if blueprint.ctas else "Up for a quick chat next week?"
    if cta_enabled and cta_link:
        cta_text = f"{cta_text}\n{cta_link}"

    tone = blueprint.tone_rules.get("style", "professional") if blueprint.tone_rules else "professional"

    # Build Neutrino-style email body
    body_parts = [f"Hi {first_name},", ""]

    # Pain point hook
    if pain_point:
        body_parts.append(f"Teams in the {industry} space often deal with challenges like: {pain_point}.")
        body_parts.append("")

    # Neutrino intro
    body_parts.append(
        f"We at Neutrino Tech Systems have been helping companies like {company} accelerate "
        f"performance and reduce operational overhead with Automation, AI, Data Engineering, "
        f"and Custom Application Development."
    )
    body_parts.append("")

    # Capability bullets
    if capabilities:
        body_parts.append(f"What we help {designation} teams with:")
        for cap in capabilities:
            body_parts.append(f"    • {cap.capitalize()}")
        body_parts.append("")

    # CTA
    body_parts.append(cta_text)

    body = "\n".join(body_parts)

    subject = f"{company}'s operations" if company != "your company" else "improving your team's workflow"

    return {
        "subject": subject,
        "body": body,
        "opener_used": pain_point,
        "cta_used": cta_text,
        "tone": tone,
        "generation_method": "BLUEPRINT_FALLBACK",
    }


# ============================================================
# Database Operations
# ============================================================

def get_or_create_prospect_persona(
    db: Session,
    prospect: Prospect
) -> ProspectPersona:
    """
    Get existing persona or create new one for a prospect.
    """
    # Check if persona already exists
    existing = db.query(ProspectPersona).filter(
        ProspectPersona.prospect_id == prospect.prospect_id
    ).first()
    
    if existing:
        return existing
    
    # Classify and create new persona
    persona_type, confidence = classify_prospect(
        designation=prospect.designation or "",
        company_name=prospect.company_name
    )
    
    # Find matching blueprint
    blueprint = db.query(PersonaBlueprint).filter(
        PersonaBlueprint.persona_type == persona_type,
        PersonaBlueprint.is_active == True
    ).first()
    
    # Create persona
    persona = ProspectPersona(
        prospect_id=prospect.prospect_id,
        blueprint_id=blueprint.blueprint_id if blueprint else None,
        persona_type=persona_type,
        confidence_score=confidence,
        classification_method="RULE_BASED"
    )
    
    db.add(persona)
    db.commit()
    db.refresh(persona)
    
    return persona


def generate_email_for_prospect(
    db: Session,
    prospect: Prospect,
    product_name: str = "Our Product",
    cta_link: str = ""
) -> Optional[Dict[str, str]]:
    """
    Full pipeline: classify prospect and generate email.
    """
    # Get or create persona
    persona = get_or_create_prospect_persona(db, prospect)
    
    # Get blueprint
    if persona.blueprint_id:
        blueprint = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.blueprint_id == persona.blueprint_id
        ).first()
    else:
        # Fallback to OTHER blueprint
        blueprint = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == "OTHER",
            PersonaBlueprint.is_active == True
        ).first()
    
    if not blueprint:
        return None
    
    # Generate email
    return generate_email_from_blueprint(blueprint, prospect, product_name, cta_link)


# ============================================================
# LAYER 3: LLM-Powered Email Generation (OpenAI GPT-4o-mini)
# ============================================================

def generate_email_with_llm(
    prospect_data: Dict,
    blueprint: PersonaBlueprint,
    product_name: str = "Our Product",
    product_description: str = "",
    cta_link: str = "",
    creative_email: bool = False,
    custom_instruction: str = "",
    email_context: str = "COLD_OUTREACH",
) -> Optional[Dict[str, str]]:
    """
    Generate a personalized email using OpenAI GPT-4o-mini.
    Uses blueprint as guidance but creates unique content.
    
    Args:
        prospect_data: Dict with first_name, designation, company_name, industry
        blueprint: PersonaBlueprint with tone_rules and example content
        product_name: Name of the product being sold
        product_description: Brief description of the product
    
    Returns:
        Dict with subject, body, etc. or None if LLM unavailable
    """
    if not openai_client:
        return None

    cta_link = normalize_cta_link(cta_link)
    cta_enabled = has_effective_cta_link(cta_link)

    # Auto-detect email context from product name + description if not explicitly set
    if email_context == "COLD_OUTREACH":
        email_context = detect_email_context(product_name, product_description)

    # Route conference/in-person emails to dedicated prompt + generation path
    if email_context == EMAIL_CONTEXT_CONFERENCE:
        tone_rules = blueprint.tone_rules or {}
        tone = tone_rules.get("style", "professional")
        avoid_list = tone_rules.get("avoid", [])
        conf_system_prompt = get_conference_email_system_prompt(tone=tone, avoid_list=avoid_list)
        from app.prompts.email_examples import get_conference_few_shot_examples_block
        examples_block = get_conference_few_shot_examples_block(
            industry=prospect_data.get("industry", ""),
            email_type="conference_intro",
            limit=1,
        )
        conf_user_prompt = f"""Generate 1 conference/in-person outreach email for Neutrino Tech Systems.

=== CAMPAIGN CONTEXT ===
{product_description or "Conference/event outreach — generate an appropriate event hook."}

=== PROSPECT ===
Role: {prospect_data.get('designation', 'professional')}
Industry: {prospect_data.get('industry', 'healthcare')}
Company: {prospect_data.get('company_name', 'their company')}

{examples_block}

Rules:
- Subject: {{{{first_name}}}}, [event name] [verb phrase] — Title Case
- First line: "Hi {{{{first_name}}}},"
- Dual CTA: in-person first, virtual fallback
- Plain text only. No unsubscribe footer.

Return JSON: {{"subject": "...", "body": "..."}}"""

        def _call_openai_conf():
            return openai_client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": conf_system_prompt},
                    {"role": "user", "content": conf_user_prompt},
                ],
                temperature=0.75,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )

        try:
            resp = retry_with_backoff(_call_openai_conf, max_attempts=3, base_delay=1.0, max_delay=30.0)
            if resp is None:
                return None
            result = json.loads(resp.choices[0].message.content)
            result["body"] = finalize_email_body(result.get("body", ""))
            result["generation_method"] = "LLM_CONFERENCE"
            result["email_context"] = email_context
            return result
        except Exception as e:
            logger.error("[generate_email_with_llm] Conference path error: %s", e)
            return None

    # Build the prompt
    tone_rules = blueprint.tone_rules or {}
    tone = tone_rules.get("style", "professional")
    avoid_list = tone_rules.get("avoid", [])

    # Persona intelligence extracted from blueprint (shared builder — see app/prompts/email_examples.py)
    persona_intelligence_block = build_persona_intelligence_block(blueprint)

    industry_lower = str(prospect_data.get('industry', '')).lower()
    if "pharma" in industry_lower or "health" in industry_lower or "clinical" in industry_lower:
        in_group_pack = "charting load, SOAP notes, EHR sync, audit trail, care-team handoff, clinician minutes"
    elif "saas" in industry_lower or "software" in industry_lower or "tech" in industry_lower:
        in_group_pack = "pipeline velocity, demo-to-close, CRM hygiene, onboarding friction, expansion revenue, churn risk"
    elif "manufactur" in industry_lower or "supply" in industry_lower:
        in_group_pack = "cycle time, throughput, line downtime, QA deviation, compliance checks, handoff latency"
    elif "finance" in industry_lower or "bank" in industry_lower:
        in_group_pack = "manual reconciliation, exception queue, risk controls, approval latency, audit readiness, close cycle"
    else:
        in_group_pack = "workflow bottleneck, handoff delay, error rate, compliance risk, cycle-time drag, margin pressure"

    healthcare_mode = True  # Apply Neutrino outreach style to all industries

    if healthcare_mode:
        svc_word_count_rule = "150–250 words."
        svc_bullet_rule = "Bullet lists are allowed and encouraged to highlight capabilities."
        svc_fup_ban = ""   # "just following up" etc. allowed in healthcare follow-ups
    else:
        svc_word_count_rule = "50–80 words maximum."
        svc_bullet_rule = "No bullet point lists — integrate value into narrative."
        svc_fup_ban = '"just following up", "checking in", "circling back",'

    if healthcare_mode:
        cta_rule = (
            "End with a direct meeting or call ask. "
            "GOOD: 'Up for a quick 15–20 min virtual chat next week?' / "
            "'Let's connect for a quick, no-pressure call.' / "
            "'Happy to jump on a quick call or send a short overview — whichever's easier.'"
        ) if cta_enabled else "Close naturally. No meeting ask, no URL."

        system_prompt = f"""You are a senior B2B outbound email writer at Neutrino Tech Systems.
Write a cold outreach email in Neutrino Tech Systems' established brand voice.

=== NEUTRINO EMAIL STRUCTURE (follow this exactly) ===

1. PAIN POINT HOOK — Open with a direct, UNIQUE question about the prospect's operational challenge.
   Write a question specific to THIS prospect's role and industry. Here are diverse patterns (do NOT copy — write your own):
   - "Is [company]'s [specific process] still running on [manual method]?"
   - "How much time is [company]'s [role] team losing to [specific bottleneck]?"
   - "Are [specific system] gaps across your [industry] operations creating [specific downstream problem]?"
   BANNED OPENER: "Are manual processes, data silos, and compliance hurdles slowing your growth?" — NEVER use this.
   BAD: "Many teams face challenges with efficiency."
   BAD: "Many {{{{industry}}}} teams struggle with..."

2. COMPANY INTRO — Introduce Neutrino Tech Systems with credibility markers.
   REQUIRED: Always write "Neutrino Tech Systems" — NEVER use the {{{{our_company}}}} token.
   Include what Neutrino Tech Systems does (US-based, AI First, relevant solution area for the prospect's industry).

3. CAPABILITY BULLETS (4–8 bullets) — Show what Neutrino Tech Systems solves for the prospect.
   Use the SPECIFIC services from the NEUTRINO SERVICE REFERENCE examples in the user prompt.
   DO NOT invent generic bullets like "optimize workflows" or "ensure data integrity".
   Use "    •" bullet format (4 spaces + bullet).

4. DIRECT CTA — {cta_rule}

=== FORMAT RULES ===
- 150–250 words total body
- Bullet lists required for capabilities section
- First line must be exactly: "Hi {{{{first_name}}}},"
- Write "Neutrino Tech Systems" — NEVER use {{{{our_company}}}} token
- No signature (system appends automatically)
- Plain text only (no HTML)
- Tone: {tone}

=== HARD BANS ===
Never use: "many teams face", "many {{{{industry}}}} teams", "in today's landscape",
"leverage", "synergy", "digital transformation", "AI-powered", "cutting-edge",
"game-changer", "streamline operations", "enhance efficiency",
"I'd love to", "excited to share", "we can help you achieve"
Never use {{{{our_company}}}} — write "Neutrino Tech Systems" instead.
SUBJECT: Never use "Transform", "AI", "Improving outcomes", "idea for [name]".

=== PERSONALIZATION ===
- Pain point question must be specific to the prospect's industry and role
- Capability bullets must match their actual industry (not generic)
- Reference {{{{company_name}}}} in hook or CTA
{persona_intelligence_block}
Return JSON: {{"subject": "...", "body": "..."}}"""

    elif creative_email:
        if cta_enabled:
            sentence_4_block = """Sentence 4 - CTA:
One specific, low-friction ask referencing their company or role.
  GOOD: "Open to a 10-minute walkthrough scoped to {{{{company_name}}}}'s current process?"
  BAD: "Would you like to learn more?" """
            cta_constraint = "- Exactly one CTA"
            url_constraint = "- Do not output a standalone raw URL line; include one CTA sentence only."
            self_check_line = " SINGLE-ASK: Exactly one CTA that references their company or role?"
        else:
            sentence_4_block = """Sentence 4 - CLOSE:
Close naturally with no meeting ask and no URL.
  GOOD: "If this is relevant later, I can share a one-pager."
  BAD: "Open to a quick call?"
  BAD: "Would you like to learn more?" """
            cta_constraint = "- Do NOT include any CTA sentence, meeting ask, booking link, or URL."
            url_constraint = "- Do not output standalone raw URL lines."
            self_check_line = " NO-CTA: Confirm there is no CTA sentence, no meeting ask, and no URL."

        system_prompt = f"""You are a senior outbound strategist writing a single high-response cold email.
You think like someone inside the prospect's company, not a seller outside it.

=== HOW TO WRITE THIS EMAIL ===

Sentence 1 - ATOMIC UNIT:
State something plainly true about this role's daily work. An operational fact, not a compliment or trend.
  GOOD (healthcare): "Most documentation time isn't spent in the consult - it's spent rewriting conversations into notes."
  GOOD (SaaS): "Pipeline reviews catch stale deals, but rarely surface why they stalled."
  GOOD (manufacturing): "Line downtime gets tracked. The 20 minutes of shift-change handoff confusion usually doesn't."
  BAD: "In today's fast-paced environment, efficiency matters more than ever."
  TEST: Could this sentence appear in any email to any company? If yes, rewrite.

Sentence 2 - FRAME SHIFT:
Reframe where the real cost lives. Name a specific downstream consequence.
  GOOD: "That handoff tax shows up as delayed follow-ups and overtime."
  BAD: "This leads to inefficiencies across your organization."

Sentence 3 - OFFER BRIDGE:
One concrete mechanism + one metric. How it works, not what it is.
  GOOD: "We convert visit conversations into structured notes inside existing workflows - cutting documentation time by 60%."
  BAD: "Our platform streamlines operations and boosts efficiency."

{sentence_4_block}

=== HARD BANS (instant spam trigger — NEVER use any of these) ===
Never use: "As a key decision-maker", "As a leader", "you're likely aware", "I noticed", "I came across",
"I've been following", "I've been impressed", "I was researching", "I saw that",
"hope you're well", "hope this finds you", "quick question",
"digital transformation", "intelligent automation", "many teams struggle",
"enhance operational efficiency", "streamline operations", "streamline your",
"ai-powered", "AI-powered", "in today's [adjective] landscape", "stay ahead of",
"leverage", "synergy", "cutting-edge", "game-changer", "revolutionize",
"imagine the impact", "imagine what", "imagine how",
"I'd love to", "I'd be happy to", "excited to share",
"we can help you", "we help companies like",
bracket CTAs like "[Book a demo right here: ...]",
{svc_fup_ban}

SUBJECT HARD BANS: Never put "AI" in the subject line (instant spam trigger). Never use:
- "AI" or "Artificial Intelligence" anywhere in the subject
- "Improving outcomes" / "Transform" / "Revolutionize" (marketing language)
- "idea for [name]" or "idea for [company]" (generic, overused)
- "worth a look" or "worth a look, [name]?" (overused — BANNED)

SUBJECT HARD BANS: Never use these patterns in the subject line:
- "Why does [company]..." (accusatory question)
- "The hidden cost of..." (fear/manipulation)
- "Where [company] loses..." (negative framing)
- "Quick thought for [Name]" / "Idea for [Name]" (template spam pattern)
- "Hi [Name], ... thought on [Topic]" (template spam pattern)
- "Don't miss..." / "Act now..." / "Limited time..." (urgency spam)
- "You won't believe..." / "Secret to..." (clickbait)
- Forbidden exact phrases in subject AND body: "quick thought", "quick question", "unlock growth", "boost efficiency", "trends", "ideas for"

CATEGORY BAN: Any sentence that works unchanged for a different company in a different industry is too generic. Rewrite it. Specifically, DO NOT use geographic tokens just by saying "especially in {{{{city}}}}". If you use {{{{city}}}}, it must tie to a specific local regulation, event, or known hub attribute.

=== IN-GROUP LANGUAGE ===
Use where natural: {in_group_pack}
{persona_intelligence_block}
=== CONSTRAINTS ===
- Plain text only (no HTML)
- {svc_word_count_rule}
{cta_constraint}
- One concrete metric
- No signature in body
- Do not include unsubscribe/footer text; system appends one standard line.
- Don't start with "I"
- First line must be exactly: "Hi {{{{first_name}}}},"
{url_constraint}
- Use correct possessive token grammar: "{{{{company_name}}}}'s" (with apostrophe)
- Tokens: {{{{first_name}}}}, {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}, {{{{linkedin_url}}}}, {{{{our_company}}}}, {{{{your_name}}}}
- Include at least THREE prospect context tokens naturally in body: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}.
- Never write metadata-like lines such as "Prospect context used:".
- Subject must be varied and non-template. Avoid defaulting to "Where {{{{company_name}}}} loses ...".
- SUBJECT SPAM AVOIDANCE: Do NOT use provocative, negative, or accusatory phrasing in the subject. Also avoid common cold-email templates like "Quick thought for [Name] at [Company]".
  BAD: "Why does {{{{company_name}}}}'s documentation take so long?"
  BAD: "The hidden cost at {{{{company_name}}}}"
  BAD: "Where {{{{company_name}}}} loses time"
  GOOD: "Quick thought on documentation at {{{{company_name}}}}"
  GOOD: "60% less charting time for {{{{industry}}}} teams"
  GOOD: "{{{{company_name}}}}'s clinical workflow"
  BANNED: "idea for {{{{company_name}}}}", "idea for {{{{first_name}}}}", "idea for X" — too generic, overused
- Prefer one of these subject families:
  1) Curiosity-driven observation (neutral tone, not negative): "{{{{company_name}}}}'s [specific process]"
  2) Concrete metric or result: "60% less [process] time"
  3) Process-specific hook: "{{{{company_name}}}}'s [workflow]", "[role] + [specific outcome]"
  4) Mechanism-led result: "how teams cut [metric] without changing [system]"
- NEVER frame the subject as a complaint, accusation, or negative question about the recipient's company.

=== SELF-CHECK BEFORE OUTPUT ===
 SWAP TEST: Would this email work for any company? If yes, rewrite.
 SPECIFICITY: Does it name a concrete process or metric? "Efficiency" fails. "Documentation turnaround" passes.
 ATOMIC: Does sentence 1 state an insider operational fact?
{self_check_line}

=== FORMAT STRUCTURE ===
Output plain text. {svc_bullet_rule}

Hi {{{{first_name}}}},

[Sentence 1 — atomic operational fact about their role]

[Sentence 2 — frame shift: where the real cost shows up]

[Sentence 3 — your mechanism + one metric, naturally woven in]

[Sentence 4 — CTA or soft close, one sentence only]

Rules:
- Exactly 4 sentences after the greeting. Not 3. Not 5. Four.
- Separate paragraphs with ONE blank line.
- No signature in body. System appends it automatically.
- Total body: {svc_word_count_rule}

Return JSON: {{"subject": "...", "body": "..."}}
"""
    else:
        system_prompt = f"""You are a senior B2B outbound strategist writing a cold email that earns a reply.
You think like an operator inside the prospect's company — not a vendor pitching from outside.

TONE: {tone}, slightly casual
AVOID: {', '.join(avoid_list) if avoid_list else 'generic language, spam phrases'}

=== 4-SENTENCE EMAIL STRUCTURE ===
1. ATOMIC UNIT: One operational fact about this role's daily work (insider reality, not a trend).
2. FRAME SHIFT: Name the specific downstream consequence — where the cost actually shows up.
3. OFFER BRIDGE: One concrete mechanism + one specific metric/number.
4. CTA: {"Direct meeting or call ask is appropriate for healthcare outreach. GOOD: 'Up for a quick 15-20 min chat next week?'" if healthcare_mode else ("One low-friction interest question only — no booking asks. GOOD: 'Worth exploring?' / 'Is this on your radar?'" if cta_enabled else "Neutral close only. No CTA, no booking ask, no URL.")}

=== HARD BANS (instant spam trigger — NEVER use) ===
Never use: "As a key decision-maker", "As a leader", "I noticed", "I came across",
"I've been following", "I've been impressed", "I was researching", "I saw that",
"hope you're well", "hope this finds you", "quick question",
"digital transformation", "leverage", "streamline operations", "streamline your",
"enhance efficiency", "enhance operational efficiency",
"cutting-edge", "AI-powered", "ai-powered", "in today's landscape", "stay ahead",
"imagine the impact", "imagine what", "imagine how",
"I'd love to", "excited to share", "we can help you", "we help companies like",
"act now", "limited time", "guaranteed", {svc_fup_ban}
bracket CTAs like "[Book a demo right here: ...]"

SUBJECT BANS: Never put "AI" in subject. Never use "Transform", "Improving outcomes",
"idea for [name]", "idea for [company]", "worth a look" in subject.

=== WRITING RULES ===
- Plain text ONLY. No HTML tags.
- {svc_word_count_rule}
- Reading level: 5th grade. Short sentences. No jargon.
- 3:1 you/I ratio minimum.
- {svc_bullet_rule}
- No signature in body (system appends automatically).
- No unsubscribe footer (system appends automatically).
- First line must be exactly: "Hi {{{{first_name}}}},"
- Do NOT start with "I".
- Tokens: {{{{first_name}}}}, {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}
{persona_intelligence_block}
Return JSON: {{"subject": "...", "body": "..."}}"""

    # Build product context — only include description if meaningful, never expose raw campaign/product name
    product_context = ""
    if product_description and product_description.strip():
        product_context = f"""What we do (INTERNAL CONTEXT ONLY — do NOT name the product or brand in the email):
{product_description}
RULE: Describe the MECHANISM and RESULT in your own words. Never mention the product name, brand name, or "AI" in the subject line."""
    elif product_name and product_name.lower() not in ("our product", "test", "test 1", "test 2", "test 3", "campaign"):
        product_context = f"""What we do (INTERNAL CONTEXT ONLY — do NOT name the product in the email):
{product_name}
RULE: Describe the MECHANISM and RESULT in your own words. Never mention the product name."""

    capability_pool_block = get_capability_pool_block()
    intro_tone_block = get_step_tone_block(1)  # Single email always uses intro tone

    if healthcare_mode:
        user_prompt = f"""Write a Neutrino Tech Systems cold outreach email for:
- Name: {prospect_data.get('first_name', 'there')}
- Role: {prospect_data.get('designation', 'professional')}
- Company: {prospect_data.get('company_name', 'their company')}
- Industry: {prospect_data.get('industry', 'technology')}

{product_context}

{capability_pool_block}

{intro_tone_block}

{"CTA Link: " + cta_link if cta_enabled else "No CTA link. Do NOT include a meeting ask, booking link, or URL."}

Follow the NEUTRINO EMAIL STRUCTURE from the system prompt exactly.

Return JSON: {{"subject": "...", "body": "..."}}"""

    elif creative_email:
        user_prompt = f"""Write a cold email for:
- Name: {prospect_data.get('first_name', 'there')}
- Role: {prospect_data.get('designation', 'professional')}
- Company: {prospect_data.get('company_name', 'their company')}
- Industry: {prospect_data.get('industry', 'technology')}

{product_context}

{few_shot_block}

{"CTA Link: " + cta_link if cta_enabled else "No CTA link provided. Do NOT include any CTA sentence, meeting ask, booking link, or URL."}
- Available personalization tokens: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}, {{{{linkedin_url}}}}
- Include at least THREE core tokens naturally in the body: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}.
- Never write metadata-like lines such as "Prospect context used:".
- NEVER use the internal campaign/product name in the subject line or email body. Write as if the recipient has no idea what your product is called.

Return a JSON object with:
{{"subject": "...", "body": "plain text body..."}}"""
    else:
        user_prompt = f"""Write a cold email for:
- Name: {prospect_data.get('first_name', 'there')}
- Role: {prospect_data.get('designation', 'professional')}
- Company: {prospect_data.get('company_name', 'their company')}
- Industry: {prospect_data.get('industry', 'technology')}

{product_context}

{few_shot_block}

{"CTA Link: " + cta_link if cta_enabled else "No CTA link provided. Do NOT include any CTA sentence, meeting ask, booking link, or URL."}
- Available personalization tokens: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}, {{{{linkedin_url}}}}
- Include at least THREE core tokens naturally in the body: {{{{company_name}}}}, {{{{designation}}}}, {{{{industry}}}}, {{{{city}}}}, {{{{state}}}}.
- Never write metadata-like lines such as "Prospect context used:".
- NEVER use the internal campaign/product name in the subject line or email body. Write as if the recipient has no idea what your product is called.
- Generate an atomic opener grounded in what you know about how {prospect_data.get('designation', 'this role')} roles actually work — NOT based on templates. The opener must pass the SWAP TEST: too specific to work for any other role or company.

Return a JSON object with:
{{"subject": "...", "body": "plain text body..."}}"""

    if custom_instruction:
        user_prompt += f"\n\nUSER_PREFERENCE: {custom_instruction}"
    temperature = 0.85 if creative_email else 0.7

    def _call_openai():
        """Single OpenAI call — wrapped so retry_with_backoff can retry it."""
        return openai_client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=800 if healthcare_mode else 500,
            response_format={"type": "json_object"},
        )

    try:
        # Up to 3 attempts with exponential backoff (1 s → 2 s → 4 s)
        response = retry_with_backoff(
            _call_openai,
            max_attempts=3,
            base_delay=1.0,
            max_delay=30.0,
        )

        if response is None:
            # All retries exhausted — surface as None so caller falls back
            logger.error("[generate_email_with_llm] All retry attempts exhausted. Returning None.")
            return None

        result = json.loads(response.choices[0].message.content)
        
        body = result.get("body", "")
        closing = result.get("closing_style", "Best")
        
        # Clean up any LLM-hallucinated signatures before appending our official one
        # Strip common sign-offs

        # Remove any signature blocks generated by LLM
        import re
        
        body = re.sub(
            # r"\n\s*(Best|Regards|Best regards|Thanks|Cheers|Sincerely)[,]?\s*\n+[^\n]+$",
            r"\n+(Best|Regards|Best regards|Thanks|Cheers|Sincerely)[,]?\s*\n+[^\n]+$",
            "",
            body,
            flags=re.IGNORECASE
        ).strip()
        
        # CTA Stitcher: Ensure the CTA link URL is present only when configured.
        # The LLM already writes a CTA sentence in the body — only append the link, never repeat the CTA text.
        link_to_check = cta_link if cta_enabled else ""
        if cta_enabled and link_to_check not in body and "{{calendar_link}}" not in body and "{calendar_link}" not in body:
            body += f"\n\nLink to find time: {link_to_check}"
        elif not cta_enabled:
            body = strip_cta_content_no_link(body)

        # Enforce minimum personalization context if model output is too generic.
        # body = ensure_core_personalization_tokens(body)

        # Enforce paragraph spacing: blank lines between paragraphs,
        # no blank lines between bullet points.
        # body = normalize_unsubscribe_footer(body)
        # body = normalize_paragraph_spacing(body)
        body = finalize_email_body(body)

        # Append sign-off (mandatory footer handled by sender service now)
        # if "{{your_name}}" not in body and "{your_name}" not in body:
        #      body += f"\n\nRegards,\n{{{{your_name}}}}"

        # Normalize unsubscribe/footer text so creative and generic previews match.
        # body = normalize_unsubscribe_footer(body)
        
        return {
            "subject": result.get("subject", f"{prospect_data.get('company_name', 'your team')}'s workflow"),
            "body": body,
            "model_used": settings.OPENAI_MODEL,
            "tokens_used": response.usage.total_tokens if response.usage else 0,
            "generation_method": "LLM"
        }
        
    except Exception as e:
        logger.error("[generate_email_with_llm] Unexpected error: %s", e)
        return None


def generate_email_hybrid(
    db: Session,
    prospect: Prospect,
    product_name: str = "Our Product",
    product_description: str = "",
    use_llm: bool = True,
    cta_link: str = ""
) -> Dict[str, str]:
    """
    Hybrid email generation: Try LLM first, fallback to blueprint.
    
    Args:
        db: Database session
        prospect: Prospect object
        product_name: Name of the product
        product_description: Brief product description
        use_llm: Whether to try LLM generation first
    
    Returns:
        Dict with subject, body, and generation metadata
    """
    # Get or create persona
    persona = get_or_create_prospect_persona(db, prospect)
    
    # Get blueprint
    blueprint = db.query(PersonaBlueprint).filter(
        PersonaBlueprint.persona_type == persona.persona_type,
        PersonaBlueprint.is_active == True
    ).first()
    
    if not blueprint:
        blueprint = db.query(PersonaBlueprint).filter(
            PersonaBlueprint.persona_type == "OTHER",
            PersonaBlueprint.is_active == True
        ).first()
    
    if not blueprint:
        return {"error": "No blueprint found"}
    
    # Try LLM generation first
    if use_llm and openai_client:
        prospect_data = {
            "first_name": prospect.first_name,
            "designation": prospect.designation,
            "company_name": prospect.company_name,
            "industry": prospect.industry
        }
        
        llm_result = generate_email_with_llm(
            prospect_data=prospect_data,
            blueprint=blueprint,
            product_name=product_name,
            product_description=product_description,
            cta_link=cta_link
        )
        
        if llm_result:
            llm_result["persona_type"] = persona.persona_type
            return llm_result
    
    # Fallback to blueprint-based generation
    result = generate_email_from_blueprint(blueprint, prospect, product_name, cta_link)
    result["generation_method"] = "BLUEPRINT"
    result["persona_type"] = persona.persona_type
    return result

