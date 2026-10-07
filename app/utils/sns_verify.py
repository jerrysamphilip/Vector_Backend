# app/utils/sns_verify.py
"""
Authenticity checks for the SES/SNS webhook (BR-DF-09).

- Wrapped SNS messages (Notification, SubscriptionConfirmation, ...) are verified against
  AWS's signature, using the signing certificate from an sns.<region>.amazonaws.com URL.
- SubscribeURL is only followed when it points at SNS itself, so the endpoint can't be
  used to make the server fetch arbitrary URLs.
- Raw-delivery messages carry no signature, so they must present the shared
  SES_WEBHOOK_TOKEN (query ?token= or X-Webhook-Token header).
"""

import base64
import hmac
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_cert_cache: dict = {}

_SIGNED_FIELDS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}


def is_sns_url(url: Optional[str]) -> bool:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(_SNS_HOST.match(parsed.hostname or ""))


def string_to_sign(message: dict) -> Optional[str]:
    fields = _SIGNED_FIELDS.get(message.get("Type"))
    if not fields:
        return None
    parts = []
    for field in fields:
        if field == "Subject" and message.get("Subject") is None:
            continue
        if message.get(field) is None:
            return None
        parts.append(f"{field}\n{message[field]}\n")
    return "".join(parts)


async def _signing_certificate(url: str):
    if url not in _cert_cache:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
        _cert_cache[url] = x509.load_pem_x509_certificate(response.content)
    return _cert_cache[url]


async def verify_sns_signature(message: dict, expected_topic_arn: str = "") -> bool:
    if expected_topic_arn and message.get("TopicArn") != expected_topic_arn:
        logger.warning("[SNS] Rejected message for unexpected topic %s", message.get("TopicArn"))
        return False
    cert_url = message.get("SigningCertURL") or message.get("SigningCertUrl")
    if not is_sns_url(cert_url) or not cert_url.endswith(".pem"):
        logger.warning("[SNS] Rejected message: signing certificate URL is not an SNS URL")
        return False
    to_sign = string_to_sign(message)
    if to_sign is None or not message.get("Signature"):
        return False
    algorithm = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(message.get("SignatureVersion")))
    if algorithm is None:
        return False
    try:
        certificate = await _signing_certificate(cert_url)
        certificate.public_key().verify(base64.b64decode(message["Signature"]), to_sign.encode(),
                                        padding.PKCS1v15(), algorithm)
        return True
    except (InvalidSignature, ValueError, httpx.HTTPError) as exc:
        logger.warning("[SNS] Signature verification failed: %s", exc)
        return False


def token_matches(presented: Optional[str], expected: str) -> bool:
    return bool(presented) and bool(expected) and hmac.compare_digest(presented, expected)
