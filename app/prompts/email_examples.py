# app/prompts/email_examples.py
"""
Few-shot email examples for LLM-guided generation.

Examples are stored per persona_type and industry_tag.
The get_few_shot_examples_block() function returns a formatted prompt block
injected into the user_prompt in ai_email_service.py and sequence_generator.py.

Signatures and unsubscribe footers are stripped from examples — the system's
finalize_email_body() appends the standard footer and {{your_name}} token
automatically to every generated email, so including them here would duplicate them.

To add more examples: add a new dict to EMAIL_EXAMPLES[persona_type].
No changes needed in service files or prompt functions.
"""

from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Healthcare / Pharma industry detection
# ---------------------------------------------------------------------------

HEALTHCARE_PHARMA_KEYWORDS = {
    "healthcare",
    "pharma",
    "pharmaceutical",
    "specialty pharmacy",
    "biotech",
    "biopharma",
    "life sciences",
    "health system",
    "hospital",
    "clinic",
    "medical",
    "health tech",
    "healthtech",
    "med tech",
    "medtech",
    "rx",
    "hub",
    "payer",
}


def is_healthcare_pharma(industry: str) -> bool:
    """Return True if the prospect's industry is healthcare/pharma-related."""
    if not industry:
        return False
    industry_lower = industry.lower()
    return any(kw in industry_lower for kw in HEALTHCARE_PHARMA_KEYWORDS)


# ---------------------------------------------------------------------------
# Example email bank
# Each entry:
#   industry_tag  — lowercase substring to match against prospect.industry
#                   None = applies to all industries
#   email_type    — "intro" | "followup_1" | "followup_2" | "followup_3"
#                                          | "followup_4" | "followup_5"
#   subject       — subject line
#   body          — tokenized body (no signature, no unsubscribe footer)
#   what_works    — annotation explaining the structure for the LLM
# ---------------------------------------------------------------------------

