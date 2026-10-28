# app/services/ses_client.py
"""
One shared Amazon SES client per configuration (T-5).

Building a boto3 client is expensive (endpoint resolution, credential chain, a new HTTPS
connection pool), so transactional mail (invitations, password resets) reuses one client
per region / access key / endpoint instead of making one per message.

The client itself lives in the SES adapter (app/adapters/ses.py, chunk S2): built with
``total_max_attempts=1`` (the SDK never resends mail on its own, F079) and shared with campaign
sending (app/services/email_sender_service.py). These names stay for existing callers.
"""

from typing import Any

from app.adapters.ses import reset_ses_clients as _reset_ses_clients
from app.adapters.ses import shared_ses_client


def get_ses_client() -> Any:
    """The cached SES client for the current settings (built on first use)."""
    return shared_ses_client()


def reset_ses_clients() -> None:
    """Forget the cached clients (tests, credential rotation)."""
    _reset_ses_clients()
