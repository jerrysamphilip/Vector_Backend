"""SNS signature verification (app/utils/sns_verify.py; team review C-04 / C-09), no network.

A self-signed RSA certificate stands in for AWS's signing certificate; messages are signed the SNS way
(SignatureVersion 1 = SHA1, 2 = SHA256) and the certificate is served from an in-memory transport.
"""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.utils import sns_verify as sv
from tests.lrest_sns_signing import (
    CERT_URL, TOPIC, make_certificate, notification, now_stamp, serve_certificate, sign, signed_message,
)

EVENT = {"notificationType": "Delivery", "mail": {"messageId": "ses-1"}, "delivery": {"timestamp": "t"}}


@pytest.fixture(scope="module")
def keypair():
    return make_certificate()


@pytest.fixture(autouse=True)
def _no_topic_limits(monkeypatch):
    monkeypatch.setattr(sv, "_configured_topics", lambda: "")
    sv._cert_cache.clear()
    yield
    sv._cert_cache.clear()


def verify(message, expected_topic=""):
    return asyncio.run(sv.verify_sns_signature(message, expected_topic))


# ── accepted ──────────────────────────────────────────────────────

@pytest.mark.parametrize("version", ["1", "2"])
def test_valid_notification_is_accepted(monkeypatch, keypair, version):
    key, pem = keypair
    calls = serve_certificate(monkeypatch, pem)
    assert verify(signed_message(key, EVENT, version), TOPIC) is True
    assert calls == [CERT_URL]


@pytest.mark.parametrize("kind", ["SubscriptionConfirmation", "UnsubscribeConfirmation"])
def test_valid_subscription_messages_are_accepted(monkeypatch, keypair, kind):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    message = sign(key, {"Type": kind, "MessageId": "m", "Token": "tok", "TopicArn": TOPIC, "Message": "x",
                         "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=X", "Timestamp": now_stamp()}, "2")
    assert verify(message) is True


def test_subject_is_part_of_the_signature(monkeypatch, keypair):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    message = sign(key, {**notification(EVENT), "Subject": "Amazon SES Email Event Notification"})
    assert verify(message) is True
    assert verify({**message, "Subject": "changed"}) is False


def test_certificate_is_cached_per_url(monkeypatch, keypair):
    key, pem = keypair
    calls = serve_certificate(monkeypatch, pem)
    assert verify(signed_message(key, EVENT)) and verify(signed_message(key, EVENT, "2"))
    assert calls == [CERT_URL]


# ── refused (403 at the webhook) ─────────────────────────────────

def test_tampered_message_is_refused(monkeypatch, keypair):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    message = signed_message(key, EVENT)
    assert verify({**message, "Message": message["Message"].replace("Delivery", "Bounce")}) is False


def test_signature_from_another_key_is_refused(monkeypatch, keypair):
    _, pem = keypair
    other_key, _ = make_certificate()
    serve_certificate(monkeypatch, pem)
    assert verify(signed_message(other_key, EVENT)) is False


@pytest.mark.parametrize("change", [
    {"Signature": "!!not base64!!"},
    {"Signature": ""},
    {"SignatureVersion": "3"},
    {"Type": "Unknown"},
    {"MessageId": None},
])
def test_malformed_signature_fields_are_refused(monkeypatch, keypair, change):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    assert verify({**signed_message(key, EVENT), **change}) is False


def test_message_older_than_24h_is_refused(monkeypatch, keypair):
    key, pem = keypair
    calls = serve_certificate(monkeypatch, pem)
    old = signed_message(key, EVENT, stamp=now_stamp(-timedelta(hours=24, minutes=1)))
    assert verify(old) is False
    assert calls == []  # refused before any download
    fresh = signed_message(key, EVENT, stamp=now_stamp(-timedelta(hours=23)))
    assert verify(fresh) is True


@pytest.mark.parametrize("stamp", [None, "", "yesterday", now_stamp(timedelta(hours=2))])
def test_missing_bad_or_future_timestamp_is_refused(monkeypatch, keypair, stamp):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    message = sign(key, {**notification(EVENT), "Timestamp": stamp or "x"})
    if stamp is None:
        message = {**message, "Timestamp": None}
    assert verify(message) is False


def test_timestamp_rules():
    now = datetime(2026, 10, 1, 12, 0, 0)
    assert sv.timestamp_fresh("2026-10-01T11:00:00.000Z", now)
    assert sv.timestamp_fresh("2026-10-01T11:00:00", now)  # naive = UTC
    assert sv.timestamp_fresh("2026-10-01T13:00:00+02:00", now)
    assert not sv.timestamp_fresh("2026-09-30T11:59:59Z", now)
    assert not sv.timestamp_fresh("2026-10-01T12:16:00Z", now)
    assert not sv.timestamp_fresh(12345, now)


def test_expired_or_not_yet_valid_certificate_is_refused(monkeypatch):
    now = datetime.now(timezone.utc)
    for window in ((now - timedelta(days=10), now - timedelta(days=1)), (now + timedelta(days=1), now + timedelta(days=9))):
        key, pem = make_certificate(not_before=window[0], not_after=window[1])
        serve_certificate(monkeypatch, pem)
        assert verify(signed_message(key, EVENT)) is False


