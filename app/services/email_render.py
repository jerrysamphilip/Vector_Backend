"""
Rendering a campaign step email for one contact, shared by pre-scheduling (the snapshot stored on
each EmailMessage at launch / after an edit) and the preview (POST /campaigns/{id}/email-previews).

Pure: no I/O. Two stages, exactly as an email is built today:

1. ``render_step_email``: the template's {{tokens}} filled in for the contact, CTA text removed when
   the campaign has no link, the body finalized (app/utils/email_utils.finalize_email_body). This is
   the snapshot app/services/campaign_email_service.py writes to email_messages.subject/body_text.
2. ``finalize_for_send``: what app/services/email_sender_service.py ``send_email`` adds at send time to
   that snapshot: the sender's postal address line and the unsubscribe line (not for same-domain
   mail), the {{unsubscribe_link}} URL and one sign-off. The sender keeps its own copy of these lines
   for now; ``finalize_for_send`` mirrors it line for line so a preview shows what goes out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from app.utils.email_utils import (UNSUBSCRIBE_REPLY_LINE, append_footer, build_signature_block,
                                   finalize_email_body, format_email_body_html, has_effective_cta_link,
                                   has_visible_text, is_html_body, normalize_cta_link, strip_cta_content_no_link,
                                   unsubscribe_link_line)

logger = logging.getLogger(__name__)

SIGN_OFF_PATTERN = r"(?i)(Best(\s+Regards)?|Regards|Thanks|Cheers|Sincerely)[,]?\s*\n\s*[A-Za-z0-9 ]+"
UNSUBSCRIBE_PATH = "/api/tracking/unsubscribe/"
_HTML_TAG = re.compile(r"<[a-z][\s\S]*>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RenderContext:
    """The campaign values baked into a step email."""

    sender_name: Optional[str] = None
    sender_title: Optional[str] = None
    cta_link: Optional[str] = None
    our_company: Optional[str] = None
    default_sender_name: str = ""


@dataclass(frozen=True, slots=True)
class SendContext:
    """What the sender knows at send time (see EmailSenderService.send_email)."""

    message_id: str
    sender_email: str
    base_url: str
    sender_name: Optional[str] = None
    sender_title: Optional[str] = None
    unsubscribe_mode: str = "plain"
    postal_address: Optional[str] = None
    footer_line: Optional[str] = None
    company_name: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FinalEmail:
    subject: str
    body: str
    is_html: bool
    is_internal: bool
    from_header: str


def _signature(template: str, ctx: RenderContext) -> str:
    """The {{signature_block}} value; in an HTML body its lines are joined with <br> (a "\\n" is no line break)."""
    signature = build_signature_block(ctx.sender_name, ctx.sender_title, ctx.our_company)
    return signature.replace("\n", "<br>") if is_html_body(template) else signature


def substitute_placeholders(template: Optional[str], prospect: Any, ctx: RenderContext) -> str:
    """Replace {{tokens}} with the contact's data and the campaign's sender values (any case)."""
    if not template:
        return template or ""
    cta_link = normalize_cta_link(ctx.cta_link)
    substitutions = {
        "{{first_name}}": prospect.first_name or "there",
        "{{last_name}}": prospect.last_name or "",
        "{{full_name}}": f"{prospect.first_name or ''} {prospect.last_name or ''}".strip() or "there",
        "{{company_name}}": prospect.company_name or "your company",
        "{{company}}": prospect.company_name or "your company",
        "{{designation}}": prospect.designation or "Professional",
        "{{title}}": prospect.designation or "Professional",
        "{{email}}": prospect.email or "",
        "{{industry}}": prospect.industry or "",
        "{{linkedin_url}}": prospect.linkedin_url or "",
        "{{city}}": prospect.poc_city or "",
        "{{state}}": prospect.poc_state or "",
        "{{your_name}}": ctx.sender_name or ctx.default_sender_name,
        "{{signature_block}}": _signature(template, ctx),
        "{{our_company}}": ctx.our_company or "",
        "{{calendar_link}}": cta_link,
        "{{cta_link}}": cta_link,
    }
    result = template
    for placeholder, value in substitutions.items():
        result = result.replace(placeholder, str(value))
        result = result.replace(placeholder.lower(), str(value))
        result = result.replace(placeholder.upper(), str(value))
    return result


def render_body(body_template: Optional[str], prospect: Any, ctx: RenderContext) -> str:
    """
    The personalised body: tokens filled in, CTA content removed when the campaign has no link, finalized.
    Safety net: when that cleanup leaves no visible text of a template that had some, the unstripped body is
    kept (and a warning logged): an email is never emptied by its own cleanup rules.
    """
    body = substitute_placeholders(body_template or "", prospect, ctx)
    rendered = finalize_email_body(body if has_effective_cta_link(ctx.cta_link) else strip_cta_content_no_link(body))
    if has_visible_text(rendered) or not has_visible_text(body):
        return rendered
    logger.warning("Rendered email body has no visible text after the CTA / footer cleanup; keeping the "
                   "unstripped body (template starts %r)", (body_template or "")[:80])
    return format_email_body_html(body.strip())


def render_step_email(subject_template: Optional[str], body_template: Optional[str], prospect: Any,
                      ctx: RenderContext) -> tuple[str, str]:
    """(subject, body) snapshot of one step email for one contact."""
    return substitute_placeholders(subject_template or "", prospect, ctx), render_body(body_template, prospect, ctx)


# ── send time ────────────────────────────────────────────────────────────────────────────────────

def html_to_plain_text(html: str) -> str:
    """Plain-text view of an HTML body (same rules as EmailSenderService._html_to_plain_text)."""
    if not html:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'\2 (\1)', text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|h[1-6]|li)>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '    • ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    for entity, char in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"),
                         ('&nbsp;', ' ')):
        text = text.replace(entity, char)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def has_sign_off(body: str) -> bool:
    return bool(re.search(SIGN_OFF_PATTERN, html_to_plain_text(body or "")))


def clean_subject(subject: Optional[str]) -> str:
    """The Subject header: tags removed, one line, single spaces."""
    clean = re.sub(r"<[^>]+>", "", subject or "")
    clean = clean.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", clean).strip()


def _domain(address: str) -> str:
    return address.split("@")[1].lower() if "@" in address else ""


def finalize_for_send(subject: str, body: str, recipient: str, ctx: SendContext) -> FinalEmail:
    """The stored snapshot plus what the sender appends at send time (postal line, unsubscribe line, sign-off)."""
    sender_domain = _domain(ctx.sender_email)
    is_internal = sender_domain != "" and sender_domain == _domain(recipient or "")
    unsubscribe_url = f"{ctx.base_url}{UNSUBSCRIBE_PATH}{ctx.message_id}"
    content = body or ""
    html_body = is_html_body(content)  # footer lines become <p> blocks on an HTML body (append_footer)
    if not is_internal:
        if ctx.footer_line and (ctx.postal_address or "") not in content and ctx.footer_line not in content:
            content = append_footer(content, ctx.footer_line, html_body=html_body)
        if UNSUBSCRIBE_PATH not in content:
            if (ctx.unsubscribe_mode or "plain") == "html":
                content = append_footer(content, unsubscribe_link_line(unsubscribe_url), html_body=html_body,
                                        gap="\n", markup=True)
            else:
                content = append_footer(content, UNSUBSCRIBE_REPLY_LINE, html_body=html_body, gap="\n")
    content = content.replace("{{unsubscribe_link}}", unsubscribe_url)
    if not has_sign_off(content):
        signature = build_signature_block(ctx.sender_name, ctx.sender_title, ctx.company_name)
        content = append_footer(content.rstrip(), f"Regards,\n{signature}", html_body=html_body)
    return FinalEmail(subject=clean_subject(subject), body=content, is_html=bool(_HTML_TAG.search(content)),
                      is_internal=is_internal, from_header=f"{ctx.sender_name or ''} <{ctx.sender_email}>".strip())
