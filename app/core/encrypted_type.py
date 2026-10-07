# app/core/encrypted_type.py
"""
Column type that encrypts values at rest with Fernet (AES-128-CBC + HMAC), used for
mailbox SMTP/IMAP passwords (BR-DF-09). Code keeps reading and writing plain strings.

Stored form is "enc:v1:<token>". Values without the prefix are legacy plain text: they
are still readable, and app/db/security_schema.py encrypts them on start-up.
"""

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import String, Text, TypeDecorator

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


@lru_cache()
def _fernet() -> Fernet:
    from app.core.config import settings
    from app.core.secrets_guard import credentials_fernet_key
    return Fernet(credentials_fernet_key(settings.CREDENTIALS_ENCRYPTION_KEY))


def check_credentials_key():
    """Fail fast at start-up (deployed) if CREDENTIALS_ENCRYPTION_KEY is missing."""
    _fernet()


def encrypt_value(value):
    if value is None or value == "" or str(value).startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(str(value).encode()).decode()


def decrypt_value(value):
    if value is None or not str(value).startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("Stored credential could not be decrypted: CREDENTIALS_ENCRYPTION_KEY has changed.")
        return None


class EncryptedString(TypeDecorator):
    impl = String(1024)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_value(value)

    def process_result_value(self, value, dialect):
        return decrypt_value(value)


class EncryptedText(EncryptedString):
    """Encrypted at rest like EncryptedString, for long values such as OAuth tokens."""
    impl = Text
    cache_ok = True
