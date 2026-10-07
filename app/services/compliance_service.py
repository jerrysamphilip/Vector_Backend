# app/services/compliance_service.py
"""
Compliance Service for email content validation.
Checks spam triggers, GDPR/CAN-SPAM requirements, disallowed phrases,
and provides an enhanced pre-send "linter" for anti-spam mitigation.
"""

import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from sqlalchemy.orm import Session
import uuid
import logging

from app.models import ComplianceCheck, EmailTemplate
from app.core.config import settings

logger = logging.getLogger(__name__)

# ============================================================
# SPAM TRIGGERS (Words/phrases that increase spam score)
# ============================================================

SPAM_TRIGGERS = {
    # High severity (10 points each)
    "high": [
        "FREE!!!", "ACT NOW!!!", "LIMITED TIME OFFER!!!",
        "URGENT!!!", "WINNER", "CONGRATULATIONS!!!",
        "100% FREE", "NO OBLIGATION", "RISK FREE",
        "CASH BONUS", "EARN MONEY", "MAKE MONEY FAST",
    ],
    # Medium severity (5 points each)
    "medium": [
        "act now", "limited time", "urgent", "don't miss",
        "exclusive deal", "special promotion", "free trial",
        "no cost", "zero cost", "guaranteed",
        "best price", "lowest price", "save big",
    ],
    # Low severity (2 points each)
    "low": [
        "click here", "click below", "order now",
        "buy now", "subscribe now", "sign up today",
        "limited offer", "one time offer", "special offer",
    ],
}


# ============================================================
# DISALLOWED PHRASES (Block content with these)
# ============================================================

DISALLOWED_PHRASES = [
    # Deceptive
    "this is not spam",
    "you have been selected",
    "you are a winner",
    "claim your prize",
    
    # Aggressive
    "don't delete this",
    "final warning",
    "your account will be closed",
    "immediate action required",
    
    # Misleading
    "as seen on tv",
    "celebrity endorsed",
    "secret method",
    "miracle cure",
]


# ============================================================
# PERSONAL EMAIL DOMAINS (Business email validation)
# ============================================================

PERSONAL_DOMAINS = [
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "aol.com", "icloud.com", "mail.com", "protonmail.com",
    "ymail.com", "live.com", "msn.com", "me.com",
    "inbox.com", "zoho.com", "gmx.com", "fastmail.com",
    "googlemail.com", "pm.me", "tutanota.com", "hey.com",
    "rocketmail.com", "att.net", "comcast.net", "verizon.net",
]


# ============================================================
# SALESPERSON PERSONA PHRASES (AI-detectable language)
# ============================================================

SALESPERSON_PHRASES = [
    "comprehensive solution",
    "streamline your",
    "leverage our",
    "synergy",
    "cutting-edge",
    "state-of-the-art",
    "game-changing",
    "revolutionary",
    "unlock your",
    "unlock the",
    "transform your",
    "supercharge",
    "skyrocket",
    "empower your",
    "next-generation",
    "best-in-class",
    "world-class",
    "seamless integration",
    "robust platform",
    "scalable solution",
    "holistic approach",
    "paradigm shift",
    "disruptive",
    "end-to-end",
    "one-stop shop",
    "turnkey solution",
]


# ============================================================
# IMPERATIVE VERBS (Action verbs typical of spam/phishing)
# ============================================================

IMPERATIVE_VERBS = [
    "click", "verify", "book", "schedule", "respond",
    "try", "act", "claim", "register", "download",
    "subscribe", "confirm", "activate", "redeem",
    "grab", "seize", "hurry", "rush", "order",
    "apply", "enroll", "join", "start", "begin",
    "reserve", "secure", "unlock", "access", "open",
]


# ============================================================
# HIGH-RISK URL SHORTENERS
# ============================================================

DEFAULT_BLOCKED_SHORTENERS = [
    "bit.ly", "tinyurl.com", "t.co", "goo.gl",
    "ow.ly", "is.gd", "buff.ly", "rebrand.ly",
    "tiny.cc", "lnkd.in", "soo.gd", "s2r.co",
]


