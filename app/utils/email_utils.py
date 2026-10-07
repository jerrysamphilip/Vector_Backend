import re

# app/utils/email_utils.py

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "protonmail.com",
    "zoho.com",
}

CTA_PLACEHOLDER_PATTERN = re.compile(r"^\{+\s*(calendar_link|cta_link)\s*\}+$", re.IGNORECASE)

def parse_email(raw_email):
    # Handle None, NaN (float), or other non-string values
    if raw_email is None or not isinstance(raw_email, str):
        return None
    
    if not raw_email.strip():
        return None

    email = raw_email.strip().lower()
    if "@" not in email:
        return None

    domain = email.split("@")[-1]
    if domain in PERSONAL_EMAIL_DOMAINS:
        return None

    return {
        "email": email,
        "email_type": "BUSINESS",
        "email_provider": domain,
    }


def normalize_unsubscribe_footer(body_content: str) -> str:
    """
    Normalize unsubscribe content to a single canonical footer line.
    Used in preview/generation and sender pipeline for consistent output.
    """
    if not body_content:
        body_content = ""

    text = str(body_content).replace("\r\n", "\n")

    # Remove placeholders and raw tracking unsubscribe links.
    text = re.sub(r"\{\{\s*unsubscribe_link\s*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+/api/tracking/unsubscribe/\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<https?://\S+/api/tracking/unsubscribe/\S+>", "", text, flags=re.IGNORECASE)

    # Remove common unsubscribe/footer variants from LLM output.
    unsubscribe_patterns = [
        r"(?im)^\s*(please\s+)?unsubscribe\b.*$",
        r"(?im)^\s*to\s+unsubscribe\b.*$",
        r"(?im)^.*\bif\s+you\s+do\s+not\s+wish\s+to\s+receive\b.*$",
        r"(?im)^.*\bclick\s+here\s+to\s+unsubscribe\b.*$",
        r"(?im)^.*\bunsubscribe<https?://\S+>.*$",
    ]
    for pattern in unsubscribe_patterns:
        text = re.sub(pattern, "", text)

    # Collapse whitespace noise after cleanup.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Unsubscribe link is appended at send time by EmailSenderService
    # with a per-message tracking URL. Do not add any text-based footer here.

    return text


def normalize_cta_link(cta_link) -> str:
    """
    Normalize CTA link input and treat placeholder-like values as empty.
    """
    if cta_link is None:
        return ""

    value = str(cta_link).strip()
    if not value:
        return ""

    if value.lower() in {"none", "null", "undefined", "n/a", "na"}:
        return ""

    if CTA_PLACEHOLDER_PATTERN.fullmatch(value):
        return ""

    return value


def has_effective_cta_link(cta_link) -> bool:
    """True only when CTA link is an actual configured URL/value, not a placeholder."""
    return bool(normalize_cta_link(cta_link))


def strip_cta_content_no_link(body_content: str) -> str:
    """
    Remove CTA/booking/link lines when campaign CTA link is empty.
    Used as a final safety guard so empty CTA config never sends asks/links.

    IMPORTANT: Only removes SHORT lines (<= 20 words) matching CTA patterns.
    Long body-content paragraphs are NEVER removed even if they contain words
    like 'call', 'connect', or 'explore' embedded in natural prose.
    """
    if not body_content:
        return ""

    text = str(body_content).replace("\r\n", "\n").replace("\r", "\n")

    # Remove lines containing CTA placeholders.
    text = re.sub(r"(?im)^.*\{\{(?:calendar_link|cta_link)\}\}.*$", "", text)
    text = re.sub(r"(?im)^.*\{(?:calendar_link|cta_link)\}.*$", "", text)

    cleaned_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue

        lower = stripped.lower()

        # Always remove lines with raw URLs — these are CTA/link lines.
        if re.search(r"https?://\S+", stripped):
            continue

        # Only apply CTA keyword filtering to SHORT lines (<= 20 words).
        # Long lines are body-content paragraphs — preserve them unconditionally.
        word_count = len(stripped.split())
        if word_count <= 20:
            # Remove explicit CTA/meeting-booking language.
            if re.search(
                r"(?i)\b(schedule|book|booking|calendar|demo|meeting|call|chat|walkthrough|connect|talk|discuss|quick look|learn more|explore)\b",
                stripped,
            ):
                continue

            # Remove question-style asks commonly used as CTA.
            if stripped.endswith("?") and re.search(
                r"(?i)^(open to|would you|can we|shall we|should we|worth|interested|up for|want to|do you have)",
                lower,
            ):
                continue

        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def ensure_core_personalization_tokens(body_content: str) -> str:
    """
    Light validation pass — log when AI output is missing personalization
    tokens but NEVER inject hardcoded sentences.  The AI prompt itself is
    responsible for weaving tokens naturally into the email body.
    """
    if not body_content:
        return body_content

    text = str(body_content).replace("\r\n", "\n").replace("\r", "\n").strip()

    # Just return the content as-is.  Forced injection created robotic
    # openings ("Given your role as {{designation}} at …") that hurt
    # deliverability and read quality.
    return text


