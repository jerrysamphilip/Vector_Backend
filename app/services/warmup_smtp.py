"""
Per-inbox SMTP sender for warmup emails.

Tries per-inbox SMTP credentials first; falls back to the SES relay
when no SMTP credentials are configured on the inbox.
"""
import asyncio
import email.utils
import logging
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.models.sending_inbox import SendingInbox
from app.services.email_sender_service import email_sender

logger = logging.getLogger(__name__)


def _display_name(email_address: str) -> str:
    local = email_address.split("@")[0]
    return " ".join(chunk.capitalize() for chunk in local.replace("_", ".").split(".") if chunk)


def _build_mime_message(
    from_email: str,
    from_name: str,
    to_email: str,
    subject: str,
    body: str,
    extra_headers: Optional[dict] = None,
) -> tuple:
    """Build a MIME text message. Returns (MIMEMultipart, message_id)."""
    msg = MIMEMultipart("alternative")
    message_id = f"<warmup-{uuid.uuid4()}@warmup.internal>"
    msg["Message-ID"] = message_id
    msg["From"] = email.utils.formataddr((from_name, from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=False)
    skip = {"message-id", "from", "to", "subject", "date"}
    for key, value in (extra_headers or {}).items():
        if key.lower() not in skip:
            msg[key] = str(value)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg, message_id


def _smtp_send_blocking(inbox: SendingInbox, to_email: str, msg: MIMEMultipart) -> None:
    """
    Blocking SMTP send using per-inbox credentials.
    Call via asyncio.to_thread().
    Raises on any failure.
    """
    host = inbox.smtp_host
    port = inbox.smtp_port or 587
    username = inbox.smtp_username or inbox.email_address
    password = inbox.smtp_password
    use_ssl = inbox.smtp_use_ssl or False

    if use_ssl:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()

    try:
        smtp.login(username, password)
        smtp.sendmail(inbox.email_address, [to_email], msg.as_bytes())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


async def send_via_inbox(
    inbox: SendingInbox,
    to_email: str,
    subject: str,
    body: str,
    extra_headers: Optional[dict] = None,
) -> dict:
    """
    Send a warmup email from inbox → to_email.

    Strategy:
      1. Per-inbox SMTP if smtp_host + smtp_password are set.
      2. SES relay fallback otherwise (or if SMTP fails).

    Returns:
      {"success": bool, "internet_message_id": str | None, "error": str | None}
    """
    from_name = _display_name(inbox.email_address)
    msg, message_id = _build_mime_message(
        inbox.email_address, from_name, to_email, subject, body, extra_headers
    )

    # --- Attempt per-inbox SMTP ---
    if inbox.smtp_host and inbox.smtp_password:
        try:
            await asyncio.to_thread(_smtp_send_blocking, inbox, to_email, msg)
            logger.debug(
                "[WarmupSMTP] Sent via per-inbox SMTP %s → %s", inbox.email_address, to_email
            )
            return {"success": True, "internet_message_id": message_id, "error": None}
        except Exception as exc:
            logger.warning(
                "[WarmupSMTP] Per-inbox SMTP failed for %s: %s — trying SES fallback",
                inbox.email_address,
                exc,
            )

    # --- SES relay fallback ---
    # Strip Message-ID from extra_headers — SES sets its own and rejects duplicates.
    ses_headers = {k: v for k, v in (extra_headers or {}).items() if k.lower() != "message-id"}
    try:
        result = await email_sender.send_plain_email(
            to_email=to_email,
            subject=subject,
            body_content=body,
            from_email_address=inbox.email_address,
            sender_name=from_name,
            extra_headers=ses_headers,
        )
        if result.get("success"):
            return {
                "success": True,
                "internet_message_id": result.get("internet_message_id") or message_id,
                "error": None,
            }
        return {"success": False, "internet_message_id": None, "error": result.get("error", "Send failed")}
    except Exception as exc:
        logger.error("[WarmupSMTP] SES fallback failed for %s: %s", inbox.email_address, exc)
        return {"success": False, "internet_message_id": None, "error": str(exc)}