# ============================================================
# LINT RESULT DATA CLASS
# ============================================================

@dataclass
class LintWarning:
    """A single lint warning from the pre-send analysis."""
    category: str       # e.g. "SUBJECT", "ASF", "LINGUISTIC", "LINK", "CONTENT"
    severity: str       # "HIGH", "MEDIUM", "LOW"
    message: str        # Human-readable description
    score_impact: float # Points added to composite score


@dataclass
class LintResult:
    """Aggregated result from the pre-send email linter."""
    composite_score: float = 0.0
    warnings: List[LintWarning] = field(default_factory=list)
    passed: bool = True

    def add(self, warning: LintWarning):
        self.warnings.append(warning)
        self.composite_score += warning.score_impact
        if warning.severity == "HIGH":
            self.passed = False

    @property
    def trigger_names(self) -> List[str]:
        return [w.message for w in self.warnings]


# ============================================================
# COMPLIANCE SERVICE FUNCTIONS (original)
# ============================================================

def calculate_spam_score(subject: str, body: str) -> Tuple[float, List[str]]:
    """
    Calculate spam score based on trigger words.
    
    Returns:
        Tuple of (score 0-100, list of triggers found)
    """
    content = f"{subject} {body}".lower()
    score = 0.0
    triggers_found = []
    
    # Check high severity
    for trigger in SPAM_TRIGGERS["high"]:
        if trigger.lower() in content:
            score += 10
            triggers_found.append(trigger)
    
    # Check medium severity
    for trigger in SPAM_TRIGGERS["medium"]:
        if trigger.lower() in content:
            score += 5
            triggers_found.append(trigger)
    
    # Check low severity
    for trigger in SPAM_TRIGGERS["low"]:
        if trigger.lower() in content:
            score += 2
            triggers_found.append(trigger)
    
    # Cap at 100
    score = min(score, 100.0)
    
    return (score, triggers_found)


def check_disallowed_phrases(subject: str, body: str) -> Tuple[bool, List[str]]:
    """
    Check for disallowed phrases in content.
    
    Returns:
        Tuple of (has_disallowed, list of found phrases)
    """
    content = f"{subject} {body}".lower()
    found = []
    
    for phrase in DISALLOWED_PHRASES:
        if phrase.lower() in content:
            found.append(phrase)
    
    return (len(found) > 0, found)


def check_unsubscribe_link(body: str) -> bool:
    """Check if body contains unsubscribe link or text."""
    patterns = [
        r"unsubscribe",
        r"opt.out",
        r"remove.from.list",
        r"stop.receiving",
    ]
    
    body_lower = body.lower()
    for pattern in patterns:
        if re.search(pattern, body_lower):
            return True
    
    return False


def is_business_email(email: str) -> bool:
    """Check if email is a business email (not personal domain)."""
    if not email or "@" not in email:
        return False
    
    domain = email.split("@")[-1].lower()
    return domain not in PERSONAL_DOMAINS


def check_personal_data(body: str) -> Tuple[bool, List[str]]:
    """
    Check for personal data patterns (PII).
    
    Returns:
        Tuple of (contains_pii, list of pii types found)
    """
    pii_patterns = {
        "ssn": r"\b\d{3}-\d{2}-\d{4}\b",  # Social Security Number
        "credit_card": r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",  # Credit Card
        "phone": r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",  # Phone Number
    }
    
    found = []
    for pii_type, pattern in pii_patterns.items():
        if re.search(pattern, body):
            found.append(pii_type)
    
    return (len(found) > 0, found)


# ============================================================
# ENHANCED PRE-SEND LINTER (New)
# ============================================================

