"""Stop flags (item 20), conditional SENT (15a), deferral (14), no platform fallback (12): fakes only."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError
from app.services import send_breaker
from app.services.email_scheduler_service import EmailSchedulerService, record_deferred_failure
from app.services.email_sender_service import PlatformEmailFailure, SenderEmailFailure
from app.services.send_guards import active_inboxes, require_active_inbox
from app.services.sequence_stop import stop_reason, stops_sequence


def _step(reply=True, bounce=True):
    return SimpleNamespace(stop_on_reply=reply, stop_on_bounce=bounce)


@pytest.mark.parametrize("step, status, stops", [
    (_step(), "REPLIED", True),
    (_step(reply=False), "REPLIED", False),
    (_step(), "BOUNCED", True),
    (_step(bounce=False), "BOUNCED", False),
    (_step(reply=False), "BOUNCED", True),
    (_step(reply=False, bounce=False), "UNSUBSCRIBED", True),
    (_step(reply=False, bounce=False), "PAUSED", True),
    (_step(), "ACTIVE", False),
    (_step(), "OPENED", False),
    (None, "REPLIED", True),  # unknown step: safe default
    (_step(reply=None), "REPLIED", True),  # NULL flag = default True
])
def test_send_step_stop_flags(step, status, stops):
    assert stops_sequence(step, status) is stops


def test_send_stop_reason_text():
    assert stop_reason("REPLIED") == "Stopped: contact replied"


# ── Conditional SENT ─────────────────────────────────────────

class _UpdateQuery:
    def __init__(self, log, results):
        self.log, self.results, self.filters = log, results, None

    def filter(self, *conds):
        self.filters = [str(c) for c in conds]
        return self

    def update(self, values, synchronize_session=None):
        self.log.append((self.filters, dict(values)))
        return self.results.pop(0)


class _Db:
    def __init__(self, results):
        self.log, self.results, self.refreshed = [], list(results), []

    def query(self, *a):
        return _UpdateQuery(self.log, self.results)

    def refresh(self, obj):
        self.refreshed.append(obj)


def _mark(db):
    msg = SimpleNamespace(message_id="m1")
    ok = EmailSchedulerService.mark_sent(db, msg, sent_at=datetime(2026, 1, 1), from_email="s@x.example.com",
                                         provider_message_id="<p@x>", ses_message_id="ses-1")
    return ok, msg


def test_send_sent_is_a_conditional_update_on_sending():
    db = _Db([1])
    ok, msg = _mark(db)
    assert ok and len(db.log) == 1 and db.refreshed == [msg]
    filters, values = db.log[0]
    assert any("status" in f for f in filters) and values["status"] == "SENT"
    assert values["ses_message_id"] == "ses-1" and values["sent_at"] == datetime(2026, 1, 1)


def test_send_sent_keeps_a_status_the_webhook_set_mid_send():
    db = _Db([0, 1])
    ok, msg = _mark(db)
    assert not ok and len(db.log) == 2
    _, values = db.log[1]
    assert "status" not in values and values["sent_at"] and values["provider_message_id"] == "<p@x>"
    assert any("sent_at IS NULL" in f for f in db.log[1][0])
    assert db.refreshed == [msg]


# ── Deferral (platform / sender failures) ────────────────────

def _msg(**kw):
    base = dict(message_id="m1", inbox_id="i1", deferred_count=0, retry_count=0, max_retries=3,
                status="SENDING", send_key="k", next_retry_at=None, last_error_code=None,
                failure_reason=None, final_status=None, final_status_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def fresh_breaker(monkeypatch):
    monkeypatch.setattr(send_breaker, "platform_breaker", send_breaker.CircuitBreaker())
    monkeypatch.setattr(send_breaker, "sender_rejections", send_breaker.RejectionTracker())


def test_send_platform_failure_requeues_without_spending_retries(fresh_breaker):
    s = EmailSchedulerService()
    msg = _msg()
    assert s._defer(msg, PlatformEmailFailure("paused", code="AccountSendingPausedException"), None) == "deferred"
    assert msg.status == "QUEUED" and msg.retry_count == 0 and msg.deferred_count == 1
    assert msg.send_key is None and msg.next_retry_at is not None and msg.final_status is None
    assert msg.last_error_code == "AccountSendingPausedException"


def test_send_deferral_is_capped(fresh_breaker):
    s = EmailSchedulerService()
    msg = _msg(deferred_count=send_breaker.MAX_DEFERRED_ATTEMPTS - 1)
    assert s._defer(msg, PlatformEmailFailure("Throttling", code="Throttling"), None) == "failed"
    assert msg.status == "FAILED" and msg.final_status == "FAILED" and "platform" in msg.failure_reason


def test_send_platform_failures_trip_the_breaker(fresh_breaker):
    for _ in range(send_breaker.PLATFORM_FAILURE_THRESHOLD - 1):
        record_deferred_failure(None, "PLATFORM", "i1", "Throttling")
    assert not send_breaker.platform_breaker.is_open()
    record_deferred_failure(None, "PLATFORM", "i1", "Throttling")
    assert send_breaker.platform_breaker.is_open()


def test_send_sender_rejections_pause_after_threshold(fresh_breaker, monkeypatch):
    paused = []
    monkeypatch.setattr("app.services.email_scheduler_service.pause_inbox_for_sender_rejections",
                        lambda db, inbox_id, error: paused.append(inbox_id) or True)
    s = EmailSchedulerService()
    for _ in range(send_breaker.SENDER_REJECTION_THRESHOLD):
        s._defer(_msg(), SenderEmailFailure("MessageRejected: not verified", code="MessageRejected"), None)
    assert paused == ["i1"]
    assert not send_breaker.platform_breaker.is_open()  # a sender problem is not a platform outage


# ── No platform-address fallback ─────────────────────────────

def _inbox(email, status="ACTIVE"):
    return SimpleNamespace(inbox_id=email, email_address=email, status=status)


def test_send_active_inboxes_and_requirement():
    campaign = SimpleNamespace(inboxes=[_inbox("b@x.example.com"), _inbox("a@x.example.com"),
                                        _inbox("c@x.example.com", "PAUSED")])
    assert [i.email_address for i in active_inboxes(campaign)] == ["a@x.example.com", "b@x.example.com"]
    require_active_inbox(campaign, "launch")
    with pytest.raises(BadRequestError) as err:
        require_active_inbox(SimpleNamespace(inboxes=[_inbox("c@x.example.com", "PAUSED")]), "resume")
    assert err.value.status_code == 400 and "no active sending inbox" in err.value.message


class _ListQuery:
    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        return []


def test_send_resolve_sender_has_no_platform_fallback():
    msg = SimpleNamespace(message_id="m1", inbox_id=None, from_email=None, campaign_id="c", prospect_id="p")
    db = SimpleNamespace(query=lambda *a: _ListQuery())
    campaign = SimpleNamespace(inboxes=[_inbox("x@x.example.com", "PAUSED")])
    assert EmailSchedulerService()._resolve_sender_for_message(msg, campaign, db) is None
    assert msg.from_email is None and msg.inbox_id is None


def test_send_hold_without_inbox_keeps_the_email_queued():
    msg = _msg()
    assert EmailSchedulerService._hold_without_inbox(msg) == "skipped"
    assert msg.status == "QUEUED" and msg.send_key is None and msg.last_error_code == "NO_ACTIVE_INBOX"


# ── Stuck-SENDING sweep: a webhook outcome is proof of acceptance ──

@pytest.mark.parametrize("fields, accepted", [
    ({"final_status": "DELIVERED"}, True),
    ({"final_status": "BOUNCED"}, True),
    ({"final_status": "COMPLAINED"}, True),
    ({"delivered_at": datetime(2026, 1, 1)}, True),
    ({"ses_message_id": "ses-1"}, True),
    ({}, False),
])
def test_send_stuck_sweep_evidence(fields, accepted):
    msg = SimpleNamespace(**{**dict(message_id="m", ses_message_id=None, provider_message_id=None,
                                    delivered_at=None, final_status=None), **fields})
    assert EmailSchedulerService._provider_accepted(msg, set()) is accepted
