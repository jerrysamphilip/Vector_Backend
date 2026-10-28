# app/utils/sns_verify.py
"""
Authenticity checks for the SES/SNS webhook (BR-DF-09; team review C-04 / C-09).

- Wrapped SNS messages (Notification, SubscriptionConfirmation, UnsubscribeConfirmation) are verified
  against AWS's signature (SignatureVersion 1 = SHA1, 2 = SHA256, RSA PKCS#1 v1.5), using the signing
  certificate from an https://sns.<region>.amazonaws.com[.cn]/...pem URL (no userinfo, port 443 only).
- The certificate must be an RSA certificate inside its validity window; it is cached per URL with a
  TTL, a size cap and a download byte limit.
- Messages older than ``MAX_MESSAGE_AGE`` (24 h, by their signed Timestamp) are refused (replay).
- The topic must match AWS_SNS_TOPIC_ARN when that is set, and be one of SNS_ALLOWED_TOPIC_ARNS
  (comma-separated; empty = any) when that is set.
- A certificate that cannot be downloaded raises ``SigningCertificateUnavailable`` (the webhook answers
  503 so SNS retries) instead of counting as a bad signature (403, which SNS does not retry).
- SubscribeURL is only followed when it points at SNS itself, so the endpoint can't be used to make
  the server fetch arbitrary URLs.
- Raw-delivery messages carry no signature, so they must present the shared SES_WEBHOOK_TOKEN
  (query ?token= or X-Webhook-Token header).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.clock import utcnow
from app.core.config import settings

logger = logging.getLogger(__name__)

_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")

MAX_MESSAGE_AGE = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=15)  # a Timestamp this far in the future is refused too
CERT_CACHE_TTL_SECONDS = 3600.0
CERT_CACHE_MAX_ENTRIES = 32
CERT_MAX_BYTES = 64 * 1024
CERT_DOWNLOAD_TIMEOUT = 5.0


class SigningCertificateUnavailable(Exception):
    """The signing certificate could not be downloaded (network / AWS error): retry later (503)."""


class _CertificateCache:
    """URL -> (certificate, fetched_at): entries expire after ``ttl``; oldest evicted beyond ``max_entries``."""

    def __init__(self, ttl: float = CERT_CACHE_TTL_SECONDS, max_entries: int = CERT_CACHE_MAX_ENTRIES) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[x509.Certificate, float]] = OrderedDict()

    def get(self, url: str) -> Optional[x509.Certificate]:
        entry = self._entries.get(url)
        if entry is None:
            return None
        certificate, fetched_at = entry
        if time.monotonic() - fetched_at > self.ttl:
            del self._entries[url]
            return None
        self._entries.move_to_end(url)
        return certificate

    def put(self, url: str, certificate: x509.Certificate) -> None:
        self._entries[url] = (certificate, time.monotonic())
        self._entries.move_to_end(url)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, url: object) -> bool:
        return url in self._entries


_cert_cache = _CertificateCache()

_SIGNED_FIELDS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}

_ALGORITHMS = {"1": hashes.SHA1, "2": hashes.SHA256}


def is_sns_url(url: Optional[str]) -> bool:
    """An https URL on an SNS host, without userinfo and on the default port (443)."""
    try:
        parsed = urlparse(url or "")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False
    if port not in (None, 443):
        return False
    return bool(_SNS_HOST.match(parsed.hostname or ""))


def string_to_sign(message: dict) -> Optional[str]:
    fields = _SIGNED_FIELDS.get(message.get("Type") or "")
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


def _configured_topics() -> Any:
    """The SNS_ALLOWED_TOPIC_ARNS setting."""
    return settings.SNS_ALLOWED_TOPIC_ARNS


def allowed_topic_arns() -> frozenset[str]:
    """SNS_ALLOWED_TOPIC_ARNS (comma-separated); empty set = any topic."""
    raw = _configured_topics()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(item) for item in raw]
    else:
        items = str(raw or "").split(",")
    return frozenset(item.strip() for item in items if item.strip())


def topic_allowed(topic_arn: Optional[str], expected_topic_arn: str = "") -> bool:
    if expected_topic_arn and topic_arn != expected_topic_arn:
        return False
    allowed = allowed_topic_arns()
    return not allowed or topic_arn in allowed


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def timestamp_fresh(value: Any, now: Optional[datetime] = None) -> bool:
    """The signed Timestamp is at most 24 h old (and not more than the clock skew in the future)."""
    stamp = _parse_timestamp(value)
    if stamp is None:
        return False
    current = now or utcnow()
    return current - MAX_MESSAGE_AGE <= stamp <= current + MAX_CLOCK_SKEW


def certificate_usable(certificate: x509.Certificate, now: Optional[datetime] = None) -> bool:
    """RSA public key and ``now`` inside the certificate's validity window."""
    if not isinstance(certificate.public_key(), rsa.RSAPublicKey):
        return False
    current = now or utcnow()
    not_before = certificate.not_valid_before_utc.replace(tzinfo=None)
    not_after = certificate.not_valid_after_utc.replace(tzinfo=None)
    return not_before <= current <= not_after