def normalize_paragraph_spacing(body: str) -> str:
    """
    Enforce consistent paragraph spacing in plain-text email bodies:

    - Paragraphs (non-bullet blocks) are separated by exactly ONE blank line.
    - Bullet-point lines ("-" / "•" prefix) are kept together with NO blank
      lines between them — a blank line before the block and after it, but
      not inside it.
    - Collapses 3+ consecutive blank lines anywhere down to one blank line.

    This runs AFTER the LLM returns body text so the format is always correct
    regardless of what the model output.
    """
    if not body:
        return body

    text = str(body).replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    out = []

    def _is_bullet(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("- ") or stripped.startswith("• ") or stripped.startswith("* ")

    i = 0
    while i < len(lines):
        line = lines[i]

        if _is_bullet(line):
            # Collect consecutive bullet lines — purge any blank lines between them
            bullet_block = []
            while i < len(lines) and (lines[i].strip() == "" or _is_bullet(lines[i])):
                if _is_bullet(lines[i]):
                    bullet_block.append(lines[i])
                i += 1
            # Ensure a blank line before the bullet block — but NOT when the
            # immediately preceding content line is an intro ending with ":"
            # (e.g. "Here are a few ways it can help:")
            if out and out[-1].strip() != "":
                prev_content = next((l for l in reversed(out) if l.strip()), "")
                if not prev_content.rstrip().endswith(":"):
                    out.append("")
            out.extend(bullet_block)
            # Ensure a blank line after the bullet block
            if i < len(lines) and lines[i].strip() != "":
                out.append("")
        else:
            # out.append(line)
            stripped = line.strip()

            if stripped == "":
                # avoid multiple blank lines
                if out and out[-1] != "":
                    out.append("")
            else:
                # ensure paragraph separation
                if out and out[-1] != "":
                    out.append("")
                out.append(line)

            i += 1

    result = "\n".join(out)

    # Collapse 3+ consecutive blank lines → one blank line
    result = re.sub(r"\n{3,}", "\n\n", result)
    # Formatting safeguards
    result = re.sub(r"^(Hi [^,]+,)\s*", r"\1\n\n", result)
    result = re.sub(r"\.(Best,)", r".\n\n\1", result)
    result = re.sub(r"Best,\n\s*\n", "Best,\n", result)
    result = re.sub(r"\n([^\n]+)\n(To unsubscribe)", r"\n\1\n\n\2", result)
    result = re.sub(r"Regards,\n{2,}", "Regards,\n", result)

    return result.strip()



def extract_latest_message_text(body_content: str) -> str:
    """
    Extract newest human-written portion of an email body.
    Removes quoted history, tracking/link noise, and unsubscribe boilerplate.
    """
    if not body_content:
        return ""

    text = str(body_content)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")

    # Remove obvious tracking and wrapper artifacts.
    text = re.sub(r"^\s*https?://\S*awstrack\S*\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"^\s*\[https?://[^\]]+\]\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"<https?://\S+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[cid:[^\]]+\]", "", text, flags=re.IGNORECASE)

    # Keep only newest content before quoted history starts.
    quote_markers = [
        r"^\s*From:\s.+$",
        r"^\s*On\s.+wrote:\s*$",
        r"^\s*-{2,}\s*On\s.+wrote\s*-{2,}\s*$",
        r"^\s*-----Original Message-----\s*$",
        r"^\s*_{10,}\s*$",
        r"^\s*Sent:\s.+$",
        r"^\s*Subject:\s.+$",
    ]
    cut_index = None
    for marker in quote_markers:
        match = re.search(marker, text, flags=re.IGNORECASE | re.MULTILINE)
        if match and match.start() > 0:
            if cut_index is None or match.start() < cut_index:
                cut_index = match.start()
    if cut_index is not None:
        text = text[:cut_index]

    # Remove unsubscribe/footer/signature boilerplate.
    cleanup_patterns = [
        r"(?im)^\s*(please\s+)?unsubscribe\b.*$",
        r"(?im)^\s*to\s+unsubscribe\b.*$",
        r"(?im)^.*\bif\s+you\s+do\s+not\s+wish\s+to\s+receive\b.*$",
        r"(?im)^.*\breply\s+['\"]?\s*unsubscribe\s*['\"]?.*$",
        r"(?im)^.*\bunsubscribe<https?://\S+>.*$",
        r"(?im)^.*\bbook\s+time\s+to\s+meet\s+with\s+me\b.*$",
        r"(?im)^\s*(regards|best regards|best|thanks),?\s*$",
        r"(?im)^\s*(head\s*[-–]\s*.+|t:\s*.+|a:\s*.+)\s*$",
    ]
    for pattern in cleanup_patterns:
        text = re.sub(pattern, "", text)

    # Remove URL-only lines and collapse blank lines.
    text = re.sub(r"(?im)^\s*https?://\S+\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def format_email_body_html(body: str) -> str:
    """
    Convert a plain-text email body (AI-generated, "\\n"-separated paragraphs
    and "    •" bullet lines) into semantic HTML — real <p> paragraphs and a
    real <ul><li> bullet list — so it renders as properly formatted rich text
    everywhere (editor, preview, sent email) instead of a wall of text with
    literal bullet characters.

    Idempotent: if `body` already contains HTML tags (e.g. saved from the
    rich text editor, or a previous call), it's returned unchanged.

    One HTML block per source line (joined with blank lines, not
    concatenated) so the existing plain-text-oriented pipeline downstream
    (CTA/unsubscribe stripping, which is line-based) keeps working on
    the underlying text.
    """
    if not body:
        return body or ""

    if re.search(r"<[a-z][\s\S]*>", body, re.IGNORECASE):
        return body  # already HTML — don't double-convert

    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = body.replace("\r\n", "\n").split("\n")
    blocks = []
    bullet_buffer = []

    def _flush_bullets():
        if bullet_buffer:
            items = "\n".join(f"<li>{_escape(item)}</li>" for item in bullet_buffer)
            blocks.append(f"<ul>\n{items}\n</ul>")
            bullet_buffer.clear()

    for line in lines:
        stripped = line.strip()
        bullet_match = re.match(r"^[•\-\*]\s+(.*)$", stripped)
        if bullet_match:
            bullet_buffer.append(bullet_match.group(1).strip())
            continue

        _flush_bullets()
        if stripped:
            blocks.append(f"<p>{_escape(stripped)}</p>")

    _flush_bullets()

    return "\n\n".join(blocks)


def finalize_email_body(body: str) -> str:
    """
    Universal email formatter used across generation, editing and sending.
    Ensures consistent formatting everywhere.

    Called more than once on the same content in some flows (e.g. once at
    save time, again at send time after placeholder substitution) — once
    `body` is already HTML, paragraph-spacing normalization is skipped since
    it only understands plain-text "•" bullet lines and would otherwise pull
    the already-formatted <ul>/<li> block apart.
    """
    body = ensure_core_personalization_tokens(body)
    body = normalize_unsubscribe_footer(body)
    if not re.search(r"<[a-z][\s\S]*>", body or "", re.IGNORECASE):
        body = normalize_paragraph_spacing(body)
    body = format_email_body_html(body)
    return body


def build_signature_block(sender_name: str, sender_title: str = None) -> str:
    """
    Build the resolved signature block: name, then title (if set), then the
    company line — used to resolve the {{signature_block}} token at send time.
    """
    from app.core.config import settings

    lines = [sender_name or settings.SENDER_NAME]
    if sender_title and sender_title.strip():
        lines.append(sender_title.strip())
    lines.append("Neutrino Tech Systems")
    return "\n".join(lines)
