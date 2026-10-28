from email.utils import parseaddr
from typing import Callable, Iterable, List, Optional
import html
import logging
import re

# app/utils/email_utils.py

logger = logging.getLogger(__name__)

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

# ── HTML bodies, block by block ─────────────────────────────────────
# The rich-text editor (TipTap getHTML) saves a body as ONE line of HTML: line-based cleanup must work on its
# blocks instead, or one rule match removes the whole email.

_HTML_BODY_RE = re.compile(r"<[a-z][\s\S]*>", re.IGNORECASE)
_BLOCK_TAGS = r"p|div|li|ul|ol|h[1-6]|blockquote|pre|table|thead|tbody|tr|td|th"
_INLINE_TAGS = r"a|span|strong|b|em|i|u|s|small|font|code"
_BLOCK_BOUNDARY = re.compile(rf"(<br\s*/?>|</?(?:{_BLOCK_TAGS})\b[^>]*>)", re.IGNORECASE)
_DROPPED = "\x00"  # marks the text of a removed block until its emptied elements are removed
_DROPPED_ELEMENT = re.compile(
    rf"<({_BLOCK_TAGS}|{_INLINE_TAGS})\b[^>]*>\s*{_DROPPED}[\s{_DROPPED}]*</\1\s*>", re.IGNORECASE)
_BR_BEFORE_DROPPED = re.compile(rf"<br\s*/?>\s*{_DROPPED}", re.IGNORECASE)
_BR_LEADING_DROPPED = re.compile(rf"(^|<(?!/)[^>]*>)\s*{_DROPPED}\s*<br\s*/?>", re.IGNORECASE)
_TAG_OR_TEXT = re.compile(r"(<[^>]*>)|([^<]+)")


def is_html_body(body: Optional[str]) -> bool:
    """Whether a body is HTML (same test as format_email_body_html and the sender's MIME builder)."""
    return bool(_HTML_BODY_RE.search(body or ""))


def block_text(fragment: str) -> str:
    """The visible text of an HTML fragment: tags removed, entities decoded, whitespace collapsed."""
    text = html.unescape(re.sub(r"<[^>]*>", "", fragment or ""))
    return " ".join(text.replace("\xa0", " ").replace("\u200b", "").split())


def has_visible_text(body: Optional[str]) -> bool:
    """Whether a (plain-text or HTML) body shows any text at all."""
    return bool(block_text(html_to_text(body or "")))


def map_html_text(fragment: str, edit: Callable[[str], str]) -> str:
    """The fragment with ``edit`` applied to its text runs only (never to tags or attribute values)."""
    return _TAG_OR_TEXT.sub(lambda m: m.group(1) or edit(m.group(2)), fragment)


def map_html_blocks(body: str, edit: Callable[[str], Optional[str]]) -> str:
    """
    Apply ``edit`` to the content of each block of an HTML body: the text between block boundaries (<p>, <li>,
    <div>, headings, table cells, <br>). ``edit`` returns the new content, or None to remove the block: its
    element goes with it (and a list left empty, and one adjacent <br>); blocks without text are left alone.
    """
    parts = _BLOCK_BOUNDARY.split(body)
    dropped = False
    for i in range(0, len(parts), 2):  # even indexes: content; odd: the boundary tags
        if not block_text(parts[i]):
            continue
        new = edit(parts[i])
        if new is None:
            parts[i] = map_html_text(parts[i], lambda _text: _DROPPED)
            dropped = True
        else:
            parts[i] = new
    out = "".join(parts)
    if not dropped:
        return out
    previous = None
    while previous != out:
        previous = out
        out = _DROPPED_ELEMENT.sub(_DROPPED, out)
        out = _BR_BEFORE_DROPPED.sub(_DROPPED, out)
        out = _BR_LEADING_DROPPED.sub(lambda m: m.group(1) + _DROPPED, out)
    return out.replace(_DROPPED, "")


