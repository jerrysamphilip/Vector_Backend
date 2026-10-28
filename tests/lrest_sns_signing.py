"""Test helpers (lrest): a self-signed signing certificate and SNS-style message signatures.

SNS signs the canonical "string to sign" (Key\\nValue\\n for the type's fields in order) with RSA PKCS#1
v1.5: SignatureVersion 1 = SHA1, 2 = SHA256. ``signed_message`` builds such a message with this test
key; ``serve_certificate`` makes app.utils.sns_verify download the certificate from an in-memory httpx
transport (no network).
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from app.utils import sns_verify

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-lrest.pem"
TOPIC = "arn:aws:sns:us-east-1:000000000000:lrest-ses-events"


def make_certificate(*, key=None, not_before: Optional[datetime] = None, not_after: Optional[datetime] = None,
                     ec_key: bool = False):
    """(private key, PEM bytes) of a self-signed certificate; validity defaults to now +/- one day."""
    if key is None:
        key = ec.generate_private_key(ec.SECP256R1()) if ec_key else rsa.generate_private_key(
            public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before or now - timedelta(days=1))
            .not_valid_after(not_after or now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM)


def sign(key, message: dict, version: str = "1") -> dict:
    """``message`` with SignatureVersion / SigningCertURL / Signature set the way SNS sets them."""
    message = {**message, "SignatureVersion": version}
    message.setdefault("SigningCertURL", CERT_URL)
    to_sign = sns_verify.string_to_sign(message)
    assert to_sign is not None
    algorithm = hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303 - SNS v1 is SHA1
    signature = key.sign(to_sign.encode(), padding.PKCS1v15(), algorithm)
    message["Signature"] = base64.b64encode(signature).decode()
    return message


def now_stamp(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def notification(ses_event: dict, *, topic: str = TOPIC, stamp: Optional[str] = None,
                 message_id: str = "sns-msg-1") -> dict:
    return {"Type": "Notification", "MessageId": message_id, "TopicArn": topic,
            "Message": json.dumps(ses_event), "Timestamp": stamp or now_stamp()}


def signed_message(key, ses_event: dict, version: str = "1", **kw) -> dict:
    return sign(key, notification(ses_event, **kw), version)


def serve_certificate(monkeypatch, pem: bytes = b"", *, handler: Optional[Callable] = None) -> list:
    """Route sns_verify's certificate download to an in-memory transport; returns the requested URLs."""
    calls: list = []

    def default(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=pem)

    transport = httpx.MockTransport(handler or default)

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(sns_verify.httpx, "AsyncClient", client)
    sns_verify._cert_cache.clear()
    return calls