def _check_subject_line(subject: str) -> List[LintWarning]:
    """Validate subject line against spam filter heuristics."""
    warnings = []
    if not subject:
        return warnings

    # Length check (>60 chars = mobile truncation + spam signal)
    if len(subject) > 60:
        warnings.append(LintWarning(
            category="SUBJECT",
            severity="MEDIUM",
            message=f"Subject line too long ({len(subject)} chars, max 60). May be truncated on mobile and trigger spam filters.",
            score_impact=3.0,
        ))

    # ALL-CAPS detection (>50% uppercase letters = spammy)
    alpha_chars = [c for c in subject if c.isalpha()]
    if alpha_chars:
        upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
        if upper_ratio > 0.5 and len(alpha_chars) > 5:
            warnings.append(LintWarning(
                category="SUBJECT",
                severity="HIGH",
                message="Subject line has excessive CAPS. ALL-CAPS subjects are a strong spam signal for Microsoft EOP.",
                score_impact=8.0,
            ))

    # Multiple exclamation/question marks
    if re.search(r"[!?]{2,}", subject):
        warnings.append(LintWarning(
            category="SUBJECT",
            severity="MEDIUM",
            message="Subject has multiple consecutive punctuation marks (!! or ??). Triggers spam filters.",
            score_impact=5.0,
        ))

    # Fake reply/forward prefix (Re: / Fwd: abuse)
    if re.match(r"(?i)^\s*(re|fwd|fw)\s*:", subject):
        warnings.append(LintWarning(
            category="SUBJECT",
            severity="HIGH",
            message="Subject uses 'Re:' or 'Fwd:' prefix on a cold email. Microsoft EOP flags this as deceptive.",
            score_impact=10.0,
        ))

    return warnings