# Practical address check: one @, no spaces, a dot-atom local part and a domain of
# letter/digit/hyphen labels with a 2+ letter TLD (rejects "broken@", "has space@x.com").
_EMAIL_RE = re.compile(
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


_ANGLE_ADDRESS = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


def extract_sender_address(source: object) -> Optional[str]:
    """
    Lower-case bare address of a sender such as SES's ``mail.source``: "a@dom.com",
    "Name <a@dom.com>" or '"Last, First" <a@dom.com>'. None when there is no valid address.
    """
    if not source or not isinstance(source, str):
        return None
    _, address = parseaddr(source)
    if "@" not in address:
        # parseaddr gives up on an unquoted comma in the display name: take the <...> part
        match = _ANGLE_ADDRESS.search(source)
        address = match.group(1) if match else ""
    address = address.strip().lower()
    local, _, domain = address.rpartition("@")
    domain = domain.strip(".")
    if not local or not _DOMAIN_RE.match(domain):
        return None
    return f"{local}@{domain}"


def extract_sender_domain(source: object) -> Optional[str]:
    """
    Lower-case domain of a sender (see extract_sender_address), None when there is no valid address.
    Splitting on "@" alone keeps the trailing ">" of the angle form ("dom.com>").
    """
    address = extract_sender_address(source)
    return address.rsplit("@", 1)[1] if address else None


# SES rewrites the Message-ID of every mail it sends to <ses-message-id@email.amazonses.com>
# (region variants: @eu-west-1.amazonses.com, ...), so a reply's In-Reply-To / References name that.
_SES_MESSAGE_ID_RE = re.compile(r"^<?\s*([^<>@\s]+)@(?:[a-z0-9-]+\.)*amazonses\.com\s*>?$", re.IGNORECASE)


def ses_id_from_message_id(header_value: Optional[str]) -> Optional[str]:
    """The bare SES message id inside an SES Message-ID (``<id@email.amazonses.com>``), else None."""
    match = _SES_MESSAGE_ID_RE.match((header_value or "").strip())
    return match.group(1) if match else None


def thread_message_ids(in_reply_to: Optional[str], references: Optional[str]) -> List[str]:
    """In-Reply-To first, then the References chain newest first; unique, in that order."""
    out: List[str] = []
    candidates: Iterable[str] = [in_reply_to or ""] + list(reversed((references or "").split()))
    for value in candidates:
        value = value.strip()
        if value and value not in out:
            out.append(value)
    return out


# Reply / forward prefixes of the common mail clients and languages, repeated ("Re: Fwd: AW: ...")
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|vs|rv|tr|antw|odp|r|ref|res|enc)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*)+",
    re.IGNORECASE)


