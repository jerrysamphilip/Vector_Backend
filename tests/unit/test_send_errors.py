"""SES failure classification (item 14) and no SDK resends (F079, item 16)."""
import asyncio
from http.client import IncompleteRead
from types import SimpleNamespace

import pytest
from botocore.exceptions import (
    ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError, ResponseStreamingError,
)
from urllib3.exceptions import ProtocolError

from app.adapters import providers
from app.adapters import ses as ses_adapter
from app.services import email_sender_service as sender_mod
from app.services.email_sender_service import (
    AmbiguousEmailFailure, PermanentEmailFailure, PlatformEmailFailure, SenderEmailFailure,
    TransientEmailFailure, classify_botocore_error, classify_client_error, email_sender,
)
from app.services.send_errors import FailureKind, classify_ses_error


@pytest.mark.parametrize("code, message, kind", [
    ("AccountSendingPausedException", "Sending paused for this account", FailureKind.PLATFORM),
    ("ConfigurationSetDoesNotExist", "Configuration set <x> does not exist", FailureKind.PLATFORM),
    ("Throttling", "Maximum sending rate exceeded.", FailureKind.PLATFORM),
    ("Throttling", "Daily message quota exceeded.", FailureKind.PLATFORM),
    ("ServiceUnavailable", "Service unavailable", FailureKind.PLATFORM),
    ("InternalFailure", "", FailureKind.PLATFORM),
    ("MessageRejected", "Email address is not verified. The following identities failed the check",
     FailureKind.SENDER),
    ("MailFromDomainNotVerifiedException", "MAIL FROM domain not verified", FailureKind.SENDER),
    ("MessageRejected", "Address blacklisted.", FailureKind.RECIPIENT),
    ("InvalidParameterValue", "Missing final '@domain'", FailureKind.RECIPIENT),
    ("MessageRejected", "Email address is on the suppression list", FailureKind.RECIPIENT),
    ("InvalidParameterValue", "Duplicate header 'Subject'", FailureKind.PERMANENT),
    ("SomethingNew", "unexpected", FailureKind.TRANSIENT),
])
def test_send_classify_ses_error(code, message, kind):
    assert classify_ses_error(code, message) is kind


def test_send_client_error_maps_to_failure_classes():
    assert isinstance(classify_client_error("AccountSendingPausedException", "paused"), PlatformEmailFailure)
    assert isinstance(classify_client_error("MessageRejected", "Email address is not verified."),
                      SenderEmailFailure)
    recipient = classify_client_error("MessageRejected", "Address blacklisted.")
    assert isinstance(recipient, PermanentEmailFailure) and recipient.recipient_fault
    other = classify_client_error("InvalidParameterValue", "Duplicate header")
    assert isinstance(other, PermanentEmailFailure) and not other.recipient_fault
    assert type(classify_client_error("Weird", "x")) is TransientEmailFailure
    # Deferred failures are transient for callers that only know the old classes
    assert isinstance(classify_client_error("Throttling", "rate"), TransientEmailFailure)


@pytest.mark.parametrize("exc, expected", [
    (EndpointConnectionError(endpoint_url="https://ses"), PlatformEmailFailure),
    (ConnectTimeoutError(endpoint_url="https://ses"), PlatformEmailFailure),
    (ReadTimeoutError(endpoint_url="https://ses"), AmbiguousEmailFailure),
    (ResponseStreamingError(error="IncompleteRead"), AmbiguousEmailFailure),
    (IncompleteRead(b"partial"), AmbiguousEmailFailure),
    (ProtocolError("Connection broken"), AmbiguousEmailFailure),
])
def test_send_connection_errors_before_vs_after_the_request(exc, expected):
    assert isinstance(classify_botocore_error(exc), expected)


def test_send_ses_client_never_retries_itself():
    retries = ses_adapter.build_ses_client().meta.config.retries
    assert retries.get("total_max_attempts") == 1
    assert sender_mod.SES_CLIENT_CONFIG.retries["total_max_attempts"] == 1


class _BrokenTransport:
    def send_raw(self, **kwargs):
        raise ResponseStreamingError(error="IncompleteRead(0 bytes read)")


def test_send_raw_maps_a_truncated_response_to_outcome_unknown():
    msg = SimpleNamespace(message_id="m1")
    with providers.override(email_transport=_BrokenTransport()):
        with pytest.raises(AmbiguousEmailFailure):
            asyncio.run(email_sender._send_raw({}, msg, "a@b.example.com"))


def test_send_with_retry_never_retries_a_deferred_failure(monkeypatch):
    attempts = []

    async def failing(*args, **kwargs):
        attempts.append(1)
        raise PlatformEmailFailure("Throttling: Maximum sending rate exceeded", code="Throttling")

    async def no_sleep(_):
        raise AssertionError("must not wait to retry a platform failure in place")

    monkeypatch.setattr(email_sender, "send_email", failing)
    monkeypatch.setattr(sender_mod.asyncio, "sleep", no_sleep)
    with pytest.raises(PlatformEmailFailure):
        asyncio.run(email_sender.send_with_retry(SimpleNamespace(message_id="m"), None, None, max_retries=3))
    assert len(attempts) == 1
