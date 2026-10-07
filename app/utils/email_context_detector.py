# app/utils/email_context_detector.py
"""
Detects whether a campaign is cold outreach or conference/pre-event outreach
by scanning the campaign name and description for conference-related keywords.

No UI fields needed — users describe their campaign naturally and the system
auto-detects the context.

Usage:
    from app.utils.email_context_detector import detect_email_context

    context = detect_email_context(campaign_name, campaign_description)
    # Returns "CONFERENCE_PREOUTREACH" or "COLD_OUTREACH"
"""

EMAIL_CONTEXT_COLD_OUTREACH = "COLD_OUTREACH"
EMAIL_CONTEXT_CONFERENCE = "CONFERENCE_PREOUTREACH"

CONFERENCE_KEYWORDS = {
    "conference",
    "summit",
    "expo",
    "trade show",
    "tradeshow",
    "meetup",
    "meet-up",
    "forum",
    "congress",
    "symposium",
    "in-person",
    "in person",
    "booth",
    "attending",
    "we'll be at",
    "we will be at",
    "will be attending",
    "see you at",
    "meet at",
    "connect at",
    "networking event",
    "annual meeting",
    "industry event",
    "we are attending",
}


def detect_email_context(text1: str = "", text2: str = "") -> str:
    """
    Returns 'CONFERENCE_PREOUTREACH' if the combined text contains conference
    keywords, otherwise returns 'COLD_OUTREACH'.

    Args:
        text1: First text to scan (e.g. campaign name).
        text2: Second text to scan (e.g. campaign description / product description).
    """
    combined = f"{text1 or ''} {text2 or ''}".lower()
    if any(kw in combined for kw in CONFERENCE_KEYWORDS):
        return EMAIL_CONTEXT_CONFERENCE
    return EMAIL_CONTEXT_COLD_OUTREACH