def test_non_rsa_certificate_is_refused(monkeypatch):
    key, pem = make_certificate(ec_key=True)
    serve_certificate(monkeypatch, pem)
    message = notification(EVENT)
    message.update(SignatureVersion="2", SigningCertURL=CERT_URL,
                   Signature=base64.b64encode(b"x" * 64).decode())
    assert verify(message) is False


def test_garbage_certificate_is_refused(monkeypatch, keypair):
    key, _ = keypair
    serve_certificate(monkeypatch, b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n")
    assert verify(signed_message(key, EVENT)) is False


def test_oversized_certificate_is_refused(monkeypatch, keypair):
    key, pem = keypair
    serve_certificate(monkeypatch, pem + b"\n" * (sv.CERT_MAX_BYTES + 1))
    assert verify(signed_message(key, EVENT)) is False
    assert CERT_URL not in sv._cert_cache


@pytest.mark.parametrize("url", [
    "http://sns.us-east-1.amazonaws.com/cert.pem",
    "https://sns.us-east-1.amazonaws.com.evil.example/cert.pem",
    "https://user:pw@sns.us-east-1.amazonaws.com/cert.pem",
    "https://user@sns.us-east-1.amazonaws.com/cert.pem",
    "https://sns.us-east-1.amazonaws.com:8443/cert.pem",
    "https://sns.us-east-1.amazonaws.com:abc/cert.pem",
    "https://sns.us-east-1.amazonaws.com/cert.txt",
    "https://sns.us-east-1.amazonaws.com/cert.txt?x=.pem",
])
def test_certificate_url_must_be_plain_sns_https(monkeypatch, keypair, url):
    key, pem = keypair
    calls = serve_certificate(monkeypatch, pem)
    assert verify(signed_message(key, EVENT) | {"SigningCertURL": url}) is False
    assert calls == []


def test_is_sns_url_rules():
    assert sv.is_sns_url("https://sns.eu-west-1.amazonaws.com/x.pem")
    assert sv.is_sns_url("https://sns.cn-north-1.amazonaws.com.cn:443/x")
    assert not sv.is_sns_url("https://sns.eu-west-1.amazonaws.com:444/x")
    assert not sv.is_sns_url("https://a:b@sns.eu-west-1.amazonaws.com/x")
    assert not sv.is_sns_url("https://[::1/x")
    assert not sv.is_sns_url(None)


def test_topic_must_match_the_configured_topic(monkeypatch, keypair):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    assert verify(signed_message(key, EVENT, topic="arn:aws:sns:us-east-1:1:other"), TOPIC) is False


def test_topic_allow_list(monkeypatch, keypair):
    key, pem = keypair
    serve_certificate(monkeypatch, pem)
    monkeypatch.setattr(sv, "_configured_topics", lambda: f" {TOPIC} , arn:aws:sns:us-east-1:1:b ")
    assert sv.allowed_topic_arns() == {TOPIC, "arn:aws:sns:us-east-1:1:b"}
    assert verify(signed_message(key, EVENT)) is True
    assert verify(signed_message(key, EVENT, topic="arn:aws:sns:us-east-1:1:c")) is False
    monkeypatch.setattr(sv, "_configured_topics", lambda: [TOPIC])
    assert sv.allowed_topic_arns() == {TOPIC}


def test_topic_allow_list_setting(monkeypatch):
    monkeypatch.undo()  # the real _configured_topics
    monkeypatch.setattr(sv, "settings", SimpleNamespace(SNS_ALLOWED_TOPIC_ARNS="arn:a,,arn:b"))
    assert sv.allowed_topic_arns() == {"arn:a", "arn:b"}
    monkeypatch.setattr(sv, "settings", SimpleNamespace(SNS_ALLOWED_TOPIC_ARNS=""))
    assert sv.allowed_topic_arns() == frozenset()
    assert sv.topic_allowed("anything")


# ── certificate unavailable (503 at the webhook, so SNS retries) ─

@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(500),
    lambda request: httpx.Response(404),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
def test_download_failure_raises_certificate_unavailable(monkeypatch, keypair, handler):
    key, _ = keypair
    serve_certificate(monkeypatch, handler=handler)
    with pytest.raises(sv.SigningCertificateUnavailable):
        verify(signed_message(key, EVENT))
    assert len(sv._cert_cache) == 0


# ── cache ─────────────────────────────────────────────────────────

def test_cache_ttl_and_size_cap(monkeypatch, keypair):
    _, pem = keypair
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(pem)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sv.time, "monotonic", lambda: clock["t"])
    cache = sv._CertificateCache(ttl=60, max_entries=2)
    cache.put("a", cert)
    assert cache.get("a") is cert and "a" in cache
    clock["t"] += 61
    assert cache.get("a") is None and len(cache) == 0
    cache.put("a", cert)
    cache.put("b", cert)
    cache.get("a")  # a is now the most recent
    cache.put("c", cert)
    assert "b" not in cache and "a" in cache and "c" in cache
    assert cache.get("missing") is None


def test_token_matches():
    assert sv.token_matches("s3cret", "s3cret")
    assert not sv.token_matches("nope", "s3cret")
    assert not sv.token_matches(None, "s3cret")
    assert not sv.token_matches("x", "")
