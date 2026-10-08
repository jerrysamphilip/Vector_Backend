# app/core/secrets_guard.py
"""
Start-up checks for the secrets the app signs and encrypts with (BR-DF-09).

- JWT_SECRET_KEY: the code default is public, so anyone could forge a login token with it.
  Deployed environments refuse to start with it (or with anything shorter than 32 chars);
  a local run gets a random per-process secret and a warning instead.
- CREDENTIALS_ENCRYPTION_KEY: encrypts mailbox passwords at rest (app/core/encrypted_type.py).
  Required when deployed; a local run falls back to a fixed development key with a warning.

"Deployed" means running in Kubernetes or ENVIRONMENT set to production/staging.
"""

import base64
import hashlib
import logging
import os
import secrets

logger = logging.getLogger(__name__)

DEFAULT_JWT_SECRET = "change-this-in-production-use-a-long-random-string"
_DEV_CREDENTIALS_KEY = "outreach-ai-local-development-only-credentials-key"


def is_deployed() -> bool:
    env = (os.getenv("ENVIRONMENT") or "").strip().lower()
    return bool(os.getenv("KUBERNETES_SERVICE_HOST")) or env in {"production", "prod", "staging", "stage"}


def resolve_jwt_secret(configured: str) -> str:
    if configured and configured != DEFAULT_JWT_SECRET and len(configured) >= 32:
        return configured
    if is_deployed():
        raise RuntimeError(
            "JWT_SECRET_KEY is missing, set to the public default, or shorter than 32 characters. "
            "Set it to a long random value (e.g. `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`) "
            "in the deployment secret before starting the API."
        )
    print("WARNING JWT_SECRET_KEY not set: using a random per-process secret (local only; sign-ins end on restart).")
    return secrets.token_urlsafe(48)


def credentials_fernet_key(configured: str) -> bytes:
    """Fernet key from CREDENTIALS_ENCRYPTION_KEY. Any string works; it's stretched with SHA-256."""
    if configured and is_deployed() and configured == _DEV_CREDENTIALS_KEY:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is set to the public development key. "
            "Set a long random value in the deployment secret before starting the API."
        )
    if not configured:
        if is_deployed():
            raise RuntimeError(
                "CREDENTIALS_ENCRYPTION_KEY is not set. Mailbox passwords are encrypted with it; set a long random "
                "value in the deployment secret, and keep it: changing it makes saved mailbox passwords unreadable."
            )
        print("WARNING CREDENTIALS_ENCRYPTION_KEY not set: using the local development key.")
        configured = _DEV_CREDENTIALS_KEY
    return base64.urlsafe_b64encode(hashlib.sha256(configured.encode()).digest())
