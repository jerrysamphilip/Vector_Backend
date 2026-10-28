"""
Mailbox SMTP / IMAP connections behind Protocols (ADR 0009 adapter layer, chunk S2).

``TlsMailConnector`` wraps app/core/tls.py (verified TLS 1.2+, STARTTLS required on plain ports) and
closes the DNS-rebinding gap of F007: in a deployed environment the host is resolved ONCE, every
address is checked with app/utils/host_safety.py (no loopback / private / link-local / metadata), and
the socket goes to the checked addresses (tried in resolver order) while SNI and the certificate's
host name check still use the host name. A name that later resolves elsewhere can't redirect the connection.

On a local setup (``settings.is_local_environment``: mailpit, test servers) nothing is resolved here,
as in app/routers/inbox_router.py ``_check_mail_host``: the libraries connect to the name as before.

Callers get the connector from ``app.adapters.providers.mail_connector()``; unit tests swap in a fake.
"""
from __future__ import annotations

import imaplib
import smtplib
from typing import List, Optional, Protocol

from app.core import tls
from app.core.config import settings
from app.utils import host_safety


class ImapConnector(Protocol):
    def open_imap(self, host: str, port: Optional[int], *, timeout: Optional[float] = None,
                  starttls: Optional[bool] = None) -> imaplib.IMAP4:
        """A connected, encrypted, not yet logged-in IMAP client."""
        ...


class SmtpConnector(Protocol):
    def open_smtp(self, host: str, port: int, *, use_ssl: bool, timeout: float) -> smtplib.SMTP:
        """A connected, encrypted, not yet logged-in SMTP client."""
        ...


class MailConnector(ImapConnector, SmtpConnector, Protocol):
    """Both directions of a mailbox."""


def pinned_addresses(host: str, port: Optional[int],
                     resolver: Optional[host_safety.Resolver] = None) -> Optional[List[str]]:
    """The checked IPs to connect to for ``host``, or None on a local setup (no pinning).

    Raises tls.UnsafeMailHostError (an OSError, rendered by tls.tls_error_message) when the host or
    any address it resolves to is not allowed."""
    if settings.is_local_environment:
        return None
    try:
        addresses = host_safety.check_host(host, port or 0, allow_private=False,
                                           resolver=resolver or host_safety.resolve)
    except host_safety.UnsafeHostError as exc:
        raise tls.UnsafeMailHostError(f"Mail server not allowed: {exc}") from exc
    return addresses


class TlsMailConnector:
    """The real connector: app/core/tls.py, pinned to the checked addresses when deployed."""

    def __init__(self, resolver: Optional[host_safety.Resolver] = None) -> None:
        self._resolver = resolver

    def open_imap(self, host: str, port: Optional[int], *, timeout: Optional[float] = None,
                  starttls: Optional[bool] = None) -> imaplib.IMAP4:
        addresses = pinned_addresses(host, port or tls.IMAP_SSL_PORT, self._resolver)
        return tls.open_imap(host, port, timeout=timeout, starttls=starttls, addresses=addresses)

    def open_smtp(self, host: str, port: int, *, use_ssl: bool, timeout: float) -> smtplib.SMTP:
        addresses = pinned_addresses(host, port, self._resolver)
        return tls.open_smtp(host, port, use_ssl=use_ssl, timeout=timeout, addresses=addresses)
