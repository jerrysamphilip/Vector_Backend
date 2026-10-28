"""UAT defect 2 (agent tz): datetimes leave the API in UTC with a zone; SES outcomes keep SES's own time;
the bounce note is a system notice, not a reply."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.clock import iso_utc, to_utc_naive
from app.core.json_encoding import install_utc_datetime_encoder
from app.domain.inbox import SYSTEM_NOTICE_SENDER, is_system_notice
from app.domain.ses_events import event_time, parse_ses_timestamp as parse_iso_utc
from app.schemas.conversation_schema import ConversationListItem, MessageResponse
from app.schemas.types import UtcDatetime
from app.services import conversation_service as conv_svc
from app.services import ses_event_service as svc
from tests.unit.test_lrest_ses_event_services import FakeUow, _mail, _message, world  # noqa: F401

NAIVE = datetime(2026, 10, 26, 17, 54, 9, 654321)
BOUNCED_AT = datetime(2026, 10, 26, 17, 54, 5)
PROCESSED = datetime(2026, 10, 26, 17, 59, 0)


# ── clock helpers ────────────────────────────────────────────────────

def test_iso_utc_marks_naive_values_as_utc_and_converts_aware_ones():
    assert iso_utc(NAIVE) == "2026-10-26T17:54:09.654321Z"
    eastern = timezone(timedelta(hours=-4))
    assert iso_utc(datetime(2026, 10, 26, 13, 54, 9, tzinfo=eastern)) == "2026-10-26T17:54:09Z"
    assert iso_utc(None) is None


def test_parse_iso_utc_reads_ses_timestamps_as_naive_utc():
    assert parse_iso_utc("2026-10-26T17:54:05.000Z") == BOUNCED_AT
    assert parse_iso_utc("2026-10-26T13:54:05-04:00") == BOUNCED_AT
    assert parse_iso_utc("2026-10-26T17:54:05") == BOUNCED_AT
    assert parse_iso_utc("t1") is None and parse_iso_utc(None) is None and parse_iso_utc("") is None
    assert to_utc_naive(NAIVE) is NAIVE


# ── UtcDatetime ──────────────────────────────────────────────────────

class _Model(BaseModel):
    at: UtcDatetime
    maybe: Optional[UtcDatetime] = None


def test_utc_datetime_json_output_ends_in_z():
    m = _Model(at=NAIVE)
    assert json.loads(m.model_dump_json()) == {"at": "2026-10-26T17:54:09.654321Z", "maybe": None}
    assert m.model_dump(mode="json")["at"] == "2026-10-26T17:54:09.654321Z"
    assert m.model_dump()["at"] == NAIVE  # python mode keeps the datetime


def test_utc_datetime_request_parsing_is_unchanged():
    assert _Model(at="2026-10-26T17:54:09.654321").at == NAIVE
    aware = _Model(at="2026-10-26T17:54:09Z").at
    assert aware.tzinfo is not None and aware.utcoffset() == timedelta(0)


def test_utc_datetime_json_schema_is_a_plain_date_time():
    assert _Model.model_json_schema(mode="serialization")["properties"]["at"] == {
        "type": "string", "format": "date-time", "title": "At"}
    assert _Model.model_json_schema(mode="validation")["properties"]["at"] == {
        "type": "string", "format": "date-time", "title": "At"}


def test_dict_responses_write_datetimes_with_z():
    install_utc_datetime_encoder()
    assert jsonable_encoder({"at": NAIVE, "day": NAIVE.date()}) == {
        "at": "2026-10-26T17:54:09.654321Z", "day": "2026-10-26"}
    app = FastAPI()

    @app.get("/x")
    def _x():
        return {"at": NAIVE, "nested": [{"at": NAIVE}], "model": _Model(at=NAIVE)}

    body = TestClient(app).get("/x").json()
    assert body == {"at": "2026-10-26T17:54:09.654321Z", "nested": [{"at": "2026-10-26T17:54:09.654321Z"}],
                    "model": {"at": "2026-10-26T17:54:09.654321Z", "maybe": None}}


def test_conversation_responses_are_utc():
    item = ConversationListItem(id="c", prospect_id="p", prospect_name="n", prospect_email="e", inbox_email="i",
                                subject="s", last_message_snippet=None, last_message_at=NAIVE, is_unread=False,
                                status="OPEN")
    assert json.loads(item.model_dump_json())["last_message_at"] == "2026-10-26T17:54:09.654321Z"


# ── SES event time ────────────────────────────────────────────────────

def test_event_time_reads_the_kind_specific_timestamp():
    assert event_time({"bounce": {"timestamp": "2026-10-26T17:54:05.000Z"}}, "Bounce") == BOUNCED_AT
    assert event_time({"complaint": {"timestamp": "2026-10-26T17:54:05Z"}}, "Complaint") == BOUNCED_AT
    assert event_time({"bounce": {}}, "Bounce") is None
    assert event_time({}, "Nope") is None


class _Conversation(SimpleNamespace):
    pass


def _bounce(timestamp):
    return {"notificationType": "Bounce", "mail": _mail(), "bounce": {
        "bounceType": "Permanent", "bounceSubType": "General", "timestamp": timestamp,
        "bouncedRecipients": [{"emailAddress": "a@b.io", "diagnosticCode": "550 5.1.1 user unknown"}]}}


def test_hard_bounce_is_stamped_with_ses_bounce_time(world, monkeypatch):  # noqa: F811
    service, uow, _ = world
    monkeypatch.setattr(svc, "utcnow", lambda: PROCESSED)
    uow.events.messages["m1"] = _message(conversation_id="conv1", final_status_at=None)
    conv = _Conversation(is_unread=False, last_message_at=datetime(2026, 10, 26, 17, 54, 3))
    uow.events.conversations["conv1"] = conv
    service.process_event(_bounce("2026-10-26T17:54:05.000Z"))
    msg = uow.events.messages["m1"]
    assert msg.final_status_at == BOUNCED_AT
    assert set(uow.events.event_times) == {BOUNCED_AT}
    note = uow.events.added_messages[0]
    assert note["sent_at"] == BOUNCED_AT
    assert conv.last_message_at == BOUNCED_AT and conv.is_unread


def test_bounce_note_is_a_system_notice(world, monkeypatch):  # noqa: F811
    service, uow, _ = world
    monkeypatch.setattr(svc, "utcnow", lambda: PROCESSED)
    uow.events.messages["m1"] = _message(conversation_id="conv1", final_status_at=None)
    uow.events.conversations["conv1"] = _Conversation(is_unread=False, last_message_at=None)
    service.process_event(_bounce("2026-10-26T17:54:05.000Z"))
    note = uow.events.added_messages[0]
    assert note["direction"] == "INBOUND"
    assert note["from_email"] == SYSTEM_NOTICE_SENDER
    assert note["reply_label_source"] == "SYSTEM"
    assert note["reply_classified_at"] == BOUNCED_AT  # the reply classifier skips it
    assert is_system_notice(SimpleNamespace(**note))


def test_bounce_without_a_timestamp_falls_back_to_now(world, monkeypatch):  # noqa: F811
    service, uow, _ = world
    monkeypatch.setattr(svc, "utcnow", lambda: PROCESSED)
    uow.events.messages["m1"] = _message(conversation_id="conv1", final_status_at=None)
    later = datetime(2026, 10, 26, 18, 0)
    conv = _Conversation(is_unread=False, last_message_at=later)
    uow.events.conversations["conv1"] = conv
    service.process_event(_bounce(None))
    assert uow.events.messages["m1"].final_status_at == PROCESSED
    assert uow.events.added_messages[0]["sent_at"] == PROCESSED
    assert conv.last_message_at == later  # never moved back past a newer message


def test_complaint_is_stamped_with_ses_complaint_time(world, monkeypatch):  # noqa: F811
    service, uow, _ = world
    monkeypatch.setattr(svc, "utcnow", lambda: PROCESSED)
    uow.events.messages["m1"] = _message(final_status_at=None)
    service.process_event({"notificationType": "Complaint", "mail": _mail(), "complaint": {
        "complaintFeedbackType": "abuse", "timestamp": "2026-10-26T17:54:05.000Z",
        "complainedRecipients": [{"emailAddress": "a@b.io"}]}})
    assert uow.events.messages["m1"].final_status_at == BOUNCED_AT
    assert set(uow.events.event_times) == {BOUNCED_AT}


# ── System notices in the inbox and the contact timeline ─────────────────

def _row(**kw):
    base = dict(direction="INBOUND", from_email="vincent@x.io", reply_label_source=None,
                subject="Re: hello", body_text="Thanks")
    base.update(kw)
    return SimpleNamespace(**base)


def test_is_system_notice():
    assert not is_system_notice(_row())
    assert is_system_notice(_row(from_email=SYSTEM_NOTICE_SENDER))
    assert is_system_notice(_row(reply_label_source="SYSTEM"))
    # Notes written before the marker (from_email = the contact) are recognised by their fixed wording
    assert is_system_notice(_row(subject="Delivery Failed: hello", body_text="Email delivery failed (Permanent).\n"))
    assert not is_system_notice(_row(subject="Delivery Failed: hello", body_text="I got your mail"))
    assert not is_system_notice(_row(direction="OUTBOUND", from_email=SYSTEM_NOTICE_SENDER))


def test_thread_message_and_list_item_flag_the_notice():
    note = SimpleNamespace(
        message_id="n1", direction="INBOUND", subject="Delivery Failed: hi", body_text="Email delivery failed (x)",
        from_email=SYSTEM_NOTICE_SENDER, to_email="me@x.io", scheduled_at=None, sent_at=NAIVE, delivered_at=None,
        status="SENT", final_status=None, campaign_id=None, campaign_name=None, reply_label=None,
        reply_confidence=None, reply_needs_review=False, reply_label_source="SYSTEM")
    out = conv_svc.ConversationService._message(note, {}, [], {})
    assert isinstance(out, MessageResponse) and out.is_system is True
    row = SimpleNamespace(id="c", prospect_id="p", first_name="V", last_name="D", prospect_email="v@x.io",
                          inbox_email="me@x.io", subject="hi", status="OPEN", is_unread=True, latest_at=NAIVE)
    last = conv_svc.LastMessage("c", "n1", "INBOUND", "Email delivery failed", "SENT", None, None, True)
    item = conv_svc._list_item(row, last, None, None)
    assert item.last_message_is_system is True and item.last_message_direction == "INBOUND"


def test_contact_timeline_leaves_out_system_notices():
    from app.services import contact_query_service as cq

    reply = _row(message_id="r1", subject_variant=None, status="SENT", final_status=None, failure_reason=None,
                 to_email="me@x.io", campaign_id=None, conversation_id="c", sent_at=NAIVE, scheduled_at=None)
    notice = _row(message_id="n1", subject="Delivery Failed: hi", from_email=SYSTEM_NOTICE_SENDER,
                  reply_label_source="SYSTEM", body_text="Email delivery failed (x)", subject_variant=None,
                  status="SENT", final_status=None, failure_reason=None, to_email="me@x.io", campaign_id=None,
                  conversation_id="c", sent_at=NAIVE, scheduled_at=None)

    class Repo:
        def emails_of(self, prospect_id, limit):
            return [(reply, None), (notice, None)]

        def email_events_of(self, prospect_id, kinds, limit):
            return []

    items = cq._email_items(Repo(), SimpleNamespace(prospect_id="p"), 10)
    assert [i["email"]["message_id"] for i in items] == ["r1"]


@pytest.mark.parametrize("value", [NAIVE, NAIVE.replace(tzinfo=timezone.utc)])
def test_iso_utc_round_trips_through_parse(value):
    assert parse_iso_utc(iso_utc(value)) == NAIVE


def test_campaign_metrics_rates_use_delivered_as_denominator():
    from app.schemas.campaign_schema import CampaignMetrics

    m = CampaignMetrics(sent_count=10, bounced_count=2, opened_count=4, replied_count=2)
    assert (m.open_rate, m.reply_rate) == (50.0, 25.0)
    assert CampaignMetrics(sent_count=2, bounced_count=2, opened_count=0).open_rate == 0.0
