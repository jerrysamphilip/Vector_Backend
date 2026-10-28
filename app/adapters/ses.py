"""
Amazon SES behind a Protocol (ADR 0009 adapter layer, chunk S2).

Only the transport: one boto3 SES client per configuration (region / access key / endpoint), built
with ``total_max_attempts=1`` so the SDK never resends a message SES may already have accepted
(F079). Errors are raised exactly as boto3 raises them (``ClientError``, ``BotoCoreError``, urllib3 /
http.client errors); the callers classify them (app/services/send_errors.py and
app/services/email_sender_service.py: deferred / permanent / transient / ambiguous).

Unit tests hand services a fake ``EmailTransport`` through ``app.adapters.providers.override``.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional, Protocol, Tuple

import boto3
from botocore.config import Config as BotoConfig

from app.core.config import settings

# One attempt per message (total_max_attempts counts the first request; "max_attempts" would mean
# retries): botocore's own retries would resend mail SES may already have accepted (F079).
SES_CLIENT_CONFIG = BotoConfig(retries={"total_max_attempts": 1, "mode": "standard"})


class EmailTransport(Protocol):
    """What the app sends mail through. Keyword arguments are SES's own (Source, Destinations,
    RawMessage, Destination, Message, ConfigurationSetName, ...); results are SES's response dicts."""

    def send_raw(self, **kwargs: Any) -> Dict[str, Any]:
        """SendRawEmail; returns at least ``{"MessageId": ...}``."""
        ...

    def send_simple(self, **kwargs: Any) -> Dict[str, Any]:
        """SendEmail (formatted Subject / Body); returns at least ``{"MessageId": ...}``."""
        ...

    def verify_email_identity(self, email: str) -> None:
        ...

    def get_send_quota(self) -> Dict[str, Any]:
        ...


class SesEmailTransport:
    """EmailTransport over one boto3 SES client (thread-safe, so shared by the API and the worker)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def send_raw(self, **kwargs: Any) -> Dict[str, Any]:
        response: Dict[str, Any] = self.client.send_raw_email(**kwargs)
        return response

    def send_simple(self, **kwargs: Any) -> Dict[str, Any]:
        response: Dict[str, Any] = self.client.send_email(**kwargs)
        return response

    def verify_email_identity(self, email: str) -> None:
        self.client.verify_email_identity(EmailAddress=email)

    def get_send_quota(self) -> Dict[str, Any]:
        response: Dict[str, Any] = self.client.get_send_quota()
        return response


ClientKey = Tuple[str, Optional[str], Optional[str]]
_clients: Dict[ClientKey, Any] = {}
_lock = threading.Lock()


def _client_key() -> ClientKey:
    endpoint = os.environ.get("AWS_ENDPOINT_URL_SES") or os.environ.get("AWS_ENDPOINT_URL") or None
    return settings.AWS_REGION, settings.AWS_ACCESS_KEY_ID or None, endpoint


def build_ses_client() -> Any:
    """A new boto3 SES client for the current settings (IAM role, or explicit keys in local dev)."""
    kwargs: Dict[str, Any] = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("ses", config=SES_CLIENT_CONFIG, **kwargs)


def shared_ses_client() -> Any:
    """The cached SES client for the current settings (built on first use; T-5)."""
    key = _client_key()
    client = _clients.get(key)
    if client is not None:
        return client
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = build_ses_client()
            _clients[key] = client
    return client


def reset_ses_clients() -> None:
    """Forget the cached clients (tests, credential rotation)."""
    with _lock:
        _clients.clear()


def ses_transport() -> SesEmailTransport:
    """The real transport over the shared client."""
    return SesEmailTransport(shared_ses_client())
