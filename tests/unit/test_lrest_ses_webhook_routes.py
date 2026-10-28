"""SES/SNS webhook route (app/routers/ses_webhook_router.py) with real SNS signatures and a fake service.

Every refusal case of app/utils/sns_verify.py is driven through the HTTP route: 403 for a message that
is not genuine, 503 (SNS retries) when the signing certificate cannot be fetched; a genuine message
reaches the service exactly once with its type, body and SNS message id.
"""
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import BadRequestError, register_domain_error_handler
from app.routers import ses_webhook_router as mod
from app.utils import sns_verify as sv
from tests.lrest_sns_signing import TOPIC, make_certificate, now_stamp, serve_certificate, sign, signed_message

URL = "/webhooks/ses/notifications"
EVENT = {"notificationType": "Delivery", "mail": {"messageId": "ses-1"}, "delivery": {"timestamp": "t"}}


class FakeService:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or {"status": "processed"}
        self.error = error

    async def handle(self, message_type, body, sns_message_id):
        self.calls.append((message_type, body, sns_message_id))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(scope="module")
def keypair():
    return make_certificate()


@pytest.fixture
def setup(monkeypatch, keypair):
    key, pem = keypair
    monkeypatch.setattr(sv, "_configured_topics", lambda: "")
    monkeypatch.setattr(mod.settings, "AWS_SNS_TOPIC_ARN", TOPIC, raising=False)
    monkeypatch.setattr(mod.settings, "SES_WEBHOOK_TOKEN", "tok-123", raising=False)
    monkeypatch.setattr(type(mod.settings), "is_production", property(lambda self: False))
    calls = serve_certificate(monkeypatch, pem)
    service = FakeService()
    app = FastAPI()
    register_domain_error_handler(app)
    app.include_router(mod.router)
    app.dependency_overrides[mod.ses_event_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False), service, key, calls


@pytest.mark.parametrize("version", ["1", "2"])
def test_signed_notification_reaches_the_service(setup, version):
    client, service, key, calls = setup
    body = signed_message(key, EVENT, version)
    r = client.post(URL, json=body, headers={"x-amz-sns-message-type": "Notification",
                                             "x-amz-sns-message-id": "hdr-id"})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "processed"}
    assert service.calls == [("Notification", body, "hdr-id")]
    assert len(calls) == 1


def test_signed_type_wins_over_the_header(setup):
    client, service, key, _ = setup
    body = sign(key, {"Type": "UnsubscribeConfirmation", "MessageId": "m", "Token": "t", "TopicArn": TOPIC,
                      "Message": "bye", "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?x", "Timestamp": now_stamp()})
    r = client.post(URL, json=body, headers={"x-amz-sns-message-type": "Notification"})
    assert r.status_code == 200
    assert service.calls[0][0] == "UnsubscribeConfirmation"


@pytest.mark.parametrize("mutate", [
    lambda m: {**m, "Message": m["Message"] + " "},                                  # tampered
    lambda m: {**m, "TopicArn": "arn:aws:sns:us-east-1:1:other"},                    # wrong topic (also unsigned)
    lambda m: {**m, "SigningCertURL": "https://evil.example/cert.pem"},              # foreign certificate
    lambda m: {**m, "SigningCertURL": "https://u@sns.us-east-1.amazonaws.com/c.pem"},  # userinfo
    lambda m: {**m, "SigningCertURL": "https://sns.us-east-1.amazonaws.com:8443/c.pem"},  # port
    lambda m: {**m, "SignatureVersion": "9"},
])
def test_not_genuine_message_is_403(setup, mutate):
    client, service, key, _ = setup
    r = client.post(URL, json=mutate(signed_message(key, EVENT)))
    assert r.status_code == 403
    assert r.json() == {"detail": "Invalid SNS signature"}
    assert service.calls == []


