"""
SES event rules (BR-DF-04 / BR-DF-08 / B09 / B10): pure functions over the SES event JSON the webhook
receives. No I/O.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Statuses in which a sent email can receive its SES outcome. SENDING: SES accepted the mail and its
# event raced ahead of the scheduler's commit of SENT; the outcome is recorded either way.
OUTCOME_STATUSES = ("SENT", "SENDING")

# Where each SES event kind keeps its own timestamp (used to build the dedupe key)
EVENT_DETAIL_KEYS = {
    "Bounce": "bounce", "Complaint": "complaint", "Delivery": "delivery", "Open": "open",
    "Click": "click", "Reject": "reject", "DeliveryDelay": "deliveryDelay",
    "Rendering Failure": "failure", "Subscription": "subscription",
}

# Diagnostic-code substrings indicating the recipient's server rejected the message because of OUR
# sending domain (auth/reputation), not because the recipient address itself is bad.
SENDER_FAULT_DIAGNOSTIC_KEYWORDS = (
    "spf", "dkim", "dmarc", "not authorized", "unauthenticated",
    "blocked", "blacklist", "block list", "reputation",
    "5.7.1", "5.7.25", "5.7.26", "5.7.27",
)


def extract_tags(mail_info: dict) -> dict:
    """Tags of an SES mail object: dict of lists (standard SNS) or list of {name, value} (configuration sets)."""
    tags_raw = mail_info.get("tags", {})
    if isinstance(tags_raw, dict):
        return {k: v[0] if isinstance(v, list) and v else v for k, v in tags_raw.items()}
    if isinstance(tags_raw, list):
        return {tag.get("name"): tag.get("value") for tag in tags_raw if "name" in tag}
    return {}


def dedupe_key(message: dict, notification_type: Optional[str], sns_message_id: Optional[str]) -> Optional[str]:
    """
    Stable identity of one SES event (B09), so SNS retries/redeliveries are processed once:
    (SES messageId, event type, event timestamp[, link]) when SES gives a timestamp, else the
    SNS MessageId. Stored as event_metadata.dedupe_key on every event the webhook writes.
    """
    mail_id = (message.get("mail") or {}).get("messageId")
    detail = message.get(EVENT_DETAIL_KEYS.get(notification_type or "", ""), {}) or {}
    stamp = detail.get("timestamp") if isinstance(detail, dict) else None
    if mail_id and stamp:
        key = f"{mail_id}:{notification_type}:{stamp}"
        if notification_type == "Click" and detail.get("link"):
            key += f":{detail.get('link')}"
        return key[:512]
    if sns_message_id:
        return f"sns:{sns_message_id}"
    return None


def is_sender_attributable_bounce(bounce_type: Optional[str], diagnostic_code: Optional[str]) -> bool:
    """
    True when a bounce points at a problem with OUR sending side rather than a genuinely bad address:
    transient bounces (rate limiting, greylisting, temporary block), or permanent bounces whose
    diagnostic text names our domain's SPF/DKIM/DMARC/reputation as the rejection reason.
    """
    if (bounce_type or "").lower() == "transient":
        return True
    diagnostic = (diagnostic_code or "").lower()
    return any(keyword in diagnostic for keyword in SENDER_FAULT_DIAGNOSTIC_KEYWORDS)


def unwrap_ses_event(body: Any) -> Optional[dict]:
    """
    The SES event inside a webhook body, or None.

    The SNS subscription may have RawMessageDelivery enabled (the body IS the SES event) or not (the
    event is JSON-encoded in the envelope's "Message" field). Whichever shape arrived is detected;
    assuming the wrong one would silently drop every notification.
    """
    if isinstance(body, dict) and "Message" in body:
        try:
            message = json.loads(body.get("Message", "{}"))
        except (TypeError, json.JSONDecodeError):
            return None
        return message if isinstance(message, dict) else None
    if isinstance(body, dict) and ("notificationType" in body or "eventType" in body):
        return body
    return None


def notification_type_of(message: dict) -> Optional[str]:
    """SES's legacy "notificationType", else the event-publishing "eventType"."""
    return message.get("notificationType") or message.get("eventType")


def header_message_id(mail_info: dict) -> Optional[str]:
    """Our X-Message-Id header, the fallback for open/click events without our tag."""
    headers = {h["name"]: h["value"] for h in mail_info.get("headers", [])}
    return headers.get("X-Message-Id")