def normalize_subject(subject: Optional[str]) -> str:
    """Subject without reply/forward prefixes, whitespace collapsed, lower-case ("" when empty)."""
    text = _SUBJECT_PREFIX_RE.sub("", subject or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_email(raw_email) -> Optional[dict]:
    # Handle None, NaN (float), or other non-string values
    if raw_email is None or not isinstance(raw_email, str):
        return None
    
    if not raw_email.strip():
        return None

    email = raw_email.strip().lower()
    if not _EMAIL_RE.match(email):
        return None

    domain = email.split("@")[-1]
    if domain in PERSONAL_EMAIL_DOMAINS:
        return None

    return {
        "email": email,
        "email_type": "BUSINESS",
        "email_provider": domain,
    }


# Unsubscribe / footer wording an LLM (or a user) puts in the body; the sender appends the real line.
_UNSUBSCRIBE_LINE_PATTERNS = (
    r"(?im)^\s*(please\s+)?unsubscribe\b.*$",
    r"(?im)^\s*to\s+unsubscribe\b.*$",
    r"(?im)^.*\bif\s+you\s+do\s+not\s+wish\s+to\s+receive\b.*$",
    r"(?im)^.*\bclick\s+here\s+to\s+unsubscribe\b.*$",
    r"(?im)^.*\bunsubscribe<https?://\S+>.*$",
)
_UNSUBSCRIBE_PLACEHOLDER = re.compile(r"\{\{\s*unsubscribe_link\s*\}\}", re.IGNORECASE)
# In HTML a URL ends at a quote or a tag: "\S+" would run on through the rest of a one-line body
_HTML_TRACKING_UNSUBSCRIBE_URL = re.compile(r"https?://[^\s\"'<>]+/api/tracking/unsubscribe/[^\s\"'<>]*",
                                            re.IGNORECASE)


def normalize_unsubscribe_footer(body_content: str) -> str:
    """
    Normalize unsubscribe content to a single canonical footer line.
    Used in preview/generation and sender pipeline for consistent output.

    Plain text: the matching LINES are removed. HTML (e.g. the rich-text editor's one-line ``getHTML``): only
    the matching BLOCK (<p>, <li>, <div>, a <br>-separated line) is removed, never the rest of the body.
    """
    if not body_content:
        body_content = ""

    text = str(body_content).replace("\r\n", "\n")

    # Remove placeholders and raw tracking unsubscribe links.
    text = _UNSUBSCRIBE_PLACEHOLDER.sub("", text)
    if is_html_body(text):
        text = _HTML_TRACKING_UNSUBSCRIBE_URL.sub("", text)
        text = map_html_blocks(text, lambda block: None if _is_unsubscribe_text(block_text(block)) else block)
    else:
        text = re.sub(r"https?://\S+/api/tracking/unsubscribe/\S+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<https?://\S+/api/tracking/unsubscribe/\S+>", "", text, flags=re.IGNORECASE)
        # Remove common unsubscribe/footer variants from LLM output.
        for pattern in _UNSUBSCRIBE_LINE_PATTERNS:
            text = re.sub(pattern, "", text)

    # Collapse whitespace noise after cleanup.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Unsubscribe link is appended at send time by EmailSenderService
    # with a per-message tracking URL. Do not add any text-based footer here.

    return text


def _is_unsubscribe_text(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _UNSUBSCRIBE_LINE_PATTERNS)


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


_CTA_WORDS = re.compile(
    r"(?i)\b(schedule|book|booking|calendar|demo|meeting|call|chat|walkthrough|connect|talk|discuss|quick look"
    r"|learn more|explore)\b")
_CTA_QUESTION_OPENERS = re.compile(
    r"(?i)^(open to|would you|can we|shall we|should we|worth|interested|up for|want to|do you have)")
_CTA_PLACEHOLDER_TOKEN = re.compile(r"\{\{?\s*(?:calendar_link|cta_link)\s*\}?\}", re.IGNORECASE)
_HTML_ANCHOR = re.compile(r"<a\b[^>]*>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_HTML_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _is_short_cta(text: str) -> bool:
    """
    A SHORT (<= 20 words) CTA / meeting ask: booking language, or a question-style ask. Longer text is a
    body paragraph and is never treated as a CTA, even with 'call', 'connect' or 'explore' in its prose.
    """
    stripped = text.strip()
    if len(stripped.split()) > 20:
        return False
    if _CTA_WORDS.search(stripped):
        return True
    return stripped.endswith("?") and bool(_CTA_QUESTION_OPENERS.search(stripped.lower()))


def _strip_cta_lines(text: str) -> str:
    """Plain text: the lines with a CTA placeholder or a raw URL, and short CTA lines, removed."""
    # Remove lines containing CTA placeholders.
    text = re.sub(r"(?im)^.*\{\{(?:calendar_link|cta_link)\}\}.*$", "", text)
    text = re.sub(r"(?im)^.*\{(?:calendar_link|cta_link)\}.*$", "", text)
    cleaned_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Always remove lines with raw URLs (CTA / link lines) and short CTA lines.
        if stripped and (re.search(r"https?://\S+", stripped) or _is_short_cta(stripped)):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _strip_cta_block(block: str) -> Optional[str]:
    """
    HTML: one block without its links (the anchor markup and bare URLs go, the anchor text stays) and CTA
    placeholders; None (drop the block) when nothing visible remains, when only a link label is left
    ("Visit", "Grab time:": at most 3 words around a removed link) or when it is a short CTA ask.
    """
    original = block
    block = _CTA_PLACEHOLDER_TOKEN.sub("", block)
    block = _HTML_ANCHOR.sub(lambda m: "" if _HTML_URL.fullmatch(block_text(m.group(1))) else m.group(1), block)
    block = map_html_text(block, lambda text: _HTML_URL.sub("", text))
    text = block_text(block)
    link_label_only = block != original and len(text.split()) <= 3
    if not re.search(r"\w", text) or link_label_only or _is_short_cta(text):
        return None
    return block


def strip_cta_content_no_link(body_content: str) -> str:
    """
    Remove CTA/booking/link content when campaign CTA link is empty.
    Used as a final safety guard so empty CTA config never sends asks/links.

    IMPORTANT: Only removes SHORT lines (<= 20 words) matching CTA patterns.
    Long body-content paragraphs are NEVER removed even if they contain words
    like 'call', 'connect', or 'explore' embedded in natural prose.

    Plain text works per line (a line with a URL or a CTA placeholder goes). HTML (the rich-text editor
    saves one line without "\n") works per block: links and placeholders are removed from the block and
    its text is kept; only a short CTA block (or one left empty) is removed.
    """
    if not body_content:
        return ""

    text = str(body_content).replace("\r\n", "\n").replace("\r", "\n")
    text = map_html_blocks(text, _strip_cta_block) if is_html_body(text) else _strip_cta_lines(text)
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
    out: list = []

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



def extract_latest_message_text(body_content: str, strip_boilerplate: bool = True) -> str:
    """
    Extract newest human-written portion of an email body.
    Removes quoted history, tracking/link noise, and (unless strip_boilerplate=False)
    unsubscribe/footer boilerplate. Detecting a prospect's own "unsubscribe" reply needs
    strip_boilerplate=False, otherwise the one word they wrote is removed as footer text.
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
    for pattern in (cleanup_patterns if strip_boilerplate else ()):
        text = re.sub(pattern, "", text)

    # Remove URL-only lines and collapse blank lines.
    text = re.sub(r"(?im)^\s*https?://\S+\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def html_to_text(body: str) -> str:
    """Plain-text view of an (possibly HTML) email body: tags removed, entities
    decoded, paragraph / line breaks kept as newlines."""
    if not body:
        return ""
    text = str(body)
    if not re.search(r"<[a-zA-Z/!][^>]*>|&[#a-zA-Z0-9]+;", text):
        return text
    text = re.sub(r"<(style|script|head|title)[^>]*>.*?</\1\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|tr|li|h[1-6]|blockquote)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ")


def conversation_snippet(body: Optional[str], limit: int = 100) -> str:
    """One-line plain-text preview of a message for the unified inbox list."""
    text = extract_latest_message_text(html_to_text(body or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "No messages"
    return text[:limit] + ("..." if len(text) > limit else "")


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
    bullet_buffer: list = []

    def _flush_bullets() -> None:
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
    original = ensure_core_personalization_tokens(body)
    body = normalize_unsubscribe_footer(original)
    if not has_visible_text(body) and has_visible_text(original):
        # Never empty a body with its own footer cleanup (e.g. a body that is only an unsubscribe sentence)
        logger.warning("Email body has no visible text after the unsubscribe cleanup; keeping it as written")
        body = original
    if not re.search(r"<[a-z][\s\S]*>", body or "", re.IGNORECASE):
        body = normalize_paragraph_spacing(body)
    body = format_email_body_html(body)
    return body


# ── Send-time footer (postal line, unsubscribe line, sign-off) ─────────────
UNSUBSCRIBE_REPLY_LINE = ("Please reply 'unsubscribe' in case you do not wish to "
                          "receive such business communication emails in the future.")


def unsubscribe_link_line(unsubscribe_url: str) -> str:
    """The unsubscribe sentence with a link (inline HTML)."""
    return (f'Please click on <a href="{unsubscribe_url}">unsubscribe</a> '
            f'in case you do not wish to receive such business communication emails in the future.')


def append_footer(body: str, line: str, *, html_body: bool, gap: str = "\n\n", markup: bool = False) -> str:
    """``body`` with ``line`` after it: after ``gap`` on a plain-text body; on an HTML body as its own ``<p>``
    block (newlines as ``<br>``, the text escaped unless it is already ``markup``), since HTML ignores the
    newlines and the line would run into the last paragraph."""
    if not html_body:
        return body + gap + line
    inner = line if markup else html.escape(line, quote=False)
    return body.rstrip() + "<p>" + inner.replace("\n", "<br>") + "</p>"


def build_signature_block(sender_name: Optional[str], sender_title: Optional[str] = None,
                          company_name: Optional[str] = None) -> str:
    """
    Build the resolved signature block: name, then title (if set), then the
    sending tenant's company name (if known) — used to resolve the
    {{signature_block}} token at send time.
    """
    from app.core.config import settings

    lines = [sender_name or settings.SENDER_NAME]
    if sender_title and sender_title.strip():
        lines.append(sender_title.strip())
    if company_name and company_name.strip():
        lines.append(company_name.strip())
    return "\n".join(lines)
