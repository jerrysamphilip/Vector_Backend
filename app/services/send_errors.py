"""
Who is at fault when SES refuses a campaign send (item 14 of the sending review).

    PLATFORM   the sending platform itself cannot send right now: the SES account is paused, the
               configuration set is missing, SES throttles or is unavailable, or the connection
               failed before the request left. Nothing is wrong with the message, the mailbox or
               the contact: the message is re-queued without spending its retry budget, and a run
               of these trips the process-wide circuit breaker (app/services/send_breaker.py).
    SENDER     the sending identity is refused (address / MAIL FROM domain not verified). The
               message is re-queued (another mailbox of the campaign may send it); repeated
               rejections for one mailbox pause that mailbox.
    RECIPIENT  the recipient address is refused (invalid, blacklisted, on the suppression list):
               permanent, and the address is suppressed (F062, unchanged).
    PERMANENT  any other refusal that will fail the same way again (malformed message).
    TRANSIENT  anything else: retried with the message's own retry budget, as before (B06).

Pure functions only: no database, no network, so the rules are unit-tested on their own.
"""
from __future__ import annotations

import re
from enum import Enum


class FailureKind(str, Enum):
    PLATFORM = "PLATFORM"
    SENDER = "SENDER"
    RECIPIENT = "RECIPIENT"
    PERMANENT = "PERMANENT"
    TRANSIENT = "TRANSIENT"


# The account / region cannot send at the moment, whatever the message
PLATFORM_CODES = frozenset({
    "AccountSendingPausedException",
    "ConfigurationSetDoesNotExist",
    "ConfigurationSetDoesNotExistException",
    "ConfigurationSetSendingPausedException",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "LimitExceededException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "InternalFailure",
    "InternalError",
    "InternalServiceError",
    "RequestTimeout",
    "RequestTimeoutException",
    "RequestExpired",
})

# The sending identity (address or MAIL FROM domain) is not usable
SENDER_CODES = frozenset({
    "MailFromDomainNotVerified",
    "MailFromDomainNotVerifiedException",
})

# Refused for good; MessageRejected / InvalidParameterValue are split further by their text
PERMANENT_CODES = frozenset({
    "MessageRejected",
    "InvalidParameterValue",
    "ValidationError",
})

_RECIPIENT_FAULT = re.compile(
    r"blacklist|illegal address|invalid (recipient|address|email)|missing final '@domain'"
    r"|address (is )?(invalid|malformed)|local address contains|domain contains|suppression list",
    re.IGNORECASE,
)
_SENDER_FAULT = re.compile(
    r"not verified|identit(y|ies) failed|mail from domain|sending authori[sz]ation|not authori[sz]ed to send",
    re.IGNORECASE,
)
_PLATFORM_TEXT = re.compile(r"sending paused|maximum sending rate|daily message quota", re.IGNORECASE)


def classify_ses_error(code: str, message: str) -> FailureKind:
    """Map an SES ClientError (code + message) to who is at fault."""
    text = message or ""
    if code in PLATFORM_CODES or (code in PERMANENT_CODES and _PLATFORM_TEXT.search(text)):
        return FailureKind.PLATFORM
    if code in SENDER_CODES:
        return FailureKind.SENDER
    if code in PERMANENT_CODES:
        if _SENDER_FAULT.search(text):
            return FailureKind.SENDER
        if _RECIPIENT_FAULT.search(text):
            return FailureKind.RECIPIENT
        return FailureKind.PERMANENT
    return FailureKind.TRANSIENT
