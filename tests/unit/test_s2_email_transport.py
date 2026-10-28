"""S2: campaign / warmup sending goes through the EmailTransport adapter; the failure classes are unchanged."""
import asyncio
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from app.adapters import providers
from app.adapters.ses import SesEmailTransport
from app.services.email_sender_service import (
    AmbiguousEmailFailure, EmailSenderService, PermanentEmailFailure, PlatformEmailFailure, SenderEmailFailure,
    TransientEmailFailure,
)


def _client_error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, "SendRawEmail")


class FakeTransport:
    def __init__(self, error=None):
        self.error = error
        self.raw = []
        self.verified = []

    def send_raw(self, **kwargs):
        if self.error:
            raise self.error
        self.raw.append(kwargs)
        return {"MessageId": f"ses-{len(self.raw)}"}

    def send_simple(self, **kwargs):
        return {"MessageId": "simple"}

    def verify_email_identity(self, email):
        self.verified.append(email)

    def get_send_quota(self):
        return {"Max24HourSend": 200.0, "MaxSendRate": 1.0, "SentLast24Hours": 3.0}


def test_warmup_send_uses_the_transport():
    transport = FakeTransport()
    sender = EmailSenderService(transport=transport)
    result = asyncio.run(sender.send_plain_email("to@example.com", "Hello", "Body", "me@example.com", "Me"))
    assert result["success"] is True and result["ses_message_id"] == "ses-1"
    [call] = transport.raw
    assert call["Source"] == "Me <me@example.com>" and call["Destinations"] == ["to@example.com"]
    assert "Subject: Hello" in call["RawMessage"]["Data"]


def test_warmup_send_reports_ses_refusals():
    sender = EmailSenderService(transport=FakeTransport(_client_error("MessageRejected", "Email address is not verified")))
    result = asyncio.run(sender.send_plain_email("to@example.com", "Hello", "Body"))
    assert result == {"success": False, "error": "MessageRejected: Email address is not verified"}


@pytest.mark.parametrize("error, expected", [
    (_client_error("Throttling", "Maximum sending rate exceeded."), PlatformEmailFailure),
    (_client_error("MailFromDomainNotVerified", "not verified"), SenderEmailFailure),
    (_client_error("MessageRejected", "Illegal address"), PermanentEmailFailure),
    (_client_error("SomethingOdd", "try again"), TransientEmailFailure),
    (EndpointConnectionError(endpoint_url="https://ses"), PlatformEmailFailure),
    (ReadTimeoutError(endpoint_url="https://ses"), AmbiguousEmailFailure),
])
def test_campaign_send_failures_keep_their_classes(error, expected):
    sender = EmailSenderService(transport=FakeTransport(error))
    with pytest.raises(expected) as info:
        asyncio.run(sender._send_raw({"Source": "a"}, SimpleNamespace(message_id="m1"), "x@example.com"))
    if expected is PermanentEmailFailure:
        assert info.value.recipient_fault is True


def test_singleton_follows_the_provider_override():
    from app.services.email_sender_service import email_sender
    transport = FakeTransport()
    with providers.override(email_transport=transport):
        assert email_sender.transport is transport
        assert email_sender.verify_email("new@example.com") is True
        assert email_sender.get_send_quota() == {"max_24_hour_send": 200.0, "max_send_rate": 1.0,
                                                 "sent_last_24_hours": 3.0}
    assert transport.verified == ["new@example.com"]


def test_template_attachments_are_read_from_the_store():
    class Store:
        files = {"k_a.pdf": b"%PDF"}

        def exists(self, key):
            return key in self.files

        def get(self, key):
            return self.files[key]

    template = SimpleNamespace(attachments=[
        SimpleNamespace(attachment_id="1", filename="a.pdf", content_type="application/pdf", storage_path="k_a.pdf"),
        SimpleNamespace(attachment_id="2", filename="gone.pdf", content_type=None, storage_path="k_gone.pdf"),
    ])
    with providers.override(attachment_store=Store()):
        loaded = EmailSenderService(transport=FakeTransport())._load_template_attachments(template)
    assert loaded == [{"filename": "a.pdf", "content_type": "application/pdf", "data": b"%PDF"}]


def test_ses_transport_maps_onto_the_boto_client():
    calls = []

    class Client:
        def send_raw_email(self, **kw):
            calls.append(("raw", kw))
            return {"MessageId": "r"}

        def send_email(self, **kw):
            calls.append(("simple", kw))
            return {"MessageId": "s"}

        def verify_email_identity(self, **kw):
            calls.append(("verify", kw))

        def get_send_quota(self):
            return {"Max24HourSend": 1}

    t = SesEmailTransport(Client())
    assert t.send_raw(Source="a", RawMessage={"Data": "x"}) == {"MessageId": "r"}
    assert t.send_simple(Source="a") == {"MessageId": "s"}
    t.verify_email_identity("v@example.com")
    assert t.get_send_quota() == {"Max24HourSend": 1}
    assert calls == [("raw", {"Source": "a", "RawMessage": {"Data": "x"}}), ("simple", {"Source": "a"}),
                     ("verify", {"EmailAddress": "v@example.com"})]
