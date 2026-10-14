# app/prompts/regeneration_prompts.py
"""
LLM prompts for regenerating one field (subject or body) of a wizard email template.

Two editions: cold outreach, and conference / in-person outreach (picked from the campaign
description by detect_email_context). Pure string building, no I/O.
"""
from typing import Optional

from app.prompts.email_examples import (
    get_capability_pool_block,
    get_conference_step_tone_block,
    get_step_tone_block,
)
from app.utils.email_context_detector import EMAIL_CONTEXT_CONFERENCE, detect_email_context
from app.utils.email_utils import has_effective_cta_link, normalize_cta_link

REGENERATABLE_FIELDS = ("subject", "body")
SYSTEM_PROMPT = "You are a B2B email copywriter. Generate only what is asked, no explanations."

COLD_EMAIL_TYPE_BY_STEP = {1: "intro", 2: "followup_1", 3: "followup_2", 4: "followup_3", 5: "followup_4", 6: "followup_5"}
CONFERENCE_EMAIL_TYPE_BY_STEP = {
    1: "conference_intro", 2: "conference_fup1", 3: "conference_fup2",
    4: "conference_fup3", 5: "conference_fup4", 6: "conference_fup5", 7: "conference_fup6",
}


def build_regeneration_prompt(
    *,
    field: str,
    current_subject: Optional[str],
    campaign_description: str,
    persona_type: str,
    product_name: str,
    cta_link: Optional[str],
    custom_instruction: Optional[str],
    creative_email: bool,
    step_number: int,
) -> str:
    """The user prompt asking the LLM for a fresh subject line or body for this step."""
    email_context = detect_email_context(product_name, campaign_description)
    is_conference = (email_context == EMAIL_CONTEXT_CONFERENCE)

    if field == 'subject':
        if is_conference:
            prompt = f"""Generate a new subject line for a <<SENDER_COMPANY>> conference/in-person outreach email.
Context: {campaign_description or 'Conference or in-person outreach at <<SENDER_COMPANY>>'}
Target persona: {persona_type or 'professional'}
Email step: {step_number}

Current subject: {current_subject}

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
Context: {campaign_description or 'B2B sales outreach at <<SENDER_COMPANY>>'}
Target persona: {persona_type or 'professional'}
Product: {product_name}

Current subject: {current_subject}

Rules:
- 3–7 words, descriptive, not clickbait
- Reference the prospect's industry, role, or a specific challenge
- Use tokens: {{{{company_name}}}}, {{{{first_name}}}}, {{{{industry}}}}
- BANNED: "Transform", "AI", "Improving outcomes", "idea for [name]", "quick question", "quick thought"
- BANNED: Urgency language ("Act now", "Last chance"), negative framing ("Why does {{{{company_name}}}} fail")
- Do NOT use actual names, use {{{{tokens}}}} only

Return ONLY the new subject line, nothing else."""
    else:
        normalized_cta_link = normalize_cta_link(cta_link)
        cta_enabled = has_effective_cta_link(normalized_cta_link)

        capability_pool_block = get_capability_pool_block()

        if is_conference:
            email_type_for_step = CONFERENCE_EMAIL_TYPE_BY_STEP.get(step_number, "conference_intro")
            step_tone_block = get_conference_step_tone_block(step_number)

            cta_rule = (
                "DUAL CTA REQUIRED — in-person meeting ask first, virtual fallback second. "
                "GOOD: 'Please let me know your availability for an In-Person meeting at the event. "
                "If not at the event, we could also connect virtually.' "
                "BAD: 'Up for a quick Zoom next week?' (virtual only — banned)"
            ) if cta_enabled else "Close naturally. No meeting ask, no URL."

            prompt = f"""Write a <<SENDER_COMPANY>> conference/in-person outreach email body.

Context: {campaign_description or 'In-person outreach at <<SENDER_COMPANY>>'}
Target persona: {persona_type or 'professional'}
Email step: {step_number} ({email_type_for_step})

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
            email_type_for_step = COLD_EMAIL_TYPE_BY_STEP.get(step_number, "intro")
            step_tone_block = get_step_tone_block(step_number)

            cta_rule = (
                "End with a direct meeting or call ask. "
                "GOOD: 'Up for a quick 15–20 min virtual chat next week?' / "
                "'Let's connect for a quick, no-pressure call.' / "
                "'Happy to jump on a quick call or send a short overview — whichever\\'s easier.'"
            ) if cta_enabled else "Close naturally. No meeting ask, no URL."

            prompt = f"""Write a <<SENDER_COMPANY>> cold outreach email body.

Context: {campaign_description or 'B2B sales outreach at <<SENDER_COMPANY>>'}
Target persona: {persona_type or 'professional'}
Product: {product_name}
Email step: {step_number} ({email_type_for_step})

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
    if custom_instruction:
        prompt += f"\n\nUSER_FEEDBACK: The user has requested: '{custom_instruction}'. Apply this preference to this specific rewrite if reasonable."

    if creative_email and field == "subject":
        prompt += """

CREATIVE_EMAIL_MODE:
- Avoid generic subject patterns ("Quick question for you", "Idea for", "Quick thought for")
- NEVER use these exact phrases: "quick thought", "quick question", "unlock growth", "boost efficiency", "trends", "ideas for"
- Keep it specific, concrete, operational, and role-relevant (e.g. "Clinical workflow notes" or "Data entry turnaround")
"""

    return prompt
