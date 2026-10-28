"""SES/SNS webhook end to end against MySQL (lrest; team review C-04 / C-09).

A self-signed RSA certificate stands in for AWS's signing certificate (served from an in-memory httpx
transport, no network). A genuine, SNS-signed Notification (SignatureVersion 1 and 2) is accepted by the
real app and recorded on the email; every refused message leaves the database untouched; a certificate
that cannot be fetched answers 503 so SNS retries.
"""
import json
import uuid
from datetime import timedelta

import httpx
import pytest

from app.core.clock import utcnow
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import GlobalUnsubscribe
from app.routers import ses_webhook_router
from app.utils import sns_verify
from tests.conftest import RUN, SessionLocal
from tests.lrest_sns_signing import TOPIC, make_certificate, now_stamp, serve_certificate, sign, signed_message

WEBHOOK = "/webhooks/ses/notifications"


@pytest.fixture(scope="module")
def keypair():
    return make_certificate()


@pytest.fixture
def sns(monkeypatch, keypair):
    key, pem = keypair
    monkeypatch.setattr(ses_webhook_router.settings, "AWS_SNS_TOPIC_ARN", TOPIC, raising=False)
    monkeypatch.setattr(sns_verify, "_configured_topics", lambda: TOPIC)
    calls = serve_certificate(monkeypatch, pem)
    return key, calls


def _sent_message(client, h, label):
    r = client.post("/contacts", headers=h, json={"email": f"{label}.{uuid.uuid4().hex[:6]}@lrest{RUN}.example.com",
                                                  "first_name": label.title()})
    assert r.status_code == 201, r.text
    ses_id = f"0100-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        m = EmailMessage(prospect_id=r.json()["prospect_id"], direction="OUTBOUND", status="SENT",
                         to_email=r.json()["email"], from_email=f"Sender <sender@lrest{RUN}.example.com>",
                         ses_message_id=ses_id, sent_at=utcnow())
        db.add(m)
        db.commit()
        return m.message_id, ses_id, r.json()["email"]


def _delivery(ses_id, addr, stamp="2026-10-27T10:00:00.000Z"):
    return {"notificationType": "Delivery", "mail": {"messageId": ses_id, "source": "s@x.io", "tags": {}},
            "delivery": {"recipients": [addr], "timestamp": stamp, "smtpResponse": "250 OK"}}


def _delivered(mid):
    with SessionLocal() as db:
        m = db.get(EmailMessage, mid)
        n = db.query(EmailEvent).filter(EmailEvent.message_id == mid,
                                        EmailEvent.event_type == EmailEvent.EVENT_DELIVERED).count()
        return m.delivered_at is not None, n


@pytest.mark.parametrize("version", ["1", "2"])
def test_signed_notification_is_recorded_end_to_end(client, tenant_a, sns, version):
    key, calls = sns
    mid, ses_id, addr = _sent_message(client, tenant_a["headers"], f"sig{version}")
    body = signed_message(key, _delivery(ses_id, addr), version, message_id=f"sns-{uuid.uuid4()}")
    r = client.post(WEBHOOK, json=body, headers={"x-amz-sns-message-type": "Notification"})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "processed", "type": "delivery", "emails": [addr]}
    assert _delivered(mid) == (True, 1)
    # SNS redelivers the same message: recognised, recorded once
    r = client.post(WEBHOOK, json=body)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert _delivered(mid) == (True, 1)


def test_signed_bounce_suppresses_the_address(client, tenant_a, sns):
    key, _ = sns
    mid, ses_id, addr = _sent_message(client, tenant_a["headers"], "bounce")
    event = {"notificationType": "Bounce", "mail": {"messageId": ses_id, "source": "s@x.io"}, "bounce": {
        "bounceType": "Permanent", "bounceSubType": "General", "timestamp": "2026-10-27T10:00:01.000Z",
        "bouncedRecipients": [{"emailAddress": addr, "diagnosticCode": "550 5.1.1 user unknown"}]}}
    r = client.post(WEBHOOK, json=signed_message(key, event, "2"))
    assert r.status_code == 200 and r.json()["bounce_type"] == "Permanent"
    with SessionLocal() as db:
        assert db.get(EmailMessage, mid).status == "BOUNCED"
        entry = db.query(GlobalUnsubscribe).filter(GlobalUnsubscribe.tenant_id == tenant_a["tenant_id"],
                                                   GlobalUnsubscribe.email == addr.lower()).one()
        assert entry.reason.startswith("Hard bounce")


@pytest.mark.parametrize("case", ["tampered", "old", "topic", "port", "userinfo"])
def test_refused_messages_change_nothing(client, tenant_a, sns, case):
    key, calls = sns
    mid, ses_id, addr = _sent_message(client, tenant_a["headers"], f"ref-{case}")
    body = signed_message(key, _delivery(ses_id, addr))
    if case == "tampered":
        body["Message"] = json.dumps({**json.loads(body["Message"]), "notificationType": "Bounce"})
    elif case == "old":
        body = signed_message(key, _delivery(ses_id, addr), stamp=now_stamp(-timedelta(hours=25)))
    elif case == "topic":
        body = signed_message(key, _delivery(ses_id, addr), topic="arn:aws:sns:us-east-1:1:someone-else")
    elif case == "port":
        body = sign(key, {**body, "SigningCertURL": "https://sns.us-east-1.amazonaws.com:444/c.pem"})
    else:
        body = sign(key, {**body, "SigningCertURL": "https://me@sns.us-east-1.amazonaws.com/c.pem"})
    r = client.post(WEBHOOK, json=body)
    assert r.status_code == 403, r.text
    assert _delivered(mid) == (False, 0)


def test_certificate_unavailable_is_503_and_changes_nothing(client, tenant_a, sns, monkeypatch):
    key, _ = sns
    mid, ses_id, addr = _sent_message(client, tenant_a["headers"], "nocert")
    serve_certificate(monkeypatch, handler=lambda request: httpx.Response(503))
    r = client.post(WEBHOOK, json=signed_message(key, _delivery(ses_id, addr)))
    assert r.status_code == 503 and r.json() == {"detail": "Signing certificate unavailable"}
    assert _delivered(mid) == (False, 0)


def test_unsubscribe_confirmation_is_logged_only(client, sns):
    key, _ = sns
    body = sign(key, {"Type": "UnsubscribeConfirmation", "MessageId": "m-unsub", "Token": "tok", "TopicArn": TOPIC,
                      "Message": "You have chosen to deactivate subscription",
                      "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
                      "Timestamp": now_stamp()}, "2")
    r = client.post(WEBHOOK, json=body)
    assert r.status_code == 200 and r.json() == {"status": "ignored", "type": "UnsubscribeConfirmation"}