def _check_asf_triggers(body: str) -> List[LintWarning]:
    """Check for Microsoft EOP Advanced Spam Filter (ASF) triggers."""
    warnings = []
    if not body:
        return warnings

    # Numeric IP addresses in URLs (e.g. http://192.168.1.1/...)
    if re.search(r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", body):
        warnings.append(LintWarning(
            category="ASF",
            severity="HIGH",
            message="Body contains a URL with a numeric IP address. This is an ASF trigger in Microsoft EOP.",
            score_impact=10.0,
        ))

    # High-risk URL shorteners
    blocked_shorteners = settings.BLOCKED_URL_SHORTENERS.split(",") if settings.BLOCKED_URL_SHORTENERS else DEFAULT_BLOCKED_SHORTENERS
    blocked_shorteners = [s.strip().lower() for s in blocked_shorteners if s.strip()]
    body_lower = body.lower()
    for shortener in blocked_shorteners:
        if shortener in body_lower:
            warnings.append(LintWarning(
                category="ASF",
                severity="MEDIUM",
                message=f"Body contains URL shortener '{shortener}'. Shorteners are flagged by enterprise spam filters.",
                score_impact=5.0,
            ))
            break  # only flag once

    # Excessive remote images (>3 = ASF trigger)
    remote_images = re.findall(r"<img\s+[^>]*src\s*=\s*[\"']https?://", body, re.IGNORECASE)
    if len(remote_images) > 3:
        warnings.append(LintWarning(
            category="ASF",
            severity="MEDIUM",
            message=f"Body has {len(remote_images)} remote images. More than 3 is an ASF trigger in Microsoft EOP.",
            score_impact=4.0,
        ))

    # HTML iframe or JavaScript (strong ASF triggers)
    if re.search(r"<iframe\b", body, re.IGNORECASE):
        warnings.append(LintWarning(
            category="ASF",
            severity="HIGH",
            message="Body contains an <iframe> tag. This is a high-confidence ASF trigger.",
            score_impact=15.0,
        ))
    if re.search(r"<script\b", body, re.IGNORECASE):
        warnings.append(LintWarning(
            category="ASF",
            severity="HIGH",
            message="Body contains a <script> tag. This is a high-confidence ASF trigger.",
            score_impact=15.0,
        ))

    return warnings


def _check_linguistic_signals(subject: str, body: str) -> List[LintWarning]:
    """
    Detect AI-generated / salesperson language patterns.
    Checks imperative verb density and banned persona phrases.
    """
    warnings = []
    content = f"{subject} {body}"
    if not content.strip():
        return warnings

    # --- Imperative verb density ---
    words = re.findall(r"[a-zA-Z]+", content.lower())
    if words:
        imperative_count = sum(1 for w in words if w in IMPERATIVE_VERBS)
        density = (imperative_count / len(words)) * 100
        max_density = settings.MAX_IMPERATIVE_VERB_DENSITY

        if density > max_density:
            warnings.append(LintWarning(
                category="LINGUISTIC",
                severity="MEDIUM",
                message=f"High imperative verb density ({density:.1f}%, threshold {max_density}%). AI-generated content signal.",
                score_impact=5.0,
            ))

    # --- Salesperson persona phrases ---
    content_lower = content.lower()
    found_phrases = []
    for phrase in SALESPERSON_PHRASES:
        if phrase.lower() in content_lower:
            found_phrases.append(phrase)

    if found_phrases:
        severity = "HIGH" if len(found_phrases) >= 3 else "MEDIUM"
        impact = 4.0 * len(found_phrases)
        warnings.append(LintWarning(
            category="LINGUISTIC",
            severity=severity,
            message=f"Salesperson persona detected: {', '.join(found_phrases[:5])}. These phrases correlate with AI-generated spam classification.",
            score_impact=min(impact, 20.0),
        ))

    return warnings


def _check_link_domains(body: str, sender_domain: str) -> List[LintWarning]:
    """Validate that all URLs in body match the sender domain or configured tracking domain."""
    warnings = []
    if not body or not sender_domain:
        return warnings

    # Extract all URLs from body
    urls = re.findall(r"https?://([^/\s\"'<>]+)", body, re.IGNORECASE)
    if not urls:
        return warnings

    allowed_domains = {sender_domain.lower()}
    if settings.CUSTOM_TRACKING_DOMAIN:
        allowed_domains.add(settings.CUSTOM_TRACKING_DOMAIN.lower())
    # Also allow tracking via the BASE_URL domain
    base_url_domain = re.sub(r"https?://", "", settings.BASE_URL).split("/")[0].lower()
    if base_url_domain:
        allowed_domains.add(base_url_domain)

    mismatched = []
    for url_domain in urls:
        url_domain_clean = url_domain.lower().split(":")[0]  # strip port
        # Check if the URL domain matches or is a subdomain of any allowed domain
        if not any(url_domain_clean == d or url_domain_clean.endswith("." + d) for d in allowed_domains):
            mismatched.append(url_domain_clean)

    if mismatched:
        unique_mismatched = list(set(mismatched))[:3]
        warnings.append(LintWarning(
            category="LINK",
            severity="MEDIUM",
            message=f"Links to non-sender domains: {', '.join(unique_mismatched)}. Mismatched link domains trigger impersonation filters.",
            score_impact=5.0,
        ))

    return warnings


def lint_email_content(
    subject: str,
    body: str,
    sender_domain: str = "",
) -> LintResult:
    """
    Pre-send email linter: comprehensive check for spam triggers, ASF signals,
    linguistic patterns, and link validation.

    Returns a LintResult with composite score and detailed warnings.
    This replaces the simpler calculate_spam_score for the pre-send gate.
    """
    result = LintResult()

    # 1. Existing keyword-based spam scoring
    keyword_score, keyword_triggers = calculate_spam_score(subject or "", body or "")
    if keyword_triggers:
        result.add(LintWarning(
            category="CONTENT",
            severity="HIGH" if keyword_score >= 15 else "MEDIUM",
            message=f"Spam trigger words: {', '.join(keyword_triggers[:5])}",
            score_impact=keyword_score,
        ))

    # 2. Disallowed phrases
    has_disallowed, disallowed_found = check_disallowed_phrases(subject or "", body or "")
    if has_disallowed:
        result.add(LintWarning(
            category="CONTENT",
            severity="HIGH",
            message=f"Disallowed phrases: {', '.join(disallowed_found[:5])}",
            score_impact=20.0,
        ))

    # 3. Subject line checks
    for w in _check_subject_line(subject or ""):
        result.add(w)

    # 4. ASF triggers
    for w in _check_asf_triggers(body or ""):
        result.add(w)

    # 5. Linguistic / AI detection
    for w in _check_linguistic_signals(subject or "", body or ""):
        result.add(w)

    # 6. Link domain validation
    for w in _check_link_domains(body or "", sender_domain):
        result.add(w)

    # Cap composite score at 100
    result.composite_score = min(result.composite_score, 100.0)

    # Update passed flag based on threshold
    if result.composite_score >= settings.SPAM_SCORE_BLOCK_THRESHOLD:
        result.passed = False

    return result


# ============================================================
# FULL COMPLIANCE CHECK (original, unchanged)
# ============================================================

def run_compliance_check(
    db: Session,
    template: EmailTemplate,
    region: str = "US"
) -> ComplianceCheck:
    """
    Run full compliance check on an email template.
    
    Args:
        db: Database session
        template: EmailTemplate to check
        region: Target region (US, EU, APAC)
    
    Returns:
        ComplianceCheck object with results
    """
    subject = template.subject or ""
    body = template.body or ""
    
    # Run all checks
    spam_score, spam_triggers = calculate_spam_score(subject, body)
    has_disallowed, disallowed_found = check_disallowed_phrases(subject, body)
    has_unsubscribe = check_unsubscribe_link(body)
    contains_pii, pii_types = check_personal_data(body)
    
    # Determine status
    failure_reasons = []
    
    if spam_score >= 25:
        failure_reasons.append(f"High spam score: {spam_score}")

    if has_disallowed:
        failure_reasons.append(f"Disallowed phrases: {', '.join(disallowed_found)}")

    if not has_unsubscribe:
        failure_reasons.append("Missing unsubscribe link")

    if contains_pii:
        failure_reasons.append(f"Contains personal data: {', '.join(pii_types)}")

    # Determine final status
    if has_disallowed or contains_pii:
        status = "failed"
    elif spam_score >= 25 or not has_unsubscribe:
        status = "warning"
    elif spam_score >= 15:
        status = "warning"
    else:
        status = "passed"
    
    # Create compliance check record
    check = ComplianceCheck(
        check_id=str(uuid.uuid4()),
        template_id=template.template_id,
        spam_score=spam_score,
        spam_triggers_found=spam_triggers if spam_triggers else None,
        has_unsubscribe_link=has_unsubscribe,
        has_physical_address=False,  # Would need to check template
        has_disallowed_phrases=has_disallowed,
        disallowed_phrases_found=disallowed_found if disallowed_found else None,
        contains_personal_data=contains_pii,
        personal_data_types=pii_types if pii_types else None,
        gdpr_compliant=not contains_pii,
        can_spam_compliant=has_unsubscribe,
        region=region,
        status=status,
        failure_reasons=failure_reasons if failure_reasons else None,
    )
    
    db.add(check)
    db.commit()
    db.refresh(check)
    
    return check


def quick_compliance_check(subject: str, body: str) -> Dict:
    """
    Quick compliance check without database.
    Returns dict with results.
    """
    spam_score, spam_triggers = calculate_spam_score(subject, body)
    has_disallowed, disallowed_found = check_disallowed_phrases(subject, body)
    has_unsubscribe = check_unsubscribe_link(body)
    contains_pii, pii_types = check_personal_data(body)
    
    # Determine status
    if has_disallowed or contains_pii:
        status = "failed"
    elif spam_score >= 50 or not has_unsubscribe:
        status = "warning"
    else:
        status = "passed"
    
    return {
        "status": status,
        "spam_score": spam_score,
        "spam_triggers": spam_triggers,
        "has_unsubscribe_link": has_unsubscribe,
        "has_disallowed_phrases": has_disallowed,
        "disallowed_phrases": disallowed_found,
        "contains_personal_data": contains_pii,
        "personal_data_types": pii_types,
    }
