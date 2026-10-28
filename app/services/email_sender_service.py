# app/services/email_sender_service.py
"""
AWS SES Email Sender Service
Production-ready with rate limiting, retries, and error handling.
"""

import asyncio
import logging
import re
import time
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import email.utils

from http.client import HTTPException

from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.adapters import providers
from app.adapters.ses import SES_CLIENT_CONFIG as SES_CLIENT_CONFIG  # re-exported for callers
from app.adapters.ses import EmailTransport
from app.core.config import settings
from app.models import EmailMessage, EmailTemplate, Prospect
from app.repositories.sending import session_or_none
from app.services import compliance_service
from app.services.sender_identity import get_sender_identity
from app.services.send_errors import PERMANENT_CODES, SENDER_CODES, FailureKind, classify_ses_error
from app.utils.email_utils import (
    normalize_unsubscribe_footer,
    strip_cta_content_no_link,
    normalize_cta_link,
    has_effective_cta_link,
    normalize_paragraph_spacing,
    build_signature_block,
)

logger = logging.getLogger(__name__)


# -----------------------------
# Custom Exceptions
# -----------------------------

class TransientEmailFailure(Exception):
    """Retryable SES error (throttling, temporary issues)"""
    pass


class DeferredEmailFailure(TransientEmailFailure):
    """
    The message is fine but cannot go out right now for a reason outside it (item 14): the
    sending platform (``kind`` PLATFORM: SES account paused, throttled, unavailable, configuration
    set missing, no connection before the request left) or the sending identity (``kind`` SENDER:
    address / MAIL FROM domain not verified). Callers re-queue it without spending the message's
    retry budget; send_with_retry never retries it in place.
    """

    kind = FailureKind.PLATFORM

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class PlatformEmailFailure(DeferredEmailFailure):
    kind = FailureKind.PLATFORM


class SenderEmailFailure(DeferredEmailFailure):
    kind = FailureKind.SENDER