EMAIL_EXAMPLES: Dict[str, List[Dict]] = {

    "MARKET_ACCESS": [
        {
            "industry_tag": None,
            "email_type": "intro",
            "subject": "Accelerating Innovation for {{company_name}}",
            "body": (
                "Hi {{first_name}},\n\n"
                "Are manual processes, data silos, and compliance hurdles slowing your growth?\n\n"
                "We at <<SENDER_COMPANY>> — a U.S.-based AI First Healthcare Tech Solutions Company "
                "with presence in US, Costa Rica and India — have been helping Pharma Hubs and Specialty "
                "Pharmacies like yours accelerate innovation and performance with Automation, AI, Data "
                "Engineering, Cloud, DevOps, Quality Engineering, Custom Application Development, and "
                "Salesforce solutions.\n\n"
                "What we're solving for with our US Healthcare Capabilities:\n"
                "    • Patient Enrollment/Intake\n"
                "    • Benefits Investigation (BI)\n"
                "    • Prior Authorization (PA)\n"
                "    • Financial Assistance (CoPay/PAP)\n"
                "    • Pharmacy Triage and Dispensing\n"
                "    • Patient Adherence/Engagement\n"
                "    • Hub Portal/CRM Integration\n"
                "    • Claims & Billing Support\n\n"
                "Let's connect for a quick, no-pressure call to explore how we can supercharge your operations."
            ),
            "what_works": (
                "Opens with a direct pain point question relevant to pharma ops (manual processes, data silos, compliance). "
                "Introduces <<SENDER_COMPANY>> with credibility markers (US-based, AI First, multi-country presence). "
                "Uses a focused capability bullet list so the prospect can self-identify their need instantly. "
                "Closes with a soft, no-pressure call ask."
            ),
        },
        {
            "industry_tag": None,
            "email_type": "followup_1",
            "subject": "Streamlining Specialty Pharmacy Operations",
            "body": (
                "Hi {{first_name}},\n\n"
                "Workflow gaps and staffing issues are slowing down specialty pharmacies — "
                "and hitting drug delivery and patient engagement hard.\n\n"
                "<<SENDER_COMPANY>>' smart automation tools help streamline operations, cut manual work, "
                "and free up your team for high-impact tasks. We've partnered with top players to drive "
                "results through AI, cloud, and custom automation.\n\n"
                "Quick snapshot:\n"
                "    • Automated PAP Intake: OCR + NLP + validation\n"
                "    • Payer system integrations + BI workflows\n"
                "    • Bots for PA submissions\n"
                "    • End-to-end PAP automation + custom copay engines\n\n"
                "Up for a quick 15–20 min virtual chat next week to see how we can help?\n\n"
                "Let me know what works for you!"
            ),
            "what_works": (
                "Opens with a specific operational reality for specialty pharmacy (workflow gaps, staffing). "
                "Positions <<SENDER_COMPANY>> as an automation partner with proven partnerships. "
                "Capability bullets are specific and technical (OCR + NLP, BI workflows, PA bots) — "
                "signals deep domain knowledge to the reader. "
                "CTA is conversational and low-friction (15-20 min, next week)."
            ),
        },
        {
            "industry_tag": None,
            "email_type": "followup_2",
            "subject": "Accelerate R&D and Reduce Costs with Data & AI",
            "body": (
                "Hi {{first_name}},\n\n"
                "Just following up on my earlier note.\n\n"
                "We often see pharma teams bogged down by manual reconciliation across systems or sluggish "
                "batch processes. <<SENDER_COMPANY>>' AI automation is built to fix that — cutting errors "
                "and speeding up operations significantly.\n\n"
                "And when it comes to R&D, our Data Engineering and AI services help break down silos, "
                "unify research data, and accelerate innovation — reducing time-to-market while improving ROI.\n\n"
                "What sets <<SENDER_COMPANY>> apart:\n"
                "• Integrated pharmacy + CRM workflows with order logic\n"
                "• Patient outreach via SMS nudges & chatbot frameworks\n"
                "• Seamless CRM integrations: Salesforce Health Cloud, Dynamics, Experience Cloud\n"
                "• Full RCM coding platform tailored to pharma ops\n\n"
                "Let's show you what it looks like in action. Book a no-obligation demo — "
                "we'd love to share how it could work for your team."
            ),
            "what_works": (
                "Brief follow-up acknowledgment keeps continuity without being apologetic. "
                "Addresses two distinct pain points: manual reconciliation and R&D data silos. "
                "Differentiator bullets highlight CRM integrations (Salesforce, Dynamics) by name — "
                "signals real implementation depth. "
                "CTA escalates slightly to a demo ask, appropriate for a second follow-up."
            ),
        },
        {
            "industry_tag": None,
            "email_type": "followup_3",
            "subject": "Stay Ahead of Compliance and Data Security Risks with AI and Automation",
            "body": (
                "Hi {{first_name}},\n\n"
                "Just looping back quickly.\n\n"
                "At <<SENDER_COMPANY>>, we help healthcare teams put AI to work — from improving care and "
                "streamlining operations to driving smarter R&D. We're always keeping an eye on the AI space — "
                "making sure our solutions stay innovative while checking all the boxes for healthcare "
                "compliance like HIPAA and GDPR.\n\n"
                "For Biopharma organizations, data security and compliance are key. Our Cloud and Quality "
                "Engineering services keep systems secure, audit-ready, and built to protect sensitive "
                "data end-to-end.\n\n"
                "How we help teams like {{company_name}}:\n"
                "• OCR for faxed forms, AI-powered classification, e-signature tracking\n"
                "• Eligibility bots, API plan checks, co-pay tier extraction & data normalization\n"
                "• PA form submission automation + RPA status check routines\n"
                "• Copay eligibility checks, rule validation & card dispatch integrations\n\n"
                "Want to chat next week? Happy to jump on a quick call or send a short overview — "
                "whichever's easier for you."
            ),
            "what_works": (
                "Compliance/HIPAA angle differentiates this touchpoint from the previous two. "
                "Names specific technical capabilities (OCR, RPA, eligibility bots, co-pay tier extraction) "
                "that demonstrate implementation depth beyond generic AI claims. "
                "References {{company_name}} in the capability section to personalize. "
                "CTA gives two options (call or overview) — reduces friction by letting prospect choose."
            ),
        },
        {
            "industry_tag": None,
            "email_type": "followup_4",
            "subject": "Boost Patient Engagement & Access with Salesforce",
            "body": (
                "Hi {{first_name}},\n\n"
                "Circling back one last time.\n\n"
                "At <<SENDER_COMPANY>>, we understand how vital patient access and engagement are for "
                "specialty pharmacy success. Our Salesforce implementations bring it all together — "
                "streamlining compliance, connecting data, and giving your teams actionable insights "
                "for better outcomes.\n\n"
                "Here's how we've been helping pharma teams:\n"
                "• Automating pharmacy routine rules & inventory checks\n"
                "• SMS/email reminders, chatbots for refills & missed doses\n"
                "• Workflow automation, task assignments, CRM syncing\n"
                "• Claims uploads, denial tracking, and RPA-driven rebills\n\n"
                "Think Salesforce can do more for your team? Let's connect and explore what that could look like."
            ),
            "what_works": (
                "Salesforce-specific angle opens a new conversation track for prospects already using SFDC. "
                "Patient access and engagement framing speaks to outcomes, not just technology. "
                "Capability bullets are Salesforce-native (CRM syncing, chatbots, RPA rebills). "
                "CTA is curiosity-based ('Think Salesforce can do more?') — invites rather than pushes."
            ),
        },
        {
            "industry_tag": None,
            "email_type": "followup_5",
            "subject": "Custom Application Development for Pharma Innovation",
            "body": (
                "Hi {{first_name}},\n\n"
                "I've reached out a few times and just wanted to reconnect. At <<SENDER_COMPANY>>, "
                "we specialize in helping specialty pharmacy teams like yours improve patient care and "
                "boost efficiency with custom digital engineering and automation.\n\n"
                "We know every pharma business is different — that's why our app development services "
                "are built around your unique needs. From mobile tools for field reps to powerful "
                "analytics dashboards, everything is designed to integrate seamlessly and scale "
                "with your growth.\n\n"
                "Here's how we support teams like yours:\n"
                "• Faster patient onboarding & prior auth workflows\n"
                "• Smarter medication adherence programs\n"
                "• Real-time visibility into operations\n"
                "• Better experiences for both patients & providers\n\n"
                "Curious about what a tailored solution could look like? "
                "Let's hop on a quick intro call and explore some possibilities."
            ),
            "what_works": (
                "Acknowledges multiple prior outreaches gracefully without being apologetic. "
                "Custom development angle is a distinct value proposition from automation/AI in earlier emails. "
                "Outcome-focused bullets (onboarding speed, adherence, real-time visibility) speak to "
                "business results rather than technology features. "
                "CTA is exploratory ('explore some possibilities') — appropriately low-pressure for a 5th touch."
            ),
        },
    ],

    "OPERATIONS_PHARMACY": [
        {
            "industry_tag": None,
            "email_type": "intro",
            "subject": "Streamlining Operations for {{company_name}}",
            "body": (
                "Hi {{first_name}},\n\n"
                "Are manual processes, data silos, and compliance hurdles slowing your growth?\n\n"
                "We at <<SENDER_COMPANY>> — a U.S.-based AI First Healthcare Tech Solutions Company — "
                "have been helping Pharma Hubs and Specialty Pharmacies streamline operations and reduce "
                "manual workload with Automation, AI, Data Engineering, and Custom Application Development.\n\n"
                "What we're solving for with our US Healthcare Capabilities:\n"
                "    • Patient Enrollment/Intake\n"
                "    • Benefits Investigation (BI)\n"
                "    • Prior Authorization (PA)\n"
                "    • Financial Assistance (CoPay/PAP)\n"
                "    • Pharmacy Triage and Dispensing\n"
                "    • Patient Adherence/Engagement\n"
                "    • Hub Portal/CRM Integration\n"
                "    • Claims & Billing Support\n\n"
                "Let's connect for a quick, no-pressure call to explore how we can help {{company_name}}."
            ),
            "what_works": (
                "Same pain-point hook as the MARKET_ACCESS intro but framed around operational burden "
                "rather than strategic growth — more relevant to ops managers. "
                "Capability bullets map directly to day-to-day pharmacy workflows a manager oversees. "
                "CTA references {{company_name}} specifically."
            ),
        },
        {
            "industry_tag": None,
            "email_type": "followup_1",
            "subject": "Reducing Manual Work in Specialty Pharmacy Ops",
            "body": (
                "Hi {{first_name}},\n\n"
                "Workflow gaps and staffing issues are slowing down specialty pharmacies — "
                "and hitting drug delivery and patient engagement hard.\n\n"
                "<<SENDER_COMPANY>>' smart automation tools help streamline operations, cut manual work, "
                "and free up your team for high-impact tasks.\n\n"
                "Quick snapshot:\n"
                "    • Automated PAP Intake: OCR + NLP + validation\n"
                "    • Payer system integrations + BI workflows\n"
                "    • Bots for PA submissions\n"
                "    • End-to-end PAP automation + custom copay engines\n\n"
                "Up for a quick 15–20 min virtual chat next week to see how we can help {{company_name}}?\n\n"
                "Let me know what works for you!"
            ),
            "what_works": (
                "Operational framing (workflow gaps, staffing) is relevant to a manager's daily challenges. "
                "Technical capability bullets (OCR, PA bots, BI workflows) signal real automation depth. "
                "CTA personalizes to {{company_name}} and keeps the time ask short (15-20 min)."
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# Prompt block builder
# ---------------------------------------------------------------------------

def get_few_shot_examples_block(
    persona_type: str,
    industry: str = None,
    email_type: str = None,
    limit: int = 2,
) -> str:
    """
    Return a formatted prompt block containing few-shot email examples.

    Args:
        persona_type: Blueprint persona type (e.g. "MARKET_ACCESS", "PATIENT_SERVICES_HUB")
        industry:     Prospect's industry string — used for industry_tag matching
        email_type:   Filter by email type ("intro", "followup_1" … "followup_5").
                      None = no filter, return examples of any type.
        limit:        Maximum number of examples to include (default 2)

    Returns:
        Formatted string block for injection into user_prompt, or "" if no
        examples match (safe to interpolate — adds no visible content).
    """
    # Look up examples for the given persona, fall back to OTHER (no dedicated
    # examples today — returns "" further down, which is a safe no-op).
    persona_key = persona_type.upper() if persona_type else "OTHER"
    candidates = EMAIL_EXAMPLES.get(persona_key) or EMAIL_EXAMPLES.get("OTHER", [])

    if not candidates:
        return ""

    # Filter by industry_tag if industry is provided.
    # Strategy: if the prospect's industry matches a tag (by substring OR via the
    # is_healthcare_pharma check for the "healthcare" tag), prefer those examples.
    # Fall back to untagged (None) examples if no tag match.
    if industry:
        industry_lower = industry.lower()
        industry_matched = [
            e for e in candidates
            if e.get("industry_tag") and (
                e["industry_tag"] in industry_lower
                or industry_lower in e["industry_tag"]
                or (e["industry_tag"] == "healthcare" and is_healthcare_pharma(industry))
            )
        ]
        pool = industry_matched if industry_matched else [
            e for e in candidates if not e.get("industry_tag")
        ]
    else:
        pool = candidates

    # Filter by email_type if provided
    if email_type:
        pool = [e for e in pool if e.get("email_type") == email_type]

    if not pool:
        return ""

    selected = pool[:limit]

    lines = [
        "=== SERVICE REFERENCE — Use these actual services in every email ===",
        "The examples below show <<SENDER_COMPANY>>' REAL service offerings and pain points.",
        "USE the specific services, capabilities, and pain points from these examples.",
        "Adapt the framing and angle to the prospect's context — but the services MUST come from this list.",
        "DO NOT invent generic capabilities like 'optimize workflows' or 'ensure data integrity'.",
        "DO NOT copy the opener sentences from examples verbatim — write UNIQUE openers for each email.",
        "",
    ]

    for i, example in enumerate(selected, start=1):
        etype = example.get("email_type", "")
        label = f"--- Example {i} ({etype}) ---" if etype else f"--- Example {i} ---"
        lines.append(label)
        lines.append(f"Subject: {example['subject']}")
        lines.append("")
        lines.append(example["body"])
        lines.append("")
        lines.append(f"Why this works: {example['what_works']}")
        if i < len(selected):
            lines.append("")

    lines.append("")
    lines.append("=== END TARGET FORMAT ===")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persona intelligence block — reasoning context (not example copy) extracted
# from a PersonaBlueprint's tone_rules/openers/value_angles, shared by every
# generation path (single-email and sequence) so persona nuance always reaches
# the LLM regardless of which service is calling it.
# ---------------------------------------------------------------------------

def build_persona_intelligence_block(blueprint) -> str:
    """
    Build a "PERSONA INTELLIGENCE" prompt block from a PersonaBlueprint.

    Returns "" if the blueprint has no persona_context/openers to draw from
    (safe to interpolate unconditionally — adds no visible content).
    """
    if not blueprint:
        return ""

    tone_rules = blueprint.tone_rules or {}
    persona_context = tone_rules.get("persona_context", "")
    language_use = tone_rules.get("language_use", [])
    language_avoid_persona = tone_rules.get("language_avoid", [])
    what_resonates = tone_rules.get("what_resonates", "")
    role_frustrations = blueprint.openers or []    # repurposed: role frustrations, NOT example openers
    change_signals = blueprint.value_angles or []  # repurposed: what types of change matter to this persona

    if not persona_context and not role_frustrations:
        return ""

    frustration_lines = "\n".join(f"- {f}" for f in role_frustrations[:3])
    change_lines = "\n".join(f"- {v}" for v in change_signals[:3])
    lang_use = ", ".join(language_use) if language_use else ""
    lang_avoid = ", ".join(language_avoid_persona) if language_avoid_persona else ""

    return f"""
=== PERSONA INTELLIGENCE (do NOT copy these lines — use them to REASON about this person's world) ===
Who they are: {persona_context}
What they actually struggle with (use this to construct your atomic unit — generate a UNIQUE observation, don't quote these):
{frustration_lines}
What type of change they care about:
{change_lines}
Language they use naturally: {lang_use}
Language that marks you as an outsider: {lang_avoid}
What signals "this person gets my world": {what_resonates}
IMPORTANT: The above is context to help you THINK, not text to copy. Every email must pass the SWAP TEST.
"""


# ---------------------------------------------------------------------------
# Capability pool — all sender services extracted from email examples
# Used for shuffled injection across sequence emails
# ---------------------------------------------------------------------------

NEUTRINO_CAPABILITY_POOL = [
    # Patient/enrollment services
    "Patient Enrollment/Intake",
    "Benefits Investigation (BI)",
    "Prior Authorization (PA)",
    "Financial Assistance (CoPay/PAP)",
    "Pharmacy Triage and Dispensing",
    "Patient Adherence/Engagement",
    "Hub Portal/CRM Integration",
    "Claims & Billing Support",
    # Automation / AI
    "Automated PAP Intake: OCR + NLP + validation",
    "Payer system integrations + BI workflows",
    "Bots for PA submissions",
    "End-to-end PAP automation + custom copay engines",
    "OCR for faxed forms, AI-powered classification, e-signature tracking",
    "Eligibility bots, API plan checks, co-pay tier extraction & data normalization",
    "PA form submission automation + RPA status check routines",
    "Copay eligibility checks, rule validation & card dispatch integrations",
    # CRM / Salesforce
    "Integrated pharmacy + CRM workflows with order logic",
    "Patient outreach via SMS nudges & chatbot frameworks",
    "Seamless CRM integrations: Salesforce Health Cloud, Dynamics, Experience Cloud",
    "Workflow automation, task assignments, CRM syncing",
    # RCM / billing
    "Full RCM coding platform tailored to pharma ops",
    "Claims uploads, denial tracking, and RPA-driven rebills",
    "Automating pharmacy routine rules & inventory checks",
    "SMS/email reminders, chatbots for refills & missed doses",
    # Custom dev / data
    "Faster patient onboarding & prior auth workflows",
    "Smarter medication adherence programs",
    "Real-time visibility into operations",
    "Better experiences for both patients & providers",
    "Custom application development for field reps and analytics dashboards",
    "Data Engineering and AI services to break down silos and unify research data",
]


def get_capability_pool_block() -> str:
    """
    Returns a formatted prompt block listing all <<SENDER_COMPANY>> capabilities.
    The LLM is instructed to pick 4-6 and shuffle them per email.
    """
    cap_lines = "\n".join(f"    • {cap}" for cap in NEUTRINO_CAPABILITY_POOL)
    return f"""=== SENDER CAPABILITY POOL ===
Pick 4–6 capabilities from this list for each email's bullet section.
Use DIFFERENT capabilities in each email — do NOT repeat the same bullets across emails.
DO NOT invent capabilities not in this list.

{cap_lines}

=== END CAPABILITY POOL ==="""


# ---------------------------------------------------------------------------
# Sequence tone table — opener style and CTA pressure per step
# ---------------------------------------------------------------------------

SEQUENCE_TONE_TABLE = [
    {
        "step": 1,
        "email_type": "intro",
        "opener_style": "Open with a direct pain point question specific to the prospect's role and industry. Do not copy the example — write a UNIQUE question tailored to this prospect.",
        "opener_example": "Are [specific operational pain point for this role/industry] slowing your growth?",
        "intro_style": "Full company intro: 'We at <<SENDER_COMPANY>> — a U.S.-based AI First Healthcare Tech Solutions Company with presence in US, Costa Rica and India — have been helping...'",
        "cta_style": "Soft no-pressure call ask.",
        "cta_example": "Let's connect for a quick, no-pressure call to explore how we can help.",
    },
    {
        "step": 2,
        "email_type": "followup_1",
        "opener_style": "Briefly acknowledge the previous outreach (1 short sentence), then follow immediately with a specific operational urgency statement tailored to the prospect's industry (declarative, not a question). Do not copy the example — write your own.",
        "opener_example": "Wanted to follow up on my last note. Workflow gaps and staffing issues are slowing down specialty pharmacies — and hitting drug delivery and patient engagement hard.",
        "intro_style": "Brief: one sentence connecting the urgency to <<SENDER_COMPANY>>' automation and operational services.",
        "cta_style": "Direct time-boxed ask.",
        "cta_example": "Up for a quick 15–20 min virtual chat next week to see how we can help?",
    },
    {
        "step": 3,
        "email_type": "followup_2",
        "opener_style": "Briefly acknowledge it's a follow-up (1 short sentence), then surface a new, specific pain angle relevant to the prospect's industry. Do not copy the example — write your own.",
        "opener_example": "Just following up on my earlier note. We often see teams bogged down by manual reconciliation across systems or sluggish batch processes.",
        "intro_style": "Brief: Connect the new pain angle to <<SENDER_COMPANY>>'s automation and data engineering services.",
        "cta_style": "Escalate to a demo ask.",
        "cta_example": "Let's show you what it looks like in action. Book a no-obligation demo.",
    },
    {
        "step": 4,
        "email_type": "followup_3",
        "opener_style": "Briefly loop back (1 short sentence), then pivot to a compliance or security angle specific to the prospect's industry. Do not copy the example — write your own.",
        "opener_example": "Just looping back quickly. At <<SENDER_COMPANY>>, we help teams put AI to work while checking all the boxes for compliance like HIPAA and GDPR.",
        "intro_style": "Brief: Focus on Cloud, Quality Engineering, data security angle.",
        "cta_style": "Give two options to reduce friction.",
        "cta_example": "Want to chat next week? Happy to jump on a quick call or send a short overview — whichever's easier for you.",
    },
    {
        "step": 5,
        "email_type": "followup_4",
        "opener_style": "Briefly signal this is a final attempt (1 short sentence), then introduce a Salesforce or CRM-specific angle relevant to the prospect. Do not copy the example — write your own.",
        "opener_example": "Circling back one last time. At <<SENDER_COMPANY>>, we understand how vital patient access and engagement are. Our Salesforce implementations bring it all together.",
        "intro_style": "Brief: Focus specifically on Salesforce Health Cloud, CRM syncing, patient engagement.",
        "cta_style": "Curiosity-based question CTA.",
        "cta_example": "Think Salesforce can do more for your team? Let's connect and explore what that could look like.",
    },
    {
        "step": 6,
        "email_type": "followup_5",
        "opener_style": "Acknowledge having reached out before with a warm, non-pushy tone (1 short sentence), then introduce a custom development or tailored-solution angle. Do not copy the example — write your own.",
        "opener_example": "I've reached out a few times and just wanted to reconnect. At <<SENDER_COMPANY>>, we specialize in custom digital engineering and automation.",
        "intro_style": "Brief: Focus on custom app development, tailored solutions, mobile tools, analytics dashboards.",
        "cta_style": "Exploratory low-pressure ask.",
        "cta_example": "Curious about what a tailored solution could look like? Let's hop on a quick intro call and explore some possibilities.",
    },
]


def get_sequence_tone_block(num_emails: int) -> str:
    """
    Returns a per-step tone table for sequence generation.
    Tells the LLM exactly what opener style and CTA pressure to use for each email position.
    """
    steps = SEQUENCE_TONE_TABLE[:num_emails]
    lines = ["=== PER-EMAIL TONE GUIDE ===",
             "Apply the opener style and CTA style for each email position.",
             "IMPORTANT: The opener style applies to the EMAIL BODY only — NOT the subject line.",
             "Subject lines must always be descriptive and topic-focused (never use 'following up', 'checking in', 'looping back', 'circling back', or any re-reach phrase in a subject line).",
             ""]
    for s in steps:
        lines.append(f"email_{s['step']} ({s['email_type']}):")
        lines.append(f"  Opener: {s['opener_style']}")
        lines.append(f"  Example opener: \"{s['opener_example']}\"")
        lines.append(f"  Intro: {s['intro_style']}")
        lines.append(f"  CTA: {s['cta_style']}")
        lines.append(f"  Example CTA: \"{s['cta_example']}\"")
        lines.append("")
    lines.append("=== END TONE GUIDE ===")
    return "\n".join(lines)


def get_step_tone_block(step_number: int) -> str:
    """
    Returns the tone rule for a single email step (used by regenerate endpoint).
    step_number is 1-based.
    """
    entry = next((s for s in SEQUENCE_TONE_TABLE if s["step"] == step_number), SEQUENCE_TONE_TABLE[0])
    return (
        f"=== TONE FOR THIS EMAIL (step {entry['step']} — {entry['email_type']}) ===\n"
        f"Opener style: {entry['opener_style']}\n"
        f"Example opener: \"{entry['opener_example']}\"\n"
        f"Intro style: {entry['intro_style']}\n"
        f"CTA style: {entry['cta_style']}\n"
        f"Example CTA: \"{entry['cta_example']}\"\n"
        f"=== END TONE ==="
    )


# ---------------------------------------------------------------------------
# Conference / In-Person Outreach — Few-Shot Examples
# ---------------------------------------------------------------------------
# These are drawn from real sender outreach samples for HLTH 25, Asembia AXS26,
# and in-person city visits.
#
# email_type values for conference emails:
#   "conference_intro"  — Email 1: event hook + meeting ask
#   "conference_fup1"   — Email 2: confirm attendance + hub capabilities
#   "conference_fup2"   — Email 3: automation/R&D angle
#   "conference_fup3"   — Email 4: compliance/AI angle (final for conference sub-type)
#   "conference_fup4"   — Email 5: Salesforce/CRM angle (in-person visit only)
#   "conference_fup5"   — Email 6: custom dev / tailored solutions (in-person visit only)
#   "conference_fup6"   — Email 7: final breakup (in-person visit only)
# ---------------------------------------------------------------------------

CONFERENCE_EMAIL_EXAMPLES: Dict[str, List[Dict]] = {

    "CONFERENCE": [
        {
            "industry_tag": "healthcare",
            "email_type": "conference_intro",
            "subject": "Accelerating Hub Performance — Meet at {{event_name}}",
            "body": (
                "Hi {{first_name}},\n\n"
                "Really impressive how {{company_name}} has been advancing patient access programs across specialty markets!\n\n"
                "Understanding {{company_name}}'s presence at {{event_name}}, and since our Head of Global Partnerships, Abhi, "
                "will be there at the event too, I thought of checking for the possibility for a meeting at the event. "
                "Would be great to catch up — at the event or even beforehand for a quick hello if you are attending. "
                "If not, please direct to the relevant stakeholder from your team who will be at the {{event_name}} "
                "to initiate a catch up.\n\n"
                "As a way of introduction — we at <<SENDER_COMPANY>> are an AI-first healthcare tech company based in "
                "the U.S., with teams in Costa Rica and India. We've been helping Pharma Hubs and Specialty Pharmacies "
                "like yours speed up innovation and boost performance through Automation, AI, Data Engineering, Cloud, "
                "DevOps, Quality Engineering, Custom App Development, and Salesforce solutions.\n\n"
                "Would be great to catch up and chat on a few of the below:\n"
                "    • Enhancing Patient Experience\n"
                "    • Use of AI with Innovation in Human Data and Drug Development\n"
                "    • Payment Revolution\n"
                "    • New Era of Documentation with Data and AI\n\n"
                "Please let me know your availability for an In-Person meeting at the {{event_name}}. "
                "If not at the event, we could also connect virtually at your convenient time."
            ),
            "what_works": (
                "Opens with a warm compliment ('Kudos') then immediately establishes the shared event context. "
                "Names the <<SENDER_COMPANY>> rep (Abhi) and their title — makes the outreach personal and credible. "
                "Company intro is framed as 'as a way of introduction' — secondary to the meeting ask. "
                "Email 1 bullets are DISCUSSION THEMES (not service details) — appropriate for a conference meeting agenda. "
                "CTA is always dual: in-person first, virtual as fallback. "
                "Includes a stakeholder redirect ask for prospects not attending the event."
            ),
        },
        {
            "industry_tag": "healthcare",
            "email_type": "conference_fup1",
            "subject": "Enhancing Patient Experience — Connect at {{event_name}}",
            "body": (
                "Hi {{first_name}},\n\n"
                "Just following up to see if my previous email reached you and if your plans have been firmed "
                "to be at the {{event_name}}.\n\n"
                "If not at the event this year, we could always connect virtually at your convenience.\n\n"
                "Would love to highlight <<SENDER_COMPANY>>'s US Healthcare Capabilities that could add value:\n"
                "    • Patient Enrollment/Intake\n"
                "    • Benefits Investigation (BI)\n"
                "    • Prior Authorization (PA)\n"
                "    • Financial Assistance (CoPay/PAP)\n"
                "    • Pharmacy Triage and Dispensing\n"
                "    • Patient Adherence/Engagement\n"
                "    • Hub Portal/CRM Integration\n"
                "    • Claims & Billing Support\n\n"
                "Let me know what works for you! Looking forward to catching up at the {{event_name}} or virtually. "
                "If not planning to be at the event, please direct to the relevant stakeholder from {{company_name}} "
                "who will be at the event to catch up."
            ),
            "what_works": (
                "Brief follow-up acknowledgment checks if attendance plans are confirmed — appropriate for a conference email. "
                "Pivots immediately to virtual option if they won't attend. "
                "Follow-up bullets are SERVICE-LEVEL (hub capabilities) — more specific than email 1 theme bullets. "
                "CTA again dual (event + virtual) and adds stakeholder redirect for non-attendees."
            ),
        },
        {
            "industry_tag": "healthcare",
            "email_type": "conference_fup2",
            "subject": "Accelerate R&D and Reduce Costs — {{event_name}} Meeting",
            "body": (
                "Hi {{first_name}},\n\n"
                "Just following up on my earlier note and checking if you would have some time to catch up "
                "at the {{event_name}} if attending, or direct me to someone from the team who is planning to attend.\n\n"
                "We often see pharma teams bogged down by manual reconciliation across systems or sluggish batch "
                "processes. <<SENDER_COMPANY>>'s AI automation is built to fix that — cutting errors and speeding up "
                "operations significantly.\n\n"
                "And when it comes to R&D, our Data Engineering and AI services help break down silos, unify "
                "research data, and accelerate innovation — reducing time-to-market while improving ROI.\n\n"
                "What sets <<SENDER_COMPANY>> apart:\n"
                "    • Integrated pharmacy + CRM workflows with order logic\n"
                "    • Patient outreach via SMS nudges & chatbot frameworks\n"
                "    • Seamless CRM integrations: Salesforce Health Cloud, Dynamics, Experience Cloud\n"
                "    • Full RCM coding platform tailored to pharma ops\n\n"
                "Would love to catch up with you or your team virtually or at the {{event_name}}. "
                "Please suggest your interest with availability."
            ),
            "what_works": (
                "Introduces a NEW capability angle (automation/R&D) different from email 1 theme bullets. "
                "Technical bullets signal implementation depth. "
                "Stakeholder redirect still present — 'or direct me to someone.' "
                "CTA dual as always: event OR virtual."
            ),
        },
        {
            "industry_tag": "healthcare",
            "email_type": "conference_fup3",
            "subject": "Compliance & Data Security — Connect at {{event_name}}",
            "body": (
                "Hi {{first_name}},\n\n"
                "Just looping back quickly — last chance to catch up for a quick coffee meeting at the {{event_name}}. "
                "If not planning to attend this year, we could connect virtually.\n\n"
                "At <<SENDER_COMPANY>>, we help healthcare teams put AI to work — from improving care and streamlining operations "
                "to driving smarter R&D. We're always keeping an eye on the AI space — making sure our solutions stay "
                "innovative while checking all the boxes for healthcare compliance like HIPAA and GDPR.\n\n"
                "For Biopharma organizations, data security and compliance are key. Our Cloud and Quality Engineering "
                "services keep systems secure, audit-ready, and built to protect sensitive data end-to-end.\n\n"
                "How we help teams like {{company_name}}:\n"
                "    • OCR for faxed forms, AI-powered classification, e-signature tracking\n"
                "    • Eligibility bots, API plan checks, co-pay tier extraction & data normalization\n"
                "    • PA form submission automation + RPA status check routines\n"
                "    • Copay eligibility checks, rule validation & card dispatch integrations\n\n"
                "Happy to jump on a quick call before the event or at {{event_name}} — whichever's easier for you."
            ),
            "what_works": (
                "'Just looping back quickly — last chance' signals this is the final conference touch without being rude. "
                "Compliance/HIPAA/GDPR angle is fresh — not used in prior emails in this sequence. "
                "Technical depth bullets (OCR, RPA, eligibility bots) demonstrate specificity. "
                "CTA still dual: event coffee OR virtual call. 'Whichever's easier for you' reduces friction."
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# Conference Sequence Tone Table — per-step opener and CTA rules
# Covers both sub-types:
#   Steps 1–4: Conference (named event, both attending)
#   Steps 5–7: In-Person Visit extension (rep traveling to prospect's city)
# ---------------------------------------------------------------------------

CONFERENCE_SEQUENCE_TONE_TABLE = [
    {
        "step": 1,
        "email_type": "conference_intro",
        "opener_style": (
            "Open with a warm, genuine compliment about the prospect's healthcare work. "
            "Do not copy the example opener verbatim — write a UNIQUE compliment relevant to this prospect's role and company. "
            "BANNED OPENER: 'Kudos on the incredible strides' — NEVER use this phrase or close variants. "
            "Immediately follow with the event hook: name the <<SENDER_COMPANY>> rep (from campaign description) and "
            "confirm they will be at the event. Ask about the possibility of an in-person meeting."
        ),
        "opener_example": (
            "The work {{company_name}} is doing around hub services and adherence is getting well-deserved attention. "
            "Understanding {{company_name}}'s presence at {{event_name}}, and since our Head of Global Partnerships, "
            "Abhi, will be there at the event too, I thought of checking for the possibility for a meeting at the event."
        ),
        "intro_style": (
            "Full company intro framed as 'As a way of introduction': "
            "'We at <<SENDER_COMPANY>> — an AI-first healthcare tech company based in the U.S., with teams in "
            "Costa Rica and India — have been helping Pharma Hubs and Specialty Pharmacies like yours...'"
        ),
        "bullet_style": (
            "Use DISCUSSION THEME bullets (not service bullets) — high-level topics for a conference agenda conversation. "
            "Examples: 'Enhancing Patient Experience', 'Use of AI with Innovation in Human Data and Drug Development', "
            "'Payment Revolution', 'New Era of Documentation with Data and AI'. 3–4 bullets."
        ),
        "cta_style": (
            "Dual in-person + virtual. "
            "Conference sub-type: 'In-Person meeting at [event name]... or virtually.' "
            "In-Person Visit sub-type: 'catch up at your office or over coffee during [rep]'s visit to [city] in [dates]... or connect virtually.' "
            "NEVER say 'at the event' for in-person visits — reference the city/dates instead."
        ),
        "cta_example": (
            "Conference: 'Please let me know your availability for an In-Person meeting at [event name]. If not attending, we could also connect virtually.' "
            "In-Person Visit: 'Would love to catch up at your office or over coffee during Abhi's visit to [city] in [dates]. If not, we could also connect virtually.'"
        ),
    },
    {
        "step": 2,
        "email_type": "conference_fup1",
        "opener_style": (
            "Brief follow-up acknowledgment: check if previous email was received and if attendance at the event is confirmed. "
            "Do not copy the example — write your own unique follow-up wording."
        ),
        "opener_example": (
            "Just following up to see if my previous email reached you and if your plans have been firmed to be at the {{event_name}}."
        ),
        "intro_style": "Brief: pivot to virtual option if they won't attend, then introduce hub service capabilities.",
        "bullet_style": (
            "SERVICE-LEVEL hub capabilities bullet list (more specific than email 1). "
            "Examples: Patient Enrollment/Intake, Benefits Investigation (BI), Prior Authorization (PA), "
            "Financial Assistance (CoPay/PAP), Pharmacy Triage and Dispensing, Patient Adherence/Engagement, "
            "Hub Portal/CRM Integration, Claims & Billing Support."
        ),
        "cta_style": (
            "Dual in-person + virtual. "
            "Conference: reference event name. In-Person Visit: reference city/dates. "
            "NEVER say 'at the event' for in-person visits."
        ),
        "cta_example": (
            "Conference: 'Looking forward to connecting at [event name] or virtually.' "
            "In-Person Visit: 'Looking forward to meeting during Abhi's visit to [city] or connecting virtually.'"
        ),
    },
    {
        "step": 3,
        "email_type": "conference_fup2",
        "opener_style": (
            "Brief follow-up acknowledgment, then introduce a NEW angle: automation / R&D pain point. "
            "Do not repeat the opener from email 2. Do not copy the example — write your own unique version."
        ),
        "opener_example": (
            "Just following up on my earlier note and checking if you would have some time to catch up "
            "at the {{event_name}} if attending, or direct me to someone from the team who is planning to attend."
        ),
        "intro_style": "Brief: connect the automation/R&D angle to <<SENDER_COMPANY>>'s Data Engineering and AI services.",
        "bullet_style": (
            "TECHNICAL DEPTH bullets — CRM integrations, pharmacy-specific automation. "
            "Examples: Integrated pharmacy + CRM workflows with order logic, Patient outreach via SMS nudges & chatbot frameworks, "
            "Seamless CRM integrations: Salesforce Health Cloud, Dynamics, Experience Cloud, Full RCM coding platform."
        ),
        "cta_style": (
            "Dual in-person + virtual. "
            "Conference: reference event name. In-Person Visit: reference city/dates. "
            "NEVER say 'at the event' for in-person visits."
        ),
        "cta_example": (
            "Conference: 'Would love to connect at [event name] or virtually. Please suggest your availability.' "
            "In-Person Visit: 'Would love to catch up during Abhi's visit to [city] or connect virtually. Please suggest your availability.'"
        ),
    },
    {
        "step": 4,
        "email_type": "conference_fup3",
        "opener_style": (
            "Signal this is the final conference touch with a brief, warm looping-back phrase. "
            "Pivot to compliance/AI/HIPAA angle. Mention coffee meeting to reduce the ask. "
            "Do not copy the example — write your own unique final-touch opener."
        ),
        "opener_example": (
            "Just looping back quickly — last chance to catch up for a quick coffee meeting at the {{event_name}}. "
            "If not planning to attend this year, we could connect virtually."
        ),
        "intro_style": "Brief: compliance/HIPAA/GDPR angle — Cloud and Quality Engineering keep systems secure and audit-ready.",
        "bullet_style": (
            "TECHNICAL DEPTH bullets focused on automation and data accuracy. "
            "Examples: OCR for faxed forms + AI-powered classification, Eligibility bots + API plan checks, "
            "PA form submission automation + RPA status check routines, Copay eligibility checks + card dispatch integrations."
        ),
        "cta_style": (
            "Dual in-person (coffee) + virtual. 'Whichever's easier for you' reduces friction. "
            "Conference: reference event name. In-Person Visit: reference city/dates. "
            "NEVER say 'at the event' for in-person visits."
        ),
        "cta_example": (
            "Conference: 'Happy to grab a coffee at [event name] or jump on a quick call — whichever's easier.' "
            "In-Person Visit: 'Happy to meet for coffee during Abhi's visit to [city] or jump on a quick call — whichever works.'"
        ),
    },
    {
        "step": 5,
        "email_type": "conference_fup4",
        "opener_style": (
            "For IN-PERSON VISIT sub-type only. Brief signal this is a follow-up, then Salesforce/CRM angle "
            "specific to the prospect's industry. Reference the visit date window and city. "
            "Do not copy the example — write your own unique opener."
        ),
        "opener_example": (
            "Circling back to check if your calendar has opened up for the possibility of meeting with <<SENDER_COMPANY>>'s "
            "HealthTech Solutions — Abhi — in the last week of Oct between 23rd to 29th."
        ),
        "intro_style": "Brief: Focus specifically on Salesforce Health Cloud, CRM syncing, patient access and engagement.",
        "bullet_style": (
            "Salesforce/CRM-specific bullets. "
            "Examples: Automating pharmacy routine rules & inventory checks, SMS/email reminders + chatbots for refills & missed doses, "
            "Workflow automation + task assignments + CRM syncing, Claims uploads + denial tracking + RPA-driven rebills."
        ),
        "cta_style": "Dual in-person (visit window + city) + virtual. Propose a specific time slot for the virtual option.",
        "cta_example": "How about next week Wed 1pm for our chat? Also, we could catch up in person at your office or over coffee in the last week of Oct.",
    },
    {
        "step": 6,
        "email_type": "conference_fup5",
        "opener_style": (
            "For IN-PERSON VISIT sub-type only. Acknowledge reaching out a few times with a warm, non-pushy tone. "
            "Custom app development / tailored solutions angle. "
            "Do not copy the example — write your own unique reconnect opener."
        ),
        "opener_example": (
            "I've reached out a few times and just wanted to reconnect. "
            "Our Head HealthTech Solutions will be in your area in the last week of Oct and was looking forward to meeting you."
        ),
        "intro_style": "Brief: Focus on custom app development, tailored solutions, mobile tools for field reps, analytics dashboards.",
        "bullet_style": (
            "Patient-outcome focused bullets to refresh the angle. "
            "Examples: Faster patient onboarding & prior auth workflows, Smarter medication adherence programs, "
            "Real-time visibility into operations, Better experiences for both patients & providers."
        ),
        "cta_style": "Dual in-person (city + visit window) + virtual (quick intro call). Exploratory, low-pressure.",
        "cta_example": "Curious about what a tailored solution could look like? Let's hop on a quick intro call — and also catch up over coffee when Abhi will be in your area in the last week of Oct.",
    },
    {
        "step": 7,
        "email_type": "conference_fup6",
        "opener_style": (
            "For IN-PERSON VISIT sub-type only. Final breakup email. "
            "Acknowledge the prospect is busy and this may not be the priority right now. Warm close. "
            "Do not copy the example — write your own unique final-touch opener."
        ),
        "opener_example": (
            "I understand you've got a lot on your plate — and maybe this isn't the top priority right now. "
            "Still, I wanted to reach out one last time."
        ),
        "intro_style": "Brief: RPA / manual task automation — the broadest, most relatable angle for a final touch.",
        "bullet_style": "No bullets or just 1 line. Keep it short and human. Do NOT list capabilities.",
        "cta_style": "Zero-pressure close. 'Just reply to this note when you're ready.' No meeting ask, no link.",
        "cta_example": "If you'd like to explore how this could work for your team, I'd be glad to hop on a quick intro call — no pitch, just insights. Just reply to this note when you're ready.",
    },
]


def get_conference_few_shot_examples_block(
    industry: str = None,
    email_type: str = None,
    limit: int = 2,
) -> str:
    """
    Return formatted few-shot examples for conference/in-person outreach emails.
    Parallel to get_few_shot_examples_block() but pulls from CONFERENCE_EMAIL_EXAMPLES.

    Args:
        industry:   Prospect's industry string — used for industry_tag matching
        email_type: Filter by email type ("conference_intro", "conference_fup1", etc.)
                    None = return examples of any type
        limit:      Maximum number of examples to include (default 2)
    """
    candidates = CONFERENCE_EMAIL_EXAMPLES.get("CONFERENCE", [])

    if not candidates:
        return ""

    # Industry filtering — same logic as get_few_shot_examples_block
    if industry:
        industry_lower = industry.lower()
        industry_matched = [
            e for e in candidates
            if e.get("industry_tag") and (
                e["industry_tag"] in industry_lower
                or industry_lower in e["industry_tag"]
                or (e["industry_tag"] == "healthcare" and is_healthcare_pharma(industry))
            )
        ]
        pool = industry_matched if industry_matched else [
            e for e in candidates if not e.get("industry_tag")
        ]
    else:
        pool = candidates

    # Filter by email_type if provided
    if email_type:
        pool = [e for e in pool if e.get("email_type") == email_type]

    if not pool:
        return ""

    selected = pool[:limit]

    lines = [
        "=== CONFERENCE OUTREACH STRUCTURE REFERENCE ===",
        "These examples show the STRUCTURE and TONE for conference outreach emails.",
        "DO NOT copy these examples verbatim. Adapt the framing, angle, and wording to the prospect's context.",
        "USE the structural pattern: warm opener → event hook → company intro → capability bullets → dual CTA.",
        "VARY the opener wording, bullet topics, and CTA phrasing in each email you generate.",
        "CTA: ALWAYS dual — in-person first, virtual as fallback. Never single-option.",
        "IMPORTANT: These examples use {{event_name}} tokens for a CONFERENCE sub-type.",
        "For IN-PERSON VISIT sub-type (no named event): replace 'at the event' / 'at {{event_name}}' with 'during [rep]'s visit to [city]' or 'at your office or over coffee'.",
        "'Following up', 'would love to', 'looping back' are ALLOWED in this context.",
        "",
    ]

    for i, example in enumerate(selected, start=1):
        etype = example.get("email_type", "")
        label = f"--- Example {i} ({etype}) ---" if etype else f"--- Example {i} ---"
        lines.append(label)
        lines.append(f"Subject: {example['subject']}")
        lines.append("")
        lines.append(example["body"])
        lines.append("")
        lines.append(f"Why this works: {example['what_works']}")
        if i < len(selected):
            lines.append("")

    lines.append("")
    lines.append("=== END CONFERENCE REFERENCE ===")

    return "\n".join(lines)


def get_conference_sequence_tone_block(num_emails: int) -> str:
    """
    Returns a per-step tone table for conference/in-person sequence generation.
    Parallel to get_sequence_tone_block() but uses CONFERENCE_SEQUENCE_TONE_TABLE.
    """
    steps = CONFERENCE_SEQUENCE_TONE_TABLE[:num_emails]
    lines = [
        "=== PER-EMAIL TONE GUIDE (CONFERENCE / IN-PERSON OUTREACH) ===",
        "Apply the opener style, bullet style, and CTA style for each email position exactly.",
        "SUBJECT LINES: Lead with a VALUE PROPOSITION or specific topic + event name. Title Case.",
        "Each email MUST have a DIFFERENT topic-specific subject. BANNED: 'quick catch up', 'let's catch up', 'let's meet'.",
        "Following up, would love to, looping back are ALLOWED in body copy for this email type.",
        "",
    ]
    for s in steps:
        lines.append(f"email_{s['step']} ({s['email_type']}):")
        lines.append(f"  Opener: {s['opener_style']}")
        lines.append(f"  Example opener: \"{s['opener_example']}\"")
        lines.append(f"  Intro: {s['intro_style']}")
        lines.append(f"  Bullets: {s['bullet_style']}")
        lines.append(f"  CTA: {s['cta_style']}")
        lines.append(f"  Example CTA: \"{s['cta_example']}\"")
        lines.append("")
    lines.append("=== END CONFERENCE TONE GUIDE ===")
    return "\n".join(lines)


def get_conference_step_tone_block(step_number: int) -> str:
    """
    Returns the conference tone rule for a single email step (used by regenerate endpoint).
    step_number is 1-based.
    """
    entry = next(
        (s for s in CONFERENCE_SEQUENCE_TONE_TABLE if s["step"] == step_number),
        CONFERENCE_SEQUENCE_TONE_TABLE[0]
    )
    return (
        f"=== TONE FOR THIS EMAIL (step {entry['step']} — {entry['email_type']}) ===\n"
        f"Opener style: {entry['opener_style']}\n"
        f"Example opener: \"{entry['opener_example']}\"\n"
        f"Intro style: {entry['intro_style']}\n"
        f"Bullet style: {entry['bullet_style']}\n"
        f"CTA style: {entry['cta_style']}\n"
        f"Example CTA: \"{entry['cta_example']}\"\n"
        f"=== END TONE ==="
    )
