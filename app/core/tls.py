# app/core/tls.py
"""
TLS for mailbox SMTP / IMAP connections.

smtplib and imaplib skip certificate checks unless they are handed a context, so every
mailbox connection goes through here: certificates and host names are verified (TLS 1.2+),
and credentials are only ever sent over TLS - a plain-port server must offer STARTTLS.

MAIL_TLS_VERIFY=false turns verification off for local fake servers with self-signed
certificates (logged as a warning); it is refused in production (app/core/config.py).
"""

import imaplib
import logging
import smtplib
import ssl
from typing import Optional

logger = logging.getLogger(__name__)

IMAP_SSL_PORT = 993
IMAP_STARTTLS_PORT = 143


class MailTLSError(OSError):
    """The server can't give us an encrypted connection (e.g. no STARTTLS), so we won't log in."""


def tls_context() -> ssl.SSLContext:
    """Client context for mail servers: verified certificate + host name, TLS 1.2 minimum."""
    from app.core.config import settings

    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not settings.MAIL_TLS_VERIFY:
        if settings.is_production:
            logger.error("MAIL_TLS_VERIFY=false is ignored in production; mail server certificates are verified.")
        else:
            logger.warning("MAIL_TLS_VERIFY=false: mail server TLS certificates are NOT verified (local dev/test only).")
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    return ctx


def open_imap(host: str, port: Optional[int], *, timeout: Optional[float] = None,
              starttls: Optional[bool] = None) -> imaplib.IMAP4:
    """
    Connected, encrypted (not yet logged-in) IMAP client.
    Implicit TLS by default; STARTTLS on port 143 (or when starttls=True), refusing servers without it.
    """
    port = port or IMAP_SSL_PORT
    use_starttls = (port == IMAP_STARTTLS_PORT) if starttls is None else starttls
    ctx = tls_context()
    if not use_starttls:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
    imap = imaplib.IMAP4(host, port, timeout=timeout)
    try:
        if "STARTTLS" not in imap.capabilities:
            raise MailTLSError(
                f"IMAP server {host}:{port} does not support STARTTLS, so the password would be sent "
                f"unencrypted. Use the server's SSL port (usually {IMAP_SSL_PORT}).")
        imap.starttls(ssl_context=ctx)
    except BaseException:
        try:
            imap.shutdown()
        except OSError:
            pass
        raise
    return imap


def open_smtp(host: str, port: int, *, use_ssl: bool, timeout: float) -> smtplib.SMTP:
    """Connected, encrypted (not yet logged-in) SMTP client: implicit TLS, or a required STARTTLS."""
    ctx = tls_context()
    if use_ssl:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    smtp = smtplib.SMTP(host, port, timeout=timeout)
    try:
        smtp.ehlo()
        if not smtp.has_extn("starttls"):
            raise MailTLSError(
                f"SMTP server {host}:{port} does not support STARTTLS, so the password would be sent "
                "unencrypted. Use port 465 with SSL, or 587 with STARTTLS.")
        smtp.starttls(context=ctx)
        smtp.ehlo()
    except BaseException:
        smtp.close()
        raise
    return smtp


def tls_error_message(exc: BaseException) -> Optional[str]:
    """A user-facing explanation for TLS failures, or None for anything else."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        reason = getattr(exc, "verify_message", None) or str(exc)
        return (f"The mail server's TLS certificate could not be verified ({reason}). "
                "Check the host name matches the server's certificate (e.g. imap.gmail.com, not an IP or alias).")
    if isinstance(exc, MailTLSError):
        return str(exc)
    if isinstance(exc, ssl.SSLError):
        return f"Could not establish a secure (TLS) connection to the mail server: {exc}"
    return None
