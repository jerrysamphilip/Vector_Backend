"""SES event rules (app/domain/ses_events.py) and the mentions constants: pure values, no fakes."""
import json

import pytest

from app.domain import mentions
from app.domain.ses_events import (
    OUTCOME_STATUSES, dedupe_key, extract_tags, header_message_id, is_sender_attributable_bounce,
    notification_type_of, unwrap_ses_event,
)


def test_outcome_statuses_and_mention_limits():
    assert set(OUTCOME_STATUSES) == {"SENT", "SENDING"}
    assert mentions.MAX_MENTIONS == 20
    assert "AGENT" in mentions.MENTIONABLE_ROLES and "PLATFORM_ADMIN" not in mentions.MENTIONABLE_ROLES


@pytest.mark.parametrize("raw,expected", [
    ({"tags": {"message_id": ["m1"], "campaign": ["c"], "empty": []}}, {"message_id": "m1", "campaign": "c", "empty": []}),
    ({"tags": {"plain": "v"}}, {"plain": "v"}),
    ({"tags": [{"name": "message_id", "value": "m2"}, {"value": "no name"}]}, {"message_id": "m2"}),
    ({"tags": "garbage"}, {}),
    ({}, {}),
])
def test_extract_tags_both_formats(raw, expected):
    assert extract_tags(raw) == expected


def test_dedupe_key_uses_ses_timestamp_and_click_link():
    bounce = {"mail": {"messageId": "ses-1"}, "bounce": {"timestamp": "2026-01-01T00:00:00Z"}}
    assert dedupe_key(bounce, "Bounce", "sns-1") == "ses-1:Bounce:2026-01-01T00:00:00Z"
    click = {"mail": {"messageId": "ses-1"}, "click": {"timestamp": "t", "link": "https://x"}}
    assert dedupe_key(click, "Click", None) == "ses-1:Click:t:https://x"
    click_no_link = {"mail": {"messageId": "ses-1"}, "click": {"timestamp": "t"}}
    assert dedupe_key(click_no_link, "Click", None) == "ses-1:Click:t"
    long = {"mail": {"messageId": "x" * 600}, "open": {"timestamp": "t"}}
    assert len(dedupe_key(long, "Open", None)) == 512


def test_dedupe_key_falls_back_to_sns_id_or_none():
    assert dedupe_key({"mail": {"messageId": "ses-1"}}, "Delivery", "sns-9") == "sns:sns-9"
    assert dedupe_key({"mail": None, "delivery": "not-a-dict"}, "Delivery", None) is None
    assert dedupe_key({}, None, None) is None


@pytest.mark.parametrize("bounce_type,diagnostic,expected", [
    ("Transient", "", True),
    ("transient", None, True),
    ("Permanent", "550 5.7.26 DMARC policy", True),
    ("Permanent", "550 5.1.1 user unknown", False),
    (None, None, False),
])
def test_sender_attributable_bounce(bounce_type, diagnostic, expected):
    assert is_sender_attributable_bounce(bounce_type, diagnostic) is expected


def test_unwrap_wrapped_raw_and_invalid_bodies():
    event = {"notificationType": "Delivery", "mail": {}}
    assert unwrap_ses_event({"Type": "Notification", "Message": json.dumps(event)}) == event
    assert unwrap_ses_event(event) is event
    assert unwrap_ses_event({"eventType": "Open"}) == {"eventType": "Open"}
    assert unwrap_ses_event({"Message": "not json"}) is None
    assert unwrap_ses_event({"Message": None}) is None
    assert unwrap_ses_event({"Message": "[1, 2]"}) is None
    assert unwrap_ses_event({"other": 1}) is None
    assert unwrap_ses_event(["list"]) is None


def test_notification_type_and_header_id():
    assert notification_type_of({"notificationType": "Bounce", "eventType": "X"}) == "Bounce"
    assert notification_type_of({"eventType": "Open"}) == "Open"
    assert notification_type_of({}) is None
    assert header_message_id({"headers": [{"name": "X-Message-Id", "value": "m9"}]}) == "m9"
    assert header_message_id({}) is None
