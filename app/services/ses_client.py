# app/services/ses_client.py
"""
One shared Amazon SES client per configuration (T-5).

Building a boto3 client is expensive (endpoint resolution, credential chain, a new HTTPS
connection pool), so transactional mail (invitations, password resets) reuses one client
per region / access key / endpoint instead of making one per message. boto3 clients are
thread-safe; the lock only makes sure two first callers do not both build one.
"""

import os
import threading
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig

from app.core.config import settings

# One attempt per message: the SDK must never resend mail on its own (F079, as in email_sender_service)
_CLIENT_CONFIG = BotoConfig(retries={"total_max_attempts": 1, "mode": "standard"})

_clients: Dict[Tuple[str, Optional[str], Optional[str]], Any] = {}
_lock = threading.Lock()


def _client_key() -> Tuple[str, Optional[str], Optional[str]]:
    endpoint = os.environ.get("AWS_ENDPOINT_URL_SES") or os.environ.get("AWS_ENDPOINT_URL") or None
    return settings.AWS_REGION, settings.AWS_ACCESS_KEY_ID or None, endpoint


def get_ses_client() -> Any:
    """The cached SES client for the current settings (built on first use)."""
    key = _client_key()
    client = _clients.get(key)
    if client is not None:
        return client
    with _lock:
        client = _clients.get(key)
        if client is None:
            kwargs: Dict[str, Any] = {"region_name": settings.AWS_REGION}
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
            client = boto3.client("ses", config=_CLIENT_CONFIG, **kwargs)
            _clients[key] = client
    return client


def reset_ses_clients() -> None:
    """Forget the cached clients (tests, credential rotation)."""
    with _lock:
        _clients.clear()