class PermanentEmailFailure(Exception):
    """
    Non-retryable SES error (rejected, invalid email). `recipient_fault` is True
    when the recipient address itself was refused (invalid / blacklisted), which
    is when the address should be suppressed; other permanent errors say nothing
    about the contact.
    """

    def __init__(self, message: str, code: Optional[str] = None, recipient_fault: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.recipient_fault = recipient_fault


# SES error codes that will fail the same way on every retry (see app/services/send_errors.py
# for which of them are the platform's or the sender's fault rather than the message's)
PERMANENT_SES_ERRORS = PERMANENT_CODES | SENDER_CODES


def classify_client_error(error_code: str, error_message: str) -> Exception:
    """Map an SES ClientError to a deferred, permanent or transient failure (B06, item 14)."""
    text = f"{error_code}: {error_message}"
    kind = classify_ses_error(error_code, error_message)
    if kind is FailureKind.PLATFORM:
        return PlatformEmailFailure(text, code=error_code)
    if kind is FailureKind.SENDER:
        return SenderEmailFailure(text, code=error_code)
    if kind in (FailureKind.RECIPIENT, FailureKind.PERMANENT):
        return PermanentEmailFailure(text, code=error_code, recipient_fault=kind is FailureKind.RECIPIENT)
    return TransientEmailFailure(text)


class AmbiguousEmailFailure(Exception):
    """
    The request may have reached SES before the connection failed (read timeout,
    connection dropped mid-response), so SES may have sent the email. Retrying
    could deliver it twice (BR-DF-06); callers must not retry.
    """
    pass


def classify_botocore_error(exc: Exception) -> Exception:
    # It was sent but the answer was lost (read timeout, connection dropped, truncated
    # response): SES may have sent it, so the outcome is unknown (F079). Checked first: when
    # in doubt, never send again.
    if isinstance(exc, (HTTPClientError, HTTPException, Urllib3HTTPError)):
        return AmbiguousEmailFailure(str(exc) or type(exc).__name__)
    # The request never left (no connection, TLS or proxy failure): safe to send later
    if isinstance(exc, BotoConnectionError):
        return PlatformEmailFailure(str(exc), code=type(exc).__name__)
    return TransientEmailFailure(str(exc))


# One attempt per send: the SES adapter (app/adapters/ses.py) builds its client with
# total_max_attempts=1, so botocore never resends a message SES may already have accepted (F079).
# Throttling / 5xx surface to the scheduler, which re-queues instead.


# -----------------------------
# Content helpers (pure)
# -----------------------------

def placeholder_values(prospect: Any, sender_name: Optional[str], cta_link: Optional[str],
                       sender_title: Optional[str], our_company: Optional[str]) -> Dict[str, str]:
    """The value of every template placeholder for this contact and sender."""
    first, last = prospect.first_name or "", prospect.last_name or ""
    company = prospect.company_name or "your company"
    designation = prospect.designation or "Professional"
    return {
        "{{first_name}}": first or "there",
        "{{last_name}}": last,
        "{{full_name}}": f"{first} {last}".strip() or "there",
        "{{company_name}}": company,
        "{{company}}": company,
        "{{designation}}": designation,
        "{{title}}": designation,
        "{{email}}": prospect.email or "",
        "{{industry}}": prospect.industry or "",
        "{{linkedin_url}}": prospect.linkedin_url or "",
        "{{city}}": prospect.poc_city or "",
        "{{state}}": prospect.poc_state or "",
        "{{your_name}}": sender_name or settings.SENDER_NAME,
        "{{signature_block}}": build_signature_block(sender_name, sender_title, our_company),
        "{{our_company}}": our_company or "",
        "{{calendar_link}}": cta_link or "",
        "{{cta_link}}": cta_link or "",
    }


def tidy_substitution(text: str) -> str:
    r"""
    Clean up broken patterns left by empty substitutions (e.g. "providers in , " -> "providers in ").
    Uses [ \t]* (not \s*) so newlines are NEVER consumed: consuming \n\n would collapse paragraphs.
    """
    text = re.sub(r'\bin[ \t]*,[ \t]*,?[ \t]*', 'in ', text)  # "in , , " -> "in "
    text = re.sub(r',[ \t]*,', ',', text)                        # ", ," -> ","
    text = re.sub(r'[ \t]{2,}', ' ', text)                       # collapse spaces/tabs only (NOT newlines)
    text = re.sub(r'[ \t]+,', ',', text)                         # " ," -> ","
    text = re.sub(r',[ \t]*\.', '.', text)                       # ", ." -> "."
    return text


def clean_subject(subject: Optional[str]) -> str:
    """A subject line without markup or line breaks."""
    subject = re.sub(r'<[^>]+>', '', subject or '')
    subject = subject.replace('\r', ' ').replace('\n', ' ')
    return re.sub(r'\s{2,}', ' ', subject).strip()


def _domain(address: str) -> str:
    return address.split('@')[1].lower() if '@' in address else ''


def is_internal_mail(sender_email: str, recipient_email: str) -> bool:
    """Sender and recipient share a domain: an internal test or collaboration email."""
    sender_domain = _domain(sender_email)
    return sender_domain != '' and sender_domain == _domain(recipient_email)


def with_unsubscribe_line(body_content: str, unsubscribe_url: str, unsubscribe_mode: Optional[str]) -> str:
    """Append the unsubscribe sentence (an HTML link, or 'reply unsubscribe') unless a link is already there."""
    if "/api/tracking/unsubscribe/" in body_content:
        return body_content
    if (unsubscribe_mode or 'plain') == 'html':
        return body_content + (
            f'\nPlease click on <a href="{unsubscribe_url}">unsubscribe</a> '
            f'in case you do not wish to receive such business communication emails in the future.'
        )
    return body_content + (
        "\nPlease reply 'unsubscribe' in case you do not wish to "
        "receive such business communication emails in the future."
    )


# -----------------------------
# Rate Limiter (Concurrency-safe)
# -----------------------------

class RateLimiter:
    """Semaphore-based rate limiter for SES send rate limits."""
    
    def __init__(self, rate_per_second: int) -> None:
        self._semaphore = asyncio.Semaphore(rate_per_second)
        self._rate = rate_per_second
    
    async def acquire(self) -> None:
        """Acquire a slot, auto-releases after 1 second."""
        await self._semaphore.acquire()
        # Schedule release after 1 second
        asyncio.get_running_loop().call_later(
            1.0, self._semaphore.release
        )


# -----------------------------
# Warmup Schedule (per-inbox daily limits by warmup day)
# -----------------------------

# WARMUP_SCHEDULE moved to app.models.sending_inbox.SendingInbox


# -----------------------------
# Email Sender Service
# -----------------------------

class EmailSenderService:
    """Service for sending emails via AWS SES (through the EmailTransport adapter)"""
    
    def __init__(self, transport: Optional[EmailTransport] = None) -> None:
        # None: the provider's transport (SES with IAM roles, or env credentials in local dev),
        # looked up per send so providers.override(email_transport=...) reaches the singleton
        self._transport = transport
        self.rate_limiter = RateLimiter(settings.SES_MAX_SEND_RATE)
        self._last_sent_ts = 0.0

    @property
    def transport(self) -> EmailTransport:
        return self._transport if self._transport is not None else providers.email_transport()

    def get_inbox_daily_limit(self, inbox) -> int:
        """
        Get the effective daily send limit for an inbox.
        Delegates to the SendingInbox model property which handles
        warmup schedule, user override, and daily_limit fallback.
        """
        return inbox.current_daily_limit

    def _substitute_placeholders(
        self,
        template: str,
        prospect: Prospect,
        sender_name: Optional[str] = None,
        cta_link: Optional[str] = None,
        sender_title: Optional[str] = None
    ) -> str:
        """Replace placeholders in template with prospect data."""
        if not template:
            return template
        # The sending tenant's own company name (never another tenant's)
        our_company = get_sender_identity(getattr(prospect, "tenant_id", None)).company_name
        result = template
        for placeholder, value in placeholder_values(prospect, sender_name, cta_link, sender_title,
                                                     our_company).items():
            # Case-insensitive replacement
            result = result.replace(placeholder, str(value))
            result = result.replace(placeholder.lower(), str(value))
            result = result.replace(placeholder.upper(), str(value))
        return tidy_substitution(result)

    def _add_tracking_pixel(self, html_body: str, message_id: str) -> str:
        """Add invisible 1x1 pixel for open tracking."""
        tracking_url = f"{settings.BASE_URL}/api/tracking/open/{message_id}.png"
        tracking_pixel = f'<img src="{tracking_url}" width="1" height="1" alt="" style="display:none" />'
        
        # Insert before closing body tag if exists
        if '</body>' in html_body.lower():
            html_body = html_body.replace('</body>', f'{tracking_pixel}</body>')
            html_body = html_body.replace('</BODY>', f'{tracking_pixel}</BODY>')
        else:
            html_body += tracking_pixel
        
        return html_body
    
    def _wrap_links_for_tracking(self, html_body: str, message_id: str) -> str:
        """Wrap links with tracking redirect URLs."""
        from urllib.parse import quote_plus
        
        link_pattern = r'href=["\']([^"\']+)["\']'
        
        def replace_link(match) -> str:
            original_url = match.group(1)
            # Skip mailto, tel, and already tracked URLs
            if (original_url.startswith('mailto:') or 
                original_url.startswith('tel:') or 
                'tracking/click' in original_url or
                original_url.startswith('#')):
                return match.group(0)
            
            from app.core.security import sign_tracking_url
            tracked_url = (
                f"{settings.BASE_URL}/api/tracking/click/{message_id}"
                f"?url={quote_plus(original_url)}"
                f"&sig={sign_tracking_url(message_id, original_url)}"
            )
            return f'href="{tracked_url}"'
        
        return re.sub(link_pattern, replace_link, html_body)

    SIGN_OFF_PATTERN = r"(?i)(Best(\s+Regards)?|Regards|Thanks|Cheers|Sincerely)[,]?\s*\n\s*[A-Za-z0-9 ]+"

    def has_sign_off(self, body: str) -> bool:
        """Whether the body already ends with a sign-off ("Regards,\n<name>"). No '$' anchor:
        the postal address and unsubscribe footer are appended after the body. Bodies finalized
        at launch are HTML (<p>/<br>), so the check runs on a plain-text view; matching the raw
        HTML missed them and every campaign email got a second sign-off."""
        return bool(re.search(self.SIGN_OFF_PATTERN, self._html_to_plain_text(body or "")))

    def _html_to_plain_text(self, html: str) -> str:
        """Convert HTML to plain text for multipart/alternative emails."""
        if not html:
            return ""

        text = html
        # Remove style and script blocks
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)

        # Convert links: <a href="URL">TEXT</a> -> TEXT (URL)
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            r'\2 (\1)', text, flags=re.DOTALL | re.IGNORECASE
        )

        # Block-level elements -> newlines
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</(?:p|div|tr|h[1-6]|li)>', '\n\n', text, flags=re.IGNORECASE)

        # Bullet points
        text = re.sub(r'<li[^>]*>', '    • ', text, flags=re.IGNORECASE)

        # Strip remaining tags
        text = re.sub(r'<[^>]+>', '', text)

        # Decode common HTML entities
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')
        text = text.replace('&#39;', "'")
        text = text.replace('&nbsp;', ' ')

        # Collapse excessive blank lines
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _plain_text_to_html(self, text: str) -> str:
        """Convert a plain-text email body to minimal HTML.

        Preserves <a> tags (e.g. unsubscribe link) that were injected at
        send time while escaping everything else.
        """
        if not text:
            return ""

        # 1. Pull out <a ...>...</a> tags so they survive escaping.
        placeholders: Dict[str, str] = {}
        def _stash(m) -> str:
            key = f"__LINK_{len(placeholders)}__"
            placeholders[key] = m.group(0)
            return key
        safe = re.sub(r'<a\s[^>]*>.*?</a>', _stash, text, flags=re.DOTALL | re.IGNORECASE)

        # 2. Escape HTML entities.
        safe = safe.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # 3. Restore <a> tags.
        for key, tag in placeholders.items():
            safe = safe.replace(key, tag)

        # 4. Convert newlines to <br>.
        safe = safe.replace('\n', '<br>\n')

        return safe

    def _wrap_html_body(self, html_body: str) -> str:
        """Wrap HTML fragment in proper document structure if needed."""
        stripped = html_body.strip().lower()
        if stripped.startswith('<!doctype') or stripped.startswith('<html'):
            return html_body

        return (
            '<!DOCTYPE html>\n'
            '<html lang="en">\n'
            '<head>\n'
            '  <meta charset="utf-8">\n'
            '</head>\n'
            f'<body>\n{html_body}\n</body>\n'
            '</html>'
        )

    def _load_template_attachments(self, email_template: Optional[EmailTemplate]) -> list:
        """Read attachment files from disk for a template, ready to embed in the MIME message."""
        if not email_template:
            return []

        store = providers.attachment_store()
        loaded = []
        for attachment in getattr(email_template, "attachments", None) or []:
            if not store.exists(attachment.storage_path):
                logger.warning(
                    f"[SES] Attachment file missing on disk, skipping. "
                    f"AttachmentId={attachment.attachment_id}, Path={attachment.storage_path}"
                )
                continue
            try:
                loaded.append({
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "data": store.get(attachment.storage_path),
                })
            except OSError as exc:
                logger.warning(f"[SES] Failed to read attachment {attachment.attachment_id}: {exc}")
        return loaded

    def _build_mime_message(
        self,
        from_email: str,
        to_email: str,
        subject: str,
        body_content: str,
        message_id: str,
        campaign_id: str,
        sender_email: str,
        unsubscribe_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        attachments: Optional[list] = None,
        unsubscribe_mailto: Optional[str] = None,
    ) -> MIMEMultipart:
        """
        Build a clean MIME message optimized for inbox placement.

        Uses multipart/alternative with both plain text and HTML, wrapped in
        multipart/mixed with file parts when `attachments` is provided.
        Avoids marketing headers that increase spam probability.

        `attachments` is a list of dicts: {"filename": str, "content_type": str, "data": bytes}
        """

        alt = MIMEMultipart("alternative")

        # Single source of truth for "is this already HTML (even a bare fragment
        # like TipTap's <p>...</p>, with no <html>/<body> wrapper)?" — both the
        # plain-text and HTML branches below must agree on this, otherwise HTML
        # fragments get mistaken for plain text and their tags are escaped
        # literally into the sent email.
        is_html = bool(re.search(r"<[a-z][\s\S]*>", body_content or "", re.IGNORECASE))

        # --------------------------
        # Plain Text Version
        # --------------------------
        if is_html:
            plain_text_body = self._html_to_plain_text(body_content)
        else:
            plain_text_body = body_content or ""

        part_plain = MIMEText(plain_text_body, "plain", "utf-8")

        # --------------------------
        # HTML Version
        # --------------------------
        if not is_html:
            html_body = f"""
    <html>
    <body>
    {self._plain_text_to_html(body_content)}
    </body>
    </html>
    """
        elif not re.search(r"</?(html|body)[^>]*>", body_content or "", re.IGNORECASE):
            # HTML fragment without a document wrapper (e.g. TipTap output) — wrap as-is, don't re-escape it.
            html_body = f"""
    <html>
    <body>
    {body_content}
    </body>
    </html>
    """
        else:
            html_body = body_content or ""

        part_html = MIMEText(html_body, "html", "utf-8")

        # Attach parts (plain first, html second)
        alt.attach(part_plain)
        alt.attach(part_html)

        if attachments:
            from email.mime.base import MIMEBase
            from email import encoders

            msg = MIMEMultipart("mixed")
            msg.attach(alt)

            for att in attachments:
                maintype, _, subtype = (att.get("content_type") or "application/octet-stream").partition("/")
                part = MIMEBase(maintype or "application", subtype or "octet-stream")
                part.set_payload(att["data"])
                encoders.encode_base64(part)
                # filename as a parameter: the email package quotes it and encodes a
                # non-ASCII name per RFC 2231 instead of emitting a raw, broken header (W-12)
                part.add_header("Content-Disposition", "attachment", filename=att["filename"])
                msg.attach(part)
        else:
            msg = alt

        # Clean subject
        clean_subject = re.sub(r"<[^>]+>", "", subject or "")
        clean_subject = clean_subject.replace("\r", " ").replace("\n", " ")
        clean_subject = re.sub(r"\s{2,}", " ", clean_subject).strip()

        # Standard headers
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = clean_subject
        msg["Date"] = email.utils.formatdate(localtime=True)

        # Generate Message-ID
        domain = sender_email.split("@")[1] if "@" in sender_email else "localdomain"
        msg["Message-ID"] = email.utils.make_msgid(domain=domain)

        # Reply-To (safer if same as From)
        msg["Reply-To"] = from_email

        # RFC 2369 / RFC 8058 one-click unsubscribe (Gmail/Yahoo bulk-sender requirement).
        # Only campaign sends pass unsubscribe_url; warmup/internal mail gets no list headers.
        if unsubscribe_url:
            targets = [f"<{unsubscribe_url}>"]
            if unsubscribe_mailto:
                targets.append(f"<mailto:{unsubscribe_mailto}?subject=unsubscribe&body=unsubscribe>")
            msg["List-Unsubscribe"] = ", ".join(targets)
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        # Optional extra headers (only if explicitly passed)
        if extra_headers:
            for header_key, header_value in extra_headers.items():
                if header_value:
                    msg[header_key] = header_value

        return msg

    def _normalize_unsubscribe_text(self, body_content: str, message_id: str) -> str:
        """
        Normalize unsubscribe content so all emails render consistently in-body.
        We keep one plain-text instruction and rely on List-Unsubscribe headers
        for native client unsubscribe actions.
        """
        return normalize_unsubscribe_footer(body_content)

    async def send_email(
        self,
        email_message: EmailMessage,
        email_template: EmailTemplate,
        prospect: Prospect,
        enable_tracking: bool = True,
        sender_name: Optional[str] = None,
        from_email_address: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send email via AWS SES with placeholder substitution.

        Steps: rate limit; the subject / body snapshot (_message_content); refuse personal addresses and
        spam-lint failures before SES (_refusal); the compliance footer, unsubscribe line and sign-off
        (_compliance_body); the MIME message and the SES request (_ses_request); one send (_send_raw).

        Returns a dict with success, message_id, the SES message id and the MIME Message-ID; a refusal
        returns success False with a BLOCKED_* status.

        Raises:
            TransientEmailFailure: For retryable errors
            PermanentEmailFailure: For non-retryable errors
        """
        await self.rate_limiter.acquire()
        campaign = email_message.campaign
        cta_link = normalize_cta_link(campaign.cta_link) if campaign else None
        sender_title = campaign.sender_title if campaign else None
        subject, body_content = self._message_content(email_message, email_template, prospect, sender_name,
                                                      cta_link, sender_title)
        refusal = self._refusal(email_message, prospect, subject, body_content, from_email_address)
        if refusal:
            return refusal

        sender_email = from_email_address or settings.SENDER_EMAIL
        from_email = f"{sender_name or settings.SENDER_NAME} <{sender_email}>"
        # Same-domain (internal) mail: no unsubscribe link, postal address or list headers, as they
        # trigger impersonation / spam filters.
        is_internal = is_internal_mail(sender_email, prospect.email)
        unsubscribe_url = f"{settings.BASE_URL}/api/tracking/unsubscribe/{email_message.message_id}"
        identity = get_sender_identity(prospect.tenant_id, session_or_none(email_message))
        body_content = self._compliance_body(body_content, email_message, prospect, identity, is_internal,
                                             unsubscribe_url, sender_name, sender_title)
        # SES adds the open pixel / link tracking itself (configuration set events -> /webhooks/ses).
        mime_msg = self._build_mime_message(
            from_email=from_email,
            to_email=prospect.email,
            subject=subject,
            body_content=body_content,
            message_id=str(email_message.message_id),
            campaign_id=str(email_message.campaign_id),
            sender_email=sender_email,
            unsubscribe_url=None if is_internal else unsubscribe_url,
            attachments=self._load_template_attachments(email_template),
            unsubscribe_mailto=sender_email if "@" in (sender_email or "") else None,
        )
        response = await self._send_raw(self._ses_request(from_email, prospect.email, mime_msg, email_message),
                                        email_message, prospect.email)
        ses_message_id = response["MessageId"]
        logger.info(
            f"[SES] Email sent successfully. "
            f"SES MessageId: {ses_message_id}, "
            f"Recipient: {prospect.email}, "
            f"Campaign: {email_message.campaign_id}"
        )
        # The MIME Message-ID is what mail clients put in In-Reply-To / References: IMAP sync matches
        # replies to this thread on it.
        return {
            "success": True,
            "ses_message_id": ses_message_id,
            "internet_message_id": mime_msg["Message-ID"],
            "message_id": email_message.message_id,
            "status": "SENT"
        }

    def _message_content(self, email_message: EmailMessage, email_template: EmailTemplate, prospect: Prospect,
                         sender_name: Optional[str], cta_link: Optional[str],
                         sender_title: Optional[str]) -> Tuple[str, str]:
        """
        The subject and body to send. The snapshot personalised at launch (_preschedule_all_emails) wins, so
        stale data or the raw template never overwrite it; without one the template is substituted now and
        the snapshot is kept aligned with what is sent.
        """
        if email_message.subject and email_message.subject.strip():
            subject = email_message.subject
        else:
            subject = clean_subject(self._substitute_placeholders(
                email_template.subject, prospect, sender_name, cta_link, sender_title))
            email_message.subject = subject
        if email_message.body_text and email_message.body_text.strip():
            return subject, email_message.body_text
        body_content = self._substitute_placeholders(email_template.body, prospect, sender_name, cta_link,
                                                     sender_title)
        if not has_effective_cta_link(cta_link):
            body_content = strip_cta_content_no_link(body_content)
        # Normalize unsubscribe content across creative/normal modes.
        body_content = self._normalize_unsubscribe_text(body_content=body_content,
                                                        message_id=str(email_message.message_id))
        email_message.body_text = body_content
        return subject, body_content

    @staticmethod
    def _refusal(email_message: EmailMessage, prospect: Prospect, subject: str, body_content: str,
                 from_email_address: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Refuse before SES: cold outreach to a personal mailbox provider (gmail, yahoo, ...: they flag B2B
        outreach as spam and hurt the sender's reputation), or content over the pre-send lint threshold
        (spam keywords, subject rules, Microsoft EOP ASF triggers, AI-style wording, link domains).
        """
        if not compliance_service.is_business_email(prospect.email):
            logger.warning(
                f"[SES] Email BLOCKED — personal email domain. "
                f"Recipient={prospect.email}, MessageId={email_message.message_id}"
            )
            return {
                "success": False,
                "message_id": email_message.message_id,
                "status": "BLOCKED_PERSONAL_EMAIL",
                "error": f"Recipient {prospect.email} is a personal email domain. Cold outreach to personal emails damages sender reputation."
            }
        if settings.SPAM_SCORE_BLOCK_THRESHOLD <= 0:
            return None
        sender = from_email_address or settings.SENDER_EMAIL
        lint = compliance_service.lint_email_content(subject, body_content,
                                                     sender.split("@")[-1] if "@" in sender else "")
        if lint.warnings:  # logged for diagnosis even below the threshold
            logger.info(
                f"[SES] Lint warnings for MessageId={email_message.message_id}: "
                f"Score={lint.composite_score}, "
                f"Warnings={[w.message for w in lint.warnings[:5]]}"
            )
        if lint.composite_score < settings.SPAM_SCORE_BLOCK_THRESHOLD:
            return None
        logger.warning(
            f"[SES] Email BLOCKED by pre-send lint gate. "
            f"Score={lint.composite_score} (threshold={settings.SPAM_SCORE_BLOCK_THRESHOLD}), "
            f"Triggers={lint.trigger_names[:5]}, "
            f"MessageId={email_message.message_id}, "
            f"Recipient={prospect.email}"
        )
        return {
            "success": False,
            "message_id": email_message.message_id,
            "status": "BLOCKED_SPAM",
            "error": f"Lint score {lint.composite_score} exceeds threshold {settings.SPAM_SCORE_BLOCK_THRESHOLD}. Triggers: {lint.trigger_names[:5]}"
        }

    def _compliance_body(self, body_content: str, email_message: EmailMessage, prospect: Prospect, identity: Any,
                         is_internal: bool, unsubscribe_url: str, sender_name: Optional[str],
                         sender_title: Optional[str]) -> str:
        """The postal address and unsubscribe line (external mail only), the unsubscribe link placeholder
        and exactly one sign-off."""
        if not is_internal:
            body_content = self._with_postal_address(body_content, email_message, prospect, identity)
            body_content = with_unsubscribe_line(body_content, unsubscribe_url,
                                                 getattr(email_message.campaign, 'unsubscribe_mode', 'plain'))
        # Replace explicit unsubscribe placeholder if template uses it
        body_content = body_content.replace("{{unsubscribe_link}}", unsubscribe_url)
        if not self.has_sign_off(body_content):
            body_content = body_content.rstrip() + (f"\n\nRegards,\n"
                                                    f"{build_signature_block(sender_name, sender_title, identity.company_name)}")
        return body_content

    @staticmethod
    def _with_postal_address(body_content: str, email_message: EmailMessage, prospect: Prospect,
                             identity: Any) -> str:
        """CAN-SPAM §5(a)(5): the SENDER's physical postal address appears in every email: the tenant's own,
        never a platform default. New launches are blocked without one; mail already in flight goes out
        without the line rather than with another company's."""
        footer_line = identity.footer_line
        if not footer_line:
            logger.warning(
                f"[SES] Tenant {prospect.tenant_id} has no postal address set; footer address "
                f"omitted for MessageId={email_message.message_id}. Set it in Settings."
            )
            return body_content
        if identity.postal_address not in body_content and footer_line not in body_content:
            body_content += f"\n\n{footer_line}"
        return body_content

    @staticmethod
    def _ses_request(from_email: str, recipient: str, mime_msg: MIMEMultipart,
                     email_message: EmailMessage) -> Dict[str, Any]:
        """
        The raw-email request. The configuration set is REQUIRED for bounce / complaint / delivery / open /
        click notifications to fire at all (SES only emits them for sends associated with a configuration
        set that has an SNS destination). The message_id tag is how ses_webhook_router's _extract_tags()
        knows WHICH EmailMessage a notification is about (tag values allow [A-Za-z0-9_-]; a UUID fits).
        """
        send_kwargs: Dict[str, Any] = {
            "Source": from_email,
            "Destinations": [recipient],
            "RawMessage": {"Data": mime_msg.as_string()},
        }
        if settings.AWS_SES_CONFIGURATION_SET:
            send_kwargs["ConfigurationSetName"] = settings.AWS_SES_CONFIGURATION_SET
        send_kwargs["Tags"] = [{"Name": "message_id", "Value": str(email_message.message_id)}]
        return send_kwargs

    async def _send_raw(self, send_kwargs: Dict[str, Any], email_message: EmailMessage,
                        recipient: str) -> Dict[str, Any]:
        """
        One SES send_raw_email call (the client never retries it, F079), with every failure mapped
        to the failure classes the scheduler acts on: deferred (platform / sender), permanent,
        transient, or ambiguous when the request may already have been accepted.
        """
        try:
            transport = self.transport
            return await asyncio.to_thread(transport.send_raw, **send_kwargs)
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_message = exc.response["Error"]["Message"]
            logger.error(
                f"[SES] ClientError: {error_code} - {error_message}. "
                f"MessageId: {email_message.message_id}, "
                f"Recipient: {recipient}"
            )
            raise classify_client_error(error_code, error_message)
        except (BotoCoreError, HTTPException, Urllib3HTTPError) as exc:
            logger.error(f"[SES] {type(exc).__name__}: {exc}")
            raise classify_botocore_error(exc)

    async def send_plain_email(
        self,
        to_email: str,
        subject: str,
        body_content: str,
        from_email_address: Optional[str] = None,
        sender_name: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Send a direct plain-text email without campaign/prospect compliance wrapping.
        Used by the internal warmup engine.
        """
        await self.rate_limiter.acquire()

        final_sender_name = sender_name or settings.SENDER_NAME
        sender_email = from_email_address or settings.SENDER_EMAIL
        from_email = f"{final_sender_name} <{sender_email}>"
        synthetic_message_id = f"warmup-{int(time.time() * 1000)}"

        mime_msg = self._build_mime_message(
            from_email=from_email,
            to_email=to_email,
            subject=subject,
            body_content=body_content,
            message_id=synthetic_message_id,
            campaign_id="warmup",
            sender_email=sender_email,
            unsubscribe_url=None,
            extra_headers=extra_headers,
        )

        try:
            transport = self.transport
            response = await asyncio.to_thread(
                transport.send_raw,
                Source=from_email,
                Destinations=[to_email],
                RawMessage={"Data": mime_msg.as_string()},
            )
            return {
                "success": True,
                "ses_message_id": response["MessageId"],
                "internet_message_id": mime_msg.get("Message-ID"),
            }
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_message = exc.response["Error"]["Message"]
            logger.error(
                f"[SES] Warmup send failed: {error_code} - {error_message}. "
                f"Sender={sender_email}, Recipient={to_email}"
            )
            return {"success": False, "error": f"{error_code}: {error_message}"}
        except BotoCoreError as exc:
            logger.error(f"[SES] Warmup send failed: {exc}")
            return {"success": False, "error": str(exc)}
        
        except Exception as exc:
            logger.error(f"[SES] Unexpected error: {str(exc)}")
            raise TransientEmailFailure(str(exc))
    
    async def send_with_retry(  # type: ignore[return]  # max_retries < 1 falls through (returns None)
        self,
        email_message: EmailMessage,
        email_template: EmailTemplate,
        prospect: Prospect,
        max_retries: int = 3,
        sender_name: Optional[str] = None,
        from_email_address: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send email with exponential backoff retry logic.
        
        Args:
            email_message: EmailMessage to send
            email_template: Template to use
            prospect: Recipient prospect
            max_retries: Maximum retry attempts
            sender_name: Optional sender name
            from_email_address: Specific sender email logic
            
        Returns:
            Send result dict (success, or a pre-send block such as a personal
            address or the spam lint gate, with success=False)

        Raises:
            PermanentEmailFailure: SES refused the message for good; not retried.
            DeferredEmailFailure: platform / sender-identity problem; not retried here.
            AmbiguousEmailFailure: SES may have accepted it; never retried.
            TransientEmailFailure: still failing temporarily after max_retries
                quick attempts; the caller decides whether to try again later.
        """
        attempts = max(1, int(max_retries or 1))
        for attempt in range(attempts):
            try:
                return await self.send_email(
                    email_message,
                    email_template,
                    prospect,
                    True,
                    sender_name,
                    from_email_address
                )
            except DeferredEmailFailure:
                # Platform / sender problem: retrying in place cannot help; the caller re-queues
                raise
            except TransientEmailFailure as exc:
                if attempt + 1 >= attempts:
                    logger.warning(
                        f"[SES] Temporary failure persisted after {attempts} attempt(s) "
                        f"for message {email_message.message_id}: {exc}"
                    )
                    raise
                wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                logger.warning(
                    f"[SES] Retry {attempt + 1}/{attempts} for message {email_message.message_id}. "
                    f"Waiting {wait_time}s. Error: {exc}"
                )
                await asyncio.sleep(wait_time)
            except PermanentEmailFailure as exc:
                # Never retried, and surfaced to the caller so it can mark the
                # message FAILED with the reason (and suppress a bad address)
                logger.error(f"[SES] Permanent failure, not retrying: {exc}")
                raise

    def verify_email(self, email: str) -> bool:
        """
        Send verification email (for SES sandbox mode).
        
        Args:
            email: Email address to verify
            
        Returns:
            True if verification request sent successfully
        """
        try:
            self.transport.verify_email_identity(email)
            logger.info(f"[SES] Verification email sent to {email}")
            return True
        except ClientError as exc:
            logger.error(f"[SES] Failed to verify {email}: {exc}")
            return False
    
    def get_send_quota(self) -> Dict[str, Any]:
        """Get current SES sending quota and usage."""
        try:
            response = self.transport.get_send_quota()
            return {
                "max_24_hour_send": response["Max24HourSend"],
                "max_send_rate": response["MaxSendRate"],
                "sent_last_24_hours": response["SentLast24Hours"]
            }
        except ClientError as exc:
            logger.error(f"[SES] Failed to get quota: {exc}")
            return {}


# Singleton instance
email_sender = EmailSenderService()
