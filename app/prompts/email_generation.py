# app/prompts/email_generation.py
"""
Prompts for AI email generation.
Organized in dedicated folder for easy management.
"""


def get_system_prompt(tone: str, avoid_list: list) -> str:
    """
    System prompt for email generation.
    
    Args:
        tone: Writing style (technical, executive, friendly, etc.)
        avoid_list: List of words/phrases to avoid
    """
    return f"""You are an expert B2B sales email writer. 
Write a short, personalized cold email for sales outreach.

TONE: {tone}
AVOID: {', '.join(avoid_list) if avoid_list else 'generic language'}

RULES:
- Keep it under 100 words
- Be specific and personal
- Focus on their problems, not your features
- End with a soft, low-pressure CTA
- No salesy language like "revolutionize", "game-changer"
- Write like a human, not a template
- Don't use "I hope this email finds you well"
- Don't mention that you "noticed" or "saw" things about them"""


def get_email_generation_prompt(
    first_name: str,
    designation: str,
    company_name: str,
    industry: str,
    product_name: str,
    product_description: str,
    example_openers: list,
    example_ctas: list
) -> str:
    """
    User prompt for email generation.
    """
    return f"""Write a cold email for:
- Name: {first_name}
- Role: {designation}
- Company: {company_name}
- Industry: {industry}

Product: {product_name}
{f'Description: {product_description}' if product_description else ''}

Example openers from our style guide:
{chr(10).join(f'- {o}' for o in example_openers[:2])}

Example CTAs:
{chr(10).join(f'- {c}' for c in example_ctas[:2])}

Return a JSON object with:
{{"subject": "...", "body": "..."}}"""


def get_persona_inference_prompt(designation: str, company_name: str, industry: str) -> str:
    """
    Prompt for inferring persona from prospect data.
    Used when rule-based classification fails or needs enhancement.
    """
    return f"""Analyze this prospect and determine their persona:
- Designation: {designation}
- Company: {company_name}
- Industry: {industry}

Return a JSON object with:
{{
    "persona_type": "PATIENT_SERVICES_HUB" | "MARKET_ACCESS" | "OPERATIONS_PHARMACY" | "TECHNOLOGY_DATA_DIGITAL" | "INNOVATION_STRATEGY_PRODUCT" | "OTHER",
    "seniority": "junior" | "mid" | "senior" | "executive",
    "likely_pain_points": ["..."],
    "preferred_tone": "technical" | "business" | "friendly",
    "confidence": 0.0-1.0
}}"""
