# app/core/tls.py
"""
TLS for mailbox SMTP / IMAP connections.

smtplib and imaplib skip certificate checks unless they are handed a context, so every
mailbox connection goes through here: certificates and host names are verified (TLS 1.2+),
and credentials are only ever sent over TLS - a plain-port server must offer STARTTLS.

MAIL_TLS_VERIFY=false turns verification off for local fake servers with self-signed
certificates (logged as a warning); it is refused in production (app/core/config.py).

``addresses`` (S2, DNS rebinding): the caller resolved and checked the host once
(app/adapters/mail.py) and passes the checked IPs; the socket goes to one of them (tried in order, as
socket.create_connection does with a DNS answer) while SNI, STARTTLS and the certificate's host name
check still use ``host``, so a second DNS answer can't redirect us.
"""

import imaplib
import logging
import smtplib
import socket
import ssl
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

IMAP_SSL_PORT = 993
IMAP_STARTTLS_PORT = 143


class MailTLSError(OSError):
    """The server can't give us an encrypted connection (e.g. no STARTTLS), so we won't log in."""


class UnsafeMailHostError(OSError):
    """The mail server's host may not be connected to (SSRF, F007); the message says why."""


def _pinned_connection(addresses: Sequence[str], port: int, timeout: Optional[float],
                       source_address: Any = None) -> socket.socket:
    """A TCP connection to the first reachable already-checked address (no second DNS lookup)."""
    if timeout is not None and not timeout:
        raise ValueError("Non-blocking socket (timeout=0) is not supported")
    errors: List[OSError] = []
    for address in addresses:
        try:
            if timeout is None:
                return socket.create_connection((address, port), source_address=source_address)
            return socket.create_connection((address, port), timeout, source_address)
        except OSError as exc:
            errors.append(exc)
    raise errors[-1] if errors else OSError("no address to connect to")


class _PinnedIMAP4(imaplib.IMAP4):
    """IMAP4 whose socket goes to a fixed IP; ``self.host`` (SNI / STARTTLS) stays the name."""

    def __init__(self, host: str, port: int, *, addresses: Sequence[str], timeout: Optional[float] = None) -> None:
        self._pinned_addresses = tuple(addresses)
        super().__init__(host, port, timeout)

    def _create_socket(self, timeout: Optional[float]) -> socket.socket:
        return _pinned_connection(self._pinned_addresses, self.port, timeout)


class _PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, *, addresses: Sequence[str], ssl_context: ssl.SSLContext,
                 timeout: Optional[float] = None) -> None:
        self._pinned_addresses = tuple(addresses)
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: Optional[float]) -> socket.socket:
        sock = _pinned_connection(self._pinned_addresses, self.port, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class _PinnedSMTP(smtplib.SMTP):
    """SMTP whose socket goes to a fixed IP; ``self._host`` (SNI / STARTTLS) stays the name."""

    def __init__(self, host: str, port: int, *, addresses: Sequence[str], timeout: float) -> None:
        self._pinned_addresses = tuple(addresses)
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return _pinned_connection(self._pinned_addresses, port, timeout, self.source_address)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, host: str, port: int, *, addresses: Sequence[str], timeout: float, context: ssl.SSLContext) -> None:
        self._pinned_addresses = tuple(addresses)
        self._pinned_host = host
        super().__init__(host, port, timeout=timeout, context=context)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = _pinned_connection(self._pinned_addresses, port, timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._pinned_host)


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
              starttls: Optional[bool] = None,
              addresses: Optional[Sequence[str]] = None) -> imaplib.IMAP4:
    """
    Connected, encrypted (not yet logged-in) IMAP client.
    Implicit TLS by default; STARTTLS on port 143 (or when starttls=True), refusing servers without it.
    ``addresses``: connect to these already-checked IPs instead of resolving ``host`` again.
    """
    port = port or IMAP_SSL_PORT
    use_starttls = (port == IMAP_STARTTLS_PORT) if starttls is None else starttls
    ctx = tls_context()
    imap: imaplib.IMAP4
    if not use_starttls:
        if addresses:
            return _PinnedIMAP4_SSL(host, port, addresses=addresses, ssl_context=ctx, timeout=timeout)
        return imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
    if addresses:
        imap = _PinnedIMAP4(host, port, addresses=addresses, timeout=timeout)
    else:
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


def open_smtp(host: str, port: int, *, use_ssl: bool, timeout: float,
              addresses: Optional[Sequence[str]] = None) -> smtplib.SMTP:
    """Connected, encrypted (not yet logged-in) SMTP client: implicit TLS, or a required STARTTLS.
    ``addresses``: connect to these already-checked IPs instead of resolving ``host`` again."""
    ctx = tls_context()
    smtp: smtplib.SMTP
    if use_ssl:
        if addresses:
            return _PinnedSMTP_SSL(host, port, addresses=addresses, timeout=timeout, context=ctx)
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
    if addresses:
        smtp = _PinnedSMTP(host, port, addresses=addresses, timeout=timeout)
    else:
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
    if isinstance(exc, (MailTLSError, UnsafeMailHostError)):
        return str(exc)
    if isinstance(exc, ssl.SSLError):
        return f"Could not establish a secure (TLS) connection to the mail server: {exc}"
    return None
