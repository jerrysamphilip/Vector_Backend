"""SesEventService (app/services/ses_event_service.py) and TrackingService with a dict-backed fake
repository and a counting unit of work; the not-yet-migrated collaborators (metrics, suppression, send
safety, automation) are recorded instead of run."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError
from app.models.email_message import EmailEvent
from app.services import ses_event_service as svc
from app.services import tracking_service as tsvc
from app.services.ses_event_service import SesEventService
from app.services.tracking_service import TrackingService, url_in_email


class FakeEvents:
    def __init__(self):
        self.messages = {}
        self.by_provider = {}
        self.events = []
        self.added_messages = []
        self.prospects = {}
        self.conversations = {}
        self.tenant_by_email = {}

    def prospect_tenant(self, prospect_id):
        p = self.prospects.get(prospect_id)
        return p.tenant_id if p else None

    def tenants_with_email(self, email):
        return self.tenant_by_email.get(email.strip().lower(), [])

    def message_id_for_provider_id(self, ses_id):
        return self.by_provider.get(ses_id)

    def has_event_with_key(self, message_id, key):
        return any(e[0] == message_id and e[2].get("dedupe_key") == key for e in self.events)

    def message(self, message_id):
        return self.messages.get(message_id)

    def conversation(self, conversation_id):
        return self.conversations.get(conversation_id)

    def prospect(self, prospect_id):
        return self.prospects.get(prospect_id)

    def add_event(self, message_id, event_type, metadata):
        self.events.append((message_id, event_type, metadata))

    def add_message(self, **fields):
        self.added_messages.append(fields)


class FakeUow:
    def __init__(self):
        self.events = FakeEvents()
        self.session = object()
        self.entered = self.commits = self.rollbacks = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _message(**kw):
    base = dict(message_id="m1", campaign_id="c1", prospect_id="p1", status="SENT", delivered_at=None,
                conversation_id=None, inbox_id="i1", subject="Hello", from_email="me@x.io", final_status=None,
                failure_reason=None, last_error_code=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def world(monkeypatch):
    log = {"suppress": [], "risk": [], "metric": [], "stop": [], "automation": []}
    monkeypatch.setattr(svc, "suppress", lambda db, t, e, reason, kind="UNSUBSCRIBE", source=None:
                        log["suppress"].append((t, e, kind, source)))
    monkeypatch.setattr(svc, "on_risk_event", lambda db, c, d, trigger, tenant_id=None: log["risk"].append((c, d, trigger, tenant_id)))
    monkeypatch.setattr(svc, "tenant_for_message", lambda db, m: "t-msg" if m else None)
    monkeypatch.setattr(svc, "tenant_for_sender", lambda db, e: "t-sender")
    monkeypatch.setattr(svc, "stop_pending_steps", lambda db, c, p, why: log["stop"].append((c, p, why)))
    monkeypatch.setattr(svc, "record_ses_event", lambda kind: None)

    class Metrics:
        def __init__(self, db):
            pass

        def process_event(self, campaign_id, event_type):
            log["metric"].append((campaign_id, event_type))
    monkeypatch.setattr(svc, "MetricsService", Metrics)

    class Automation:
        def process_event(self, **kw):
            log["automation"].append(kw["event_type"])
            if kw["campaign_id"] == "boom":
                raise RuntimeError("rule failed")
    monkeypatch.setattr(svc, "automation_service", Automation())
    uow = FakeUow()
    uow.events.messages["m1"] = _message()
    uow.events.prospects["p1"] = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="a@b.io")
    return SesEventService(uow), uow, log


def _mail(**kw):
    return {"messageId": "ses-1", "source": "Me <me@x.io>", "tags": {"message_id": ["m1"]}, **kw}


def test_hard_bounce(world):
    service, uow, log = world
    uow.events.messages["m1"].conversation_id = "conv1"
    uow.events.conversations["conv1"] = SimpleNamespace(is_unread=False, last_message_at=None)
    out = service.process_event({"notificationType": "Bounce", "mail": _mail(), "bounce": {
        "bounceType": "Permanent", "bounceSubType": "General", "timestamp": "t1",
        "bouncedRecipients": [{"emailAddress": "a@b.io", "diagnosticCode": "550 5.7.26 dmarc"}]}})
    assert out == {"status": "processed", "type": "bounce", "bounce_type": "Permanent", "emails": ["a@b.io"]}
    m = uow.events.messages["m1"]
    assert m.status == "BOUNCED" and m.last_error_code == "BOUNCE_Permanent"
    kinds = [e[1] for e in uow.events.events]
    assert kinds == [EmailEvent.EVENT_BOUNCE, EmailEvent.EVENT_SENDER_BOUNCE]
    assert uow.events.events[0][2]["dedupe_key"] == "ses-1:Bounce:t1"
    assert uow.events.added_messages[0]["direction"] == "INBOUND"
    assert uow.events.conversations["conv1"].is_unread is True
    assert log["suppress"] == [("t1", "a@b.io", "HARD_BOUNCE", None)]
    assert log["stop"] == [("c1", "p1", "BOUNCED")]
    assert log["risk"] == [("c1", "x.io", "hard bounce", "t-msg")]
    assert log["automation"] and uow.commits == 1


def test_soft_bounce_records_only_a_sender_bounce(world):
    service, uow, log = world
    out = service.process_event({"notificationType": "Bounce", "mail": _mail(), "bounce": {
        "bounceType": "Transient", "bouncedRecipients": [{"emailAddress": "a@b.io"}]}})
    assert out["bounce_type"] == "Transient"
    assert [e[1] for e in uow.events.events] == [EmailEvent.EVENT_SENDER_BOUNCE]
    assert uow.events.messages["m1"].status == "SENT"
    assert log["suppress"] == [] and log["risk"] == [] and log["automation"] == []


def test_hard_bounce_of_unknown_message_suppresses_every_workspace_with_the_address(world):
    service, uow, log = world
    uow.events.tenant_by_email["x@y.io"] = ["t1", "t2"]
    service.process_event({"notificationType": "Bounce", "mail": {"messageId": "unknown"}, "bounce": {
        "bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": "X@y.io"}]}})
    assert [s[0] for s in log["suppress"]] == ["t1", "t2"]
    assert log["risk"] == [(None, None, "hard bounce", "t-sender")]


def test_duplicate_event_is_ignored(world):
    service, uow, _ = world
    event = {"notificationType": "Delivery", "mail": _mail(), "delivery": {"timestamp": "t2", "recipients": ["a@b.io"]}}
    assert service.process_event(json.loads(json.dumps(event)))["status"] == "processed"
    assert service.process_event(json.loads(json.dumps(event))) == {"status": "duplicate", "type": "Delivery"}


def test_delivery_sets_delivered_at_once(world):
    service, uow, _ = world
    service.process_event({"notificationType": "Delivery", "mail": _mail(), "delivery": {"smtpResponse": "250"}})
    m = uow.events.messages["m1"]
    assert m.delivered_at is not None and m.final_status == "DELIVERED"
    service.process_event({"notificationType": "Delivery", "mail": _mail(), "delivery": {}})
    assert len(uow.events.events) == 1


def test_complaint(world):
    service, uow, log = world
    out = service.process_event({"notificationType": "Complaint", "mail": _mail(), "complaint": {
        "complaintFeedbackType": "abuse", "complainedRecipients": [{"emailAddress": "a@b.io"}]}})
    assert out == {"status": "processed", "type": "complaint", "emails": ["a@b.io"]}
    assert uow.events.messages["m1"].status == "COMPLAINED"
    assert log["suppress"] == [("t1", "a@b.io", "COMPLAINT", None)]
    assert log["risk"][0][2] == "spam complaint"


@pytest.mark.parametrize("kind,key,detail,expected_event", [
    ("Open", "open", {"userAgent": "UA"}, EmailEvent.EVENT_OPEN),
    ("Click", "click", {"link": "https://x"}, EmailEvent.EVENT_CLICK),
])
def test_open_and_click(world, kind, key, detail, expected_event):
    service, uow, log = world
    out = service.process_event({"eventType": kind, "mail": _mail(), key: detail})
    assert out == {"status": "processed", "type": kind.lower(), "message_id": "m1"}
    assert uow.events.events[0][1] == expected_event and log["automation"]


def test_open_falls_back_to_header_and_reports_missing(world):
    service, uow, _ = world
    mail = {"headers": [{"name": "X-Message-Id", "value": "m1"}]}
    assert service.process_event({"eventType": "Open", "mail": mail, "open": {}})["status"] == "processed"
    assert service.process_event({"eventType": "Click", "mail": {}, "click": {}})["reason"] == "message_not_found"
    assert service.process_event({"eventType": "Open", "mail": {}, "open": {}})["reason"] == "message_not_found"


def test_automation_failure_does_not_break_the_event(world):
    service, uow, log = world
    uow.events.messages["m1"].campaign_id = "boom"
    assert service.process_event({"eventType": "Open", "mail": _mail(), "open": {}})["status"] == "processed"


def test_reject_delay_rendering_and_subscription(world):
    service, uow, log = world
    assert service.process_event({"eventType": "Reject", "mail": _mail(), "reject": {"reason": "virus"}})["type"] == "reject"
    assert uow.events.messages["m1"].last_error_code == "REJECT_VIRUS"
    assert service.process_event({"eventType": "DeliveryDelay", "mail": _mail(), "deliveryDelay": {}})["type"] == "delivery_delay"
    out = service.process_event({"eventType": "Rendering Failure", "mail": _mail(), "failure": {"errorMessage": "bad"}})
    assert out["error"] == "bad" and uow.events.messages["m1"].status == "FAILED"
    assert service.process_event({"eventType": "Subscription", "mail": _mail(), "subscription": {}})["type"] == "subscription"
    assert log["suppress"][-1] == ("t1", "a@b.io", "UNSUBSCRIBE", "list_unsubscribe")
    for kind, key in (("Reject", "reject"), ("DeliveryDelay", "deliveryDelay"), ("Rendering Failure", "failure"),
                      ("Subscription", "subscription")):
        assert service.process_event({"eventType": kind, "mail": {}, key: {}})["status"] == "ignored"


def test_unknown_type_and_message_ids(world):
    service, uow, _ = world
    assert service.process_event({"eventType": "Send", "mail": {}}) == {"status": "ignored", "type": "Send"}
    uow.events.by_provider["ses-9"] = "m1"
    assert SesEventService._resolve_message_id(uow, {"messageId": "ses-9"}) == "m1"
    assert SesEventService._resolve_message_id(uow, {"messageId": "nope"}) is None
    assert SesEventService._resolve_message_id(uow, {}) is None


def test_handle_dispatches_by_message_type(world, monkeypatch):
    service, uow, _ = world
    calls = []

    async def confirm(url):
        calls.append(url)
        return {"status": "confirmed"}
    monkeypatch.setattr(svc, "confirm_subscription", confirm)
    run = asyncio.run
    assert run(service.handle("SubscriptionConfirmation", {"SubscribeURL": "u"}, None)) == {"status": "confirmed"}
    assert calls == ["u"]
    assert run(service.handle("UnsubscribeConfirmation", {"TopicArn": "arn"}, None))["status"] == "ignored"
    assert run(service.handle("Notification", {"Message": "not json"}, None)) == {"status": "ok"}
    wrapped = {"Type": "Notification", "MessageId": "sns-1", "Message": json.dumps({"eventType": "Send", "mail": {}})}
    assert run(service.handle("Notification", wrapped, None))["type"] == "Send"


def test_confirm_subscription(monkeypatch):
    with pytest.raises(BadRequestError):
        asyncio.run(svc.confirm_subscription("https://evil.example/confirm"))

    class Client:
        status = 200

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            return SimpleNamespace(status_code=Client.status)
    monkeypatch.setattr(svc.httpx, "AsyncClient", Client)
    url = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription"
    assert asyncio.run(svc.confirm_subscription(url))["status"] == "confirmed"
    Client.status = 500
    assert asyncio.run(svc.confirm_subscription(url)) == {"status": "error", "message": "Confirmation failed: 500"}


# ── tracking ───────────────────────────────────────────────────────

@pytest.fixture
def tracking(monkeypatch):
    uow = FakeUow()
    uow.events.messages["m1"] = SimpleNamespace(message_id="m1", prospect_id="p1", body_html="<a href='https://ok.io/x'>",
                                                body_text=None)
    uow.events.prospects["p1"] = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="a@b.io")
    log = []
    monkeypatch.setattr(tsvc, "suppress", lambda db, t, e, reason, kind, source: log.append((t, e, reason, source)))
    monkeypatch.setattr(tsvc, "verify_tracking_url", lambda mid, url, sig: sig == "good")
    return TrackingService(uow), uow, log


def test_tracking_open(tracking):
    service, uow, _ = tracking
    service.record_open("m1")
    service.record_open("nope")
    assert uow.events.events == [("m1", EmailEvent.EVENT_OPEN, {"source": "tracking_pixel"})] and uow.commits == 1


def test_tracking_open_and_click_swallow_errors(tracking):
    service, uow, _ = tracking

    def boom(_):
        raise RuntimeError("db down")
    uow.events.message = boom
    service.record_open("m1")
    assert service.record_click("m1", "https://x", "https://x", "good") is True  # signed: still redirected
    assert service.record_click("m1", "https://x", "https://x", None) is False


def test_tracking_click_redirect_rules(tracking):
    service, uow, _ = tracking
    assert service.record_click("m1", "https://ok.io/x", "https://ok.io/x", None) is True
    assert service.record_click("m1", "https://evil.io", "https://evil.io", None) is False
    assert service.record_click("m1", "https://evil.io", "https://evil.io", "good") is True
    assert service.record_click("nope", "https://ok.io/x", "https://ok.io/x", None) is False
    assert uow.events.events[0][2] == {"url": "https://ok.io/x", "signed": False, "source": "click_tracking"}


def test_tracking_unsubscribe(tracking):
    service, uow, log = tracking
    assert service.message_exists("m1") and not service.message_exists("nope")
    assert service.unsubscribe("m1", one_click=True) is True
    assert log == [("t1", "a@b.io", "One-click unsubscribe (List-Unsubscribe)", "email_link:m1")]
    assert service.unsubscribe("m1", one_click=False) and log[-1][2] == "User clicked unsubscribe link"
    assert service.unsubscribe("nope", one_click=False) is False
    uow.events.messages["m2"] = SimpleNamespace(message_id="m2", prospect_id=None)
    assert service.unsubscribe("m2", one_click=False) is True and len(log) == 2


def test_url_in_email():
    msg = SimpleNamespace(body_html=None, body_text="see https%3A%2F%2Fa.io%2Fp")
    assert url_in_email(msg, "https://a.io/p")
    assert not url_in_email(msg, "https://b.io")
    assert not url_in_email(None, "https://a.io/p")
    assert not url_in_email(msg, "")