async def _download(url: str) -> bytes:
    """The certificate bytes, at most CERT_MAX_BYTES; SigningCertificateUnavailable on any transport error."""
    try:
        async with httpx.AsyncClient(timeout=CERT_DOWNLOAD_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > CERT_MAX_BYTES:
                        raise ValueError("signing certificate is too large")
                return bytes(buf)
    except httpx.HTTPError as exc:
        raise SigningCertificateUnavailable(str(exc)) from exc


async def _signing_certificate(url: str) -> x509.Certificate:
    cached = _cert_cache.get(url)
    if cached is not None:
        return cached
    certificate = x509.load_pem_x509_certificate(await _download(url))
    _cert_cache.put(url, certificate)
    return certificate


def _envelope_problem(message: dict, expected_topic_arn: str) -> Optional[str]:
    """Why the envelope is refused before any network access, or None."""
    if not topic_allowed(message.get("TopicArn"), expected_topic_arn):
        return f"unexpected topic {message.get('TopicArn')}"
    cert_url: str = message.get("SigningCertURL") or message.get("SigningCertUrl") or ""
    if not is_sns_url(cert_url) or not urlparse(cert_url).path.endswith(".pem"):
        return "signing certificate URL is not an SNS URL"
    if string_to_sign(message) is None or not message.get("Signature"):
        return "missing signed fields"
    if str(message.get("SignatureVersion")) not in _ALGORITHMS:
        return "unsupported SignatureVersion"
    if not timestamp_fresh(message.get("Timestamp")):
        return "Timestamp is missing or older than 24 h"
    return None


async def verify_sns_signature(message: dict, expected_topic_arn: str = "") -> bool:
    """True when ``message`` is a genuine, fresh SNS message for an accepted topic.

    Raises SigningCertificateUnavailable when the certificate cannot be downloaded (retryable).
    """
    problem = _envelope_problem(message, expected_topic_arn)
    if problem:
        logger.warning("[SNS] Rejected message: %s", problem)
        return False
    cert_url: str = message.get("SigningCertURL") or message.get("SigningCertUrl") or ""
    to_sign = string_to_sign(message) or ""
    algorithm = _ALGORITHMS[str(message.get("SignatureVersion"))]()
    try:
        certificate = await _signing_certificate(cert_url)
        public_key = certificate.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey) or not certificate_usable(certificate):
            logger.warning("[SNS] Rejected message: signing certificate is not RSA or not currently valid")
            return False
        public_key.verify(base64.b64decode(message["Signature"], validate=True), to_sign.encode(),
                          padding.PKCS1v15(), algorithm)
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        logger.warning("[SNS] Signature verification failed: %s", exc)
        return False


def token_matches(presented: Optional[str], expected: str) -> bool:
    return bool(presented) and bool(expected) and hmac.compare_digest(presented or "", expected)