def test_replayed_old_message_is_403(setup):
    client, service, key, _ = setup
    r = client.post(URL, json=signed_message(key, EVENT, stamp=now_stamp(-timedelta(hours=25))))
    assert r.status_code == 403 and service.calls == []


def test_signed_message_from_a_topic_outside_the_allow_list_is_403(setup, monkeypatch):
    client, service, key, _ = setup
    monkeypatch.setattr(mod.settings, "AWS_SNS_TOPIC_ARN", "", raising=False)
    monkeypatch.setattr(sv, "_configured_topics", lambda: "arn:aws:sns:us-east-1:1:only-this")
    r = client.post(URL, json=signed_message(key, EVENT))
    assert r.status_code == 403 and service.calls == []
    monkeypatch.setattr(sv, "_configured_topics", lambda: f"arn:aws:sns:us-east-1:1:only-this,{TOPIC}")
    assert client.post(URL, json=signed_message(key, EVENT)).status_code == 200


def test_expired_certificate_is_403(setup, monkeypatch):
    client, service, _, _ = setup
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    key, pem = make_certificate(not_before=now - timedelta(days=5), not_after=now - timedelta(days=1))
    serve_certificate(monkeypatch, pem)
    assert client.post(URL, json=signed_message(key, EVENT)).status_code == 403
    assert service.calls == []


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(503),
    lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
])
def test_certificate_download_failure_is_503_so_sns_retries(setup, monkeypatch, handler):
    client, service, key, _ = setup
    serve_certificate(monkeypatch, handler=handler)
    r = client.post(URL, json=signed_message(key, EVENT))
    assert r.status_code == 503
    assert r.json() == {"detail": "Signing certificate unavailable"}
    assert service.calls == []


def test_production_without_topic_is_403(setup, monkeypatch):
    client, service, key, _ = setup
    monkeypatch.setattr(mod.settings, "AWS_SNS_TOPIC_ARN", "", raising=False)
    monkeypatch.setattr(type(mod.settings), "is_production", property(lambda self: True))
    assert client.post(URL, json=signed_message(key, EVENT)).status_code == 403


def test_raw_event_needs_the_token(setup):
    client, service, _, _ = setup
    assert client.post(URL, json=EVENT).status_code == 403
    assert client.post(URL + "?token=wrong", json=EVENT).status_code == 403
    r = client.post(URL + "?token=tok-123", json=EVENT)
    assert r.status_code == 200
    r = client.post(URL, json=EVENT, headers={"x-webhook-token": "tok-123", "x-amz-sns-message-type": "Notification"})
    assert r.status_code == 200
    assert [c[0] for c in service.calls] == [None, "Notification"]


def test_raw_event_without_configured_token(setup, monkeypatch):
    client, service, _, _ = setup
    monkeypatch.setattr(mod.settings, "SES_WEBHOOK_TOKEN", "", raising=False)
    monkeypatch.setattr(mod, "is_deployed", lambda: True)
    assert client.post(URL, json=EVENT).json() == {"detail": "Webhook token not configured"}
    monkeypatch.setattr(mod, "is_deployed", lambda: False)
    assert client.post(URL, json=EVENT).status_code == 200


def test_service_errors(setup):
    client, service, _, _ = setup
    service.error = BadRequestError("SubscribeURL is not an SNS URL")
    r = client.post(URL + "?token=tok-123", json=EVENT)
    assert r.status_code == 400 and r.json() == {"detail": "SubscribeURL is not an SNS URL"}
    service.error = RuntimeError("Unknown column 'secret'")
    r = client.post(URL + "?token=tok-123", json=EVENT)
    assert r.status_code == 500 and "secret" not in r.text


def test_body_errors(setup):
    client, _, _, _ = setup
    assert client.post(URL, content=b"{nope", headers={"content-type": "application/json"}).status_code == 400
    big = b'{"a": "' + b"x" * (mod.MAX_WEBHOOK_BODY_BYTES + 1) + b'"}'
    assert client.post(URL, content=big, headers={"content-type": "application/json"}).status_code == 413
