# app/services/email_sender_service.py
"""
AWS SES Email Sender Service
Production-ready with rate limiting, retries, and error handling.
"""

import asyncio
import logging
import re
import time
from typing import Dict, Any, Optional
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import email.utils

import boto3
from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectionClosedError, ConnectTimeoutError,
    EndpointConnectionError, ReadTimeoutError,
)

from app.core.config import settings
from app.models import EmailMessage, EmailTemplate, Prospect
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


class PermanentEmailFailure(Exception):
    """Non-retryable SES error (rejected, invalid email)"""
    pass


class AmbiguousEmailFailure(Exception):
    """
    The request may have reached SES before the connection failed (read timeout,
    connection dropped mid-response), so SES may have sent the email. Retrying
    could deliver it twice (BR-DF-06); callers must not retry.
    """
    pass


def classify_botocore_error(exc: BotoCoreError) -> Exception:
    # The request never left (no connection): safe to retry
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError)):
        return TransientEmailFailure(str(exc))
    # It was sent but no answer came back: outcome unknown
    if isinstance(exc, (ReadTimeoutError, ConnectionClosedError)):
        return AmbiguousEmailFailure(str(exc))
    return TransientEmailFailure(str(exc))


# -----------------------------
# Rate Limiter (Concurrency-safe)
# -----------------------------

class RateLimiter:
    """Semaphore-based rate limiter for SES send rate limits."""
    
    def __init__(self, rate_per_second: int):
        self._semaphore = asyncio.Semaphore(rate_per_second)
        self._rate = rate_per_second
    
    async def acquire(self):
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
    """Service for sending emails via AWS SES"""
    
    def __init__(self):
        # Initialize SES client
        # In production, use IAM roles. For local dev, use env credentials.
        client_kwargs = {
            "region_name": settings.AWS_REGION,
        }
        
        # Only pass credentials if explicitly set (for local dev)
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        
        self.ses_client = boto3.client("ses", **client_kwargs)
        self.rate_limiter = RateLimiter(settings.SES_MAX_SEND_RATE)
        self._last_sent_ts = 0.0

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
            "{{your_name}}": sender_name or settings.SENDER_NAME,
            "{{signature_block}}": build_signature_block(sender_name, sender_title),
            "{{our_company}}": "Neutrino Tech Systems",
            "{{calendar_link}}": cta_link or "",
            "{{cta_link}}": cta_link or "",
        }

        result = template
        for placeholder, value in substitutions.items():
            # Case-insensitive replacement
            result = result.replace(placeholder, str(value))
            result = result.replace(placeholder.lower(), str(value))
            result = result.replace(placeholder.upper(), str(value))

        # Clean up broken patterns from empty substitutions
        # e.g. "providers in , " → "providers in "
        # IMPORTANT: use [ \t]* (not \s*) so newlines are NEVER consumed —
        # consuming \n\n would collapse paragraph separators into one line.
        result = re.sub(r'\bin[ \t]*,[ \t]*,?[ \t]*', 'in ', result)  # "in , , " → "in "
        result = re.sub(r',[ \t]*,', ',', result)                        # ", ," → ","
        result = re.sub(r'[ \t]{2,}', ' ', result)                       # collapse multiple spaces/tabs only (NOT newlines)
        result = re.sub(r'[ \t]+,', ',', result)                         # " ," → ","  (spaces before comma only)
        result = re.sub(r',[ \t]*\.', '.', result)                       # ", ." → "." (spaces between comma/dot only)

        return result
    
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
        
        def replace_link(match):
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
        placeholders = {}
        def _stash(m):
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

        from pathlib import Path

        attachments_dir = Path(settings.ATTACHMENTS_DIR)
        loaded = []
        for attachment in getattr(email_template, "attachments", None) or []:
            file_path = attachments_dir / attachment.storage_path
            if not file_path.exists():
                logger.warning(
                    f"[SES] Attachment file missing on disk, skipping. "
                    f"AttachmentId={attachment.attachment_id}, Path={file_path}"
                )
                continue
            try:
                with open(file_path, "rb") as f:
                    loaded.append({
                        "filename": attachment.filename,
                        "content_type": attachment.content_type,
                        "data": f.read(),
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
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{att["filename"]}"',
                )
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
        
        Args:
            email_message: EmailMessage model instance
            email_template: EmailTemplate model instance  
            prospect: Prospect model instance
            enable_tracking: Whether to add open/click tracking
            sender_name: Optional sender name for signature
            from_email_address: Optional specific email address to send from
            
        Returns:
            Dict containing success status, message_id, SES message ID
            
        Raises:
            TransientEmailFailure: For retryable errors
            PermanentEmailFailure: For non-retryable errors
        """
        try:
            # Apply rate limiting
            await self.rate_limiter.acquire()
            
            # Get campaign CTA link / sender title if available
            campaign_cta_link = None
            campaign_sender_title = None
            if email_message.campaign:
                campaign_cta_link = normalize_cta_link(email_message.campaign.cta_link)
                campaign_sender_title = email_message.campaign.sender_title

            # Trust pre-computed subject/body if available
            # _preschedule_all_emails() personalizes and formats the email at launch.
            # We must use that snapshot so we don't accidentally pull in stale data
            # or overwrite creative formatting by re-substituting the raw template.
            if email_message.subject and email_message.subject.strip():
                subject = email_message.subject
            else:
                subject = self._substitute_placeholders(
                    email_template.subject,
                    prospect,
                    sender_name,
                    campaign_cta_link,
                    campaign_sender_title
                )
                # Keep DB snapshot aligned with what we actually send.
                subject = re.sub(r'<[^>]+>', '', subject or '')
                subject = subject.replace('\r', ' ').replace('\n', ' ')
                subject = re.sub(r'\s{2,}', ' ', subject).strip()
                email_message.subject = subject

            if email_message.body_text and email_message.body_text.strip():
                body_content = email_message.body_text
            else:
                body_content = self._substitute_placeholders(
                    email_template.body,
                    prospect,
                    sender_name,
                    campaign_cta_link,
                    campaign_sender_title
                )
                if not has_effective_cta_link(campaign_cta_link):
                    body_content = strip_cta_content_no_link(body_content)

                # Normalize unsubscribe content across creative/normal modes.
                body_content = self._normalize_unsubscribe_text(
                    body_content=body_content,
                    message_id=str(email_message.message_id),
                )
                email_message.body_text = body_content
            
            # --- Personal Email Guard ---
            # Block sending cold outreach to personal email providers (gmail, yahoo, etc.)
            # These providers aggressively flag B2B outreach as spam, hurting sender reputation.
            from app.services.compliance_service import lint_email_content, is_business_email
            if not is_business_email(prospect.email):
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

            # --- Pre-Send Lint Gate ---
            # Comprehensive pre-send analysis: keyword spam triggers, subject line
            # validation, Microsoft EOP ASF triggers, AI linguistic detection,
            # and link domain validation.
            if settings.SPAM_SCORE_BLOCK_THRESHOLD > 0:
                _sender_domain = (from_email_address or settings.SENDER_EMAIL).split("@")[-1] if "@" in (from_email_address or settings.SENDER_EMAIL) else ""
                _lint_result = lint_email_content(subject, body_content, _sender_domain)

                # Log all warnings for diagnosis even if score is below threshold
                if _lint_result.warnings:
                    logger.info(
                        f"[SES] Lint warnings for MessageId={email_message.message_id}: "
                        f"Score={_lint_result.composite_score}, "
                        f"Warnings={[w.message for w in _lint_result.warnings[:5]]}"
                    )

                if _lint_result.composite_score >= settings.SPAM_SCORE_BLOCK_THRESHOLD:
                    logger.warning(
                        f"[SES] Email BLOCKED by pre-send lint gate. "
                        f"Score={_lint_result.composite_score} (threshold={settings.SPAM_SCORE_BLOCK_THRESHOLD}), "
                        f"Triggers={_lint_result.trigger_names[:5]}, "
                        f"MessageId={email_message.message_id}, "
                        f"Recipient={prospect.email}"
                    )
                    return {
                        "success": False,
                        "message_id": email_message.message_id,
                        "status": "BLOCKED_SPAM",
                        "error": f"Lint score {_lint_result.composite_score} exceeds threshold {settings.SPAM_SCORE_BLOCK_THRESHOLD}. Triggers: {_lint_result.trigger_names[:5]}"
                    }

            # Build sender address early so we can do internal domain check
            final_sender_name = sender_name or settings.SENDER_NAME
            sender_email = from_email_address or settings.SENDER_EMAIL
            from_email = f"{final_sender_name} <{sender_email}>"

            # --- INTERNAL DOMAIN CHECK ---
            # If sender domain == recipient domain, it's an internal test or collaboration email.
            # Do NOT append Unsubscribe links or Physical Addresses, as they trigger impersonation/spam filters.
            sender_domain = sender_email.split('@')[1].lower() if '@' in sender_email else ''
            recipient_domain = prospect.email.split('@')[1].lower() if '@' in prospect.email else ''
            is_internal = (sender_domain == recipient_domain and sender_domain != '')

            unsubscribe_url = f"{settings.BASE_URL}/api/tracking/unsubscribe/{email_message.message_id}"

            if not is_internal:
                # CAN-SPAM §5(a)(5): physical postal address must appear in every email.
                if settings.COMPANY_PHYSICAL_ADDRESS not in body_content:
                    body_content += f"\n\n{settings.COMPANY_PHYSICAL_ADDRESS}"

                # Append unsubscribe footer (only if not already present).
                if "/api/tracking/unsubscribe/" not in body_content:
                    unsub_mode = getattr(email_message.campaign, 'unsubscribe_mode', 'plain') or 'plain'
                    if unsub_mode == 'html':
                        body_content += (
                            f'\nPlease click on <a href="{unsubscribe_url}">unsubscribe</a> '
                            f'in case you do not wish to receive such business communication emails in the future.'
                        )
                    else:
                        body_content += (
                            f"\nPlease reply 'unsubscribe' in case you do not wish to "
                            f"receive such business communication emails in the future."
                        )

            # Replace explicit unsubscribe placeholder if template uses it
            body_content = body_content.replace("{{unsubscribe_link}}", unsubscribe_url)

            # Ensure single signature. Remove the '$' anchor because 
            # physical address and unsubscribe footer are appended AFTER the body.
            signature_pattern = r"(?i)(Best(\s+Regards)?|Regards|Thanks|Cheers|Sincerely)[,]?\s*\n\s*[A-Za-z0-9 ]+"
            if not re.search(signature_pattern, body_content):
                body_content = body_content.rstrip() + f"\n\nRegards,\n{build_signature_block(sender_name, campaign_sender_title)}"

            # body_content = normalize_paragraph_spacing(body_content)
            # SES automatically inserts tracking pixels and wraps links when:
            # 1. ConfigurationSetName is specified in the send call
            # 2. Open/Click events are enabled in the Configuration Set
            # 3. SNS destination is configured to send events to our webhook
            # The webhook at /webhooks/ses/notifications handles these events.
            # 
            # Custom tracking is disabled (no BASE_URL dependency needed):
            # if enable_tracking:
            #     html_body = self._add_tracking_pixel(html_body, str(email_message.message_id))
            #     html_body = self._wrap_links_for_tracking(html_body, str(email_message.message_id))
            
            # Build MIME message with deliverability headers
            mime_msg = self._build_mime_message(
                from_email=from_email,
                to_email=prospect.email,
                subject=subject,
                body_content=body_content,
                message_id=str(email_message.message_id),
                campaign_id=str(email_message.campaign_id),
                sender_email=sender_email,
                # Same-domain (internal) mail carries no unsubscribe footer, so no list headers either
                unsubscribe_url=None if is_internal else unsubscribe_url,
                attachments=self._load_template_attachments(email_template),
                unsubscribe_mailto=sender_email if "@" in (sender_email or "") else None,
            )

            # Prepare SES raw email request
            send_kwargs = {
                "Source": from_email,
                "Destinations": [prospect.email],
                "RawMessage": {
                    "Data": mime_msg.as_string()
                },
            }

            # REQUIRED for bounce/complaint/delivery/open/click notifications to
            # fire at all: SES only emits these events for sends associated
            # with a Configuration Set that has an SNS destination attached.
            # Without this, /webhooks/ses/notifications never receives anything,
            # no matter how the SNS topic/subscription itself is configured.
            if settings.AWS_SES_CONFIGURATION_SET:
                send_kwargs["ConfigurationSetName"] = settings.AWS_SES_CONFIGURATION_SET

            # REQUIRED for the webhook to know WHICH EmailMessage a notification
            # is about: ses_webhook_router.py's _extract_tags() reads this tag
            # back out of the SNS payload as `internal_message_id` to look up
            # the row. Without it, bounce/open/click notifications arrive but
            # can never be matched to a specific message (SES tag values only
            # allow [A-Za-z0-9_-]; message_id is a UUID, which fits).
            send_kwargs["Tags"] = [
                {"Name": "message_id", "Value": str(email_message.message_id)}
            ]

            # Send email (run in thread to avoid blocking)
            response = await asyncio.to_thread(
                self.ses_client.send_raw_email,
                **send_kwargs
            )
            
            ses_message_id = response["MessageId"]
            # The MIME Message-ID (internet_message_id) is the standard RFC 5322
            # identifier that email clients put in In-Reply-To / References when
            # replying. We must store it so IMAP sync can match replies back to
            # this conversation thread.
            internet_message_id = mime_msg["Message-ID"]

            logger.info(
                f"[SES] Email sent successfully. "
                f"SES MessageId: {ses_message_id}, "
                f"Recipient: {prospect.email}, "
                f"Campaign: {email_message.campaign_id}"
            )

            return {
                "success": True,
                "ses_message_id": ses_message_id,
                "internet_message_id": internet_message_id,
                "message_id": email_message.message_id,
                "status": "SENT"
            }
            
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_message = exc.response["Error"]["Message"]
            
            logger.error(
                f"[SES] ClientError: {error_code} - {error_message}. "
                f"MessageId: {email_message.message_id}, "
                f"Recipient: {prospect.email}"
            )
            
            # Permanent failures - do NOT retry
            permanent_errors = {
                "MessageRejected",
                "MailFromDomainNotVerified", 
                "ConfigurationSetDoesNotExist",
                "AccountSendingPausedException",
                "InvalidParameterValue",   # malformed message (e.g. duplicate headers)
            }
            
            if error_code in permanent_errors:
                raise PermanentEmailFailure(f"{error_code}: {error_message}")
            
            # Transient failures - retryable
            raise TransientEmailFailure(f"{error_code}: {error_message}")
            
        except BotoCoreError as exc:
            logger.error(f"[SES] BotoCoreError: {str(exc)}")
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
            response = await asyncio.to_thread(
                self.ses_client.send_raw_email,
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
    
    async def send_with_retry(
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
            Send result dict
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                return await self.send_email(
                    email_message, 
                    email_template, 
                    prospect, 
                    True, 
                    sender_name,
                    from_email_address
                )
                
            except TransientEmailFailure as exc:
                last_error = exc
                wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                
                logger.warning(
                    f"[SES] Retry {attempt + 1}/{max_retries} for message {email_message.message_id}. "
                    f"Waiting {wait_time}s. Error: {exc}"
                )
                
                await asyncio.sleep(wait_time)
                
            except PermanentEmailFailure as exc:
                # Don't retry permanent failures
                logger.error(f"[SES] Permanent failure, not retrying: {exc}")
                return {
                    "success": False,
                    "message_id": email_message.message_id,
                    "status": "FAILED",
                    "error": str(exc)
                }
        
        # All retries exhausted
        return {
            "success": False,
            "message_id": email_message.message_id,
            "status": "FAILED",
            "error": str(last_error) if last_error else "Max retries exceeded"
        }
    
    def verify_email(self, email: str) -> bool:
        """
        Send verification email (for SES sandbox mode).
        
        Args:
            email: Email address to verify
            
        Returns:
            True if verification request sent successfully
        """
        try:
            self.ses_client.verify_email_identity(EmailAddress=email)
            logger.info(f"[SES] Verification email sent to {email}")
            return True
        except ClientError as exc:
            logger.error(f"[SES] Failed to verify {email}: {exc}")
            return False
    
    def get_send_quota(self) -> Dict[str, Any]:
        """Get current SES sending quota and usage."""
        try:
            response = self.ses_client.get_send_quota()
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
