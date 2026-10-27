"""Sending-pipeline alert rules and collectors (S-11), mailbox capacity (F-20) and the email activity
timeline (S-10): pure rules plus the collectors over a fake repository; no database."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain import inbox_sending as rules
from app.models.email_message import EmailEvent
from app.services import alert_center_service, sending_alerts
from app.services.conversation_service import build_activity

NOW = datetime(2026, 10, 27, 12, 0, 0)


@pytest.mark.parametrize("failed,sent,expected", [
    (4, 0, None),                       # too few failures to mean anything
    (5, 200, None),                     # 2.4 %
    (5, 95, rules.SEVERITY_WARNING),    # 5 %
    (10, 40, rules.SEVERITY_WARNING),   # 20 %
    (25, 75, rules.SEVERITY_CRITICAL),  # 25 %
    (0, 0, None),
])
def test_send_failure_severity(failed, sent, expected):
    assert rules.send_failure_severity(failed, sent) == expected


def test_delivery_reports_missing():
    assert not rules.delivery_reports_missing(9, 9)
    assert not rules.delivery_reports_missing(100, 19)
    assert rules.delivery_reports_missing(100, 20)


def test_ses_outage_severity():
    assert rules.ses_outage_severity(None, 0, NOW) is None
    assert rules.ses_outage_severity(NOW + timedelta(minutes=2), 0, NOW) == rules.SEVERITY_CRITICAL
    assert rules.ses_outage_severity(None, 3, NOW) == rules.SEVERITY_CRITICAL
    assert rules.ses_outage_severity(NOW - timedelta(minutes=10), 0, NOW) == rules.SEVERITY_WARNING
    assert rules.ses_outage_severity(NOW - timedelta(minutes=31), 0, NOW) is None


def test_daily_capacity():
    assert rules.daily_capacity(500, 60) == 500
    assert rules.daily_capacity(500, 600) == 144
    assert rules.daily_capacity(500, 0) == 500
    assert rules.daily_capacity(None, 60) == 0
    assert rules.remaining_today(144, 150) == 0 and rules.remaining_today(144, 44) == 100


def test_all_sending_types_have_preferences():
    for alert_type in rules.SENDING_ALERT_TYPES:
        assert alert_type in alert_center_service.ALL_ALERT_TYPES
        assert alert_type in alert_center_service.DEFAULT_PREFS
    assert rules.ALERT_INBOX_LIMIT_REACHED in alert_center_service.INBOX_ALERT_TYPES


class FakeAlertRepo:
    def __init__(self, **values):
        self.v = values

    def campaigns_without_active_mailbox(self, tenant_id):
        return self.v.get("no_mailbox", [])

    def sent_since_by_inbox(self, ids, since):
        return {i: n for i, n in self.v.get("sent", {}).items() if i in ids}

    def pending_by_inbox(self, ids):
        return {i: n for i, n in self.v.get("pending", {}).items() if i in ids}

    def send_outcomes(self, tenant_id, since):
        return self.v.get("outcomes", (0, 0, None))

    def delivery_gap(self, tenant_id, start, end):
        return self.v.get("gap", (0, 0))

    def stuck_sending(self, tenant_id, cutoff):
        return self.v.get("stuck", 0)

    def platform_deferrals(self, tenant_id, codes, since):
        assert "AccountSendingPausedException" in codes
        return self.v.get("deferred", 0)


def _inbox(inbox_id, status="ACTIVE", limit=10, delay=60):
    return SimpleNamespace(inbox_id=inbox_id, status=status, current_daily_limit=limit, delay_between_emails=delay,
                           email_address=f"{inbox_id}@example.com", last_sent_at=None)


def test_collectors():
    repo = FakeAlertRepo(
        no_mailbox=[SimpleNamespace(campaign_id="c1", campaign_name="Q4", updated_at=None, pending=7)],
        sent={"full": 10, "room": 2}, pending={"full": 3, "room": 9},
        outcomes=(6, 10, "SMTP said no " * 30), gap=(20, 10), stuck=2, deferred=0,
    )
    no_mailbox = sending_alerts._no_active_mailbox(repo, "t1", NOW)
    assert no_mailbox[0]["alert_type"] == "NO_ACTIVE_MAILBOX" and "7 email(s)" in no_mailbox[0]["message"]
    limit = sending_alerts._inbox_limit(repo, [_inbox("full"), _inbox("room"), _inbox("off", status="PAUSED")], NOW)
    assert [a["scope_id"] for a in limit] == ["full"] and limit[0]["scope_type"] == "INBOX"
    failure = sending_alerts._failure_rate(repo, "t1", NOW)[0]
    assert failure["severity"] == "CRITICAL" and len(failure["message"]) < 300
    assert sending_alerts._delivery_gap(repo, "t1", NOW)[0]["alert_type"] == "DELIVERY_EVENTS_MISSING"
    assert sending_alerts._stuck_sending(repo, "t1", NOW)[0]["severity"] == "WARNING"
    assert sending_alerts._ses_unavailable(repo, "t1", NOW, None) == []
    ses = sending_alerts._ses_unavailable(repo, "t1", NOW, NOW + timedelta(minutes=3))
    assert ses[0]["severity"] == "CRITICAL" and ses[0]["alert_type"] == "SES_UNAVAILABLE"


def test_quiet_pipeline_has_no_alerts():
    repo = FakeAlertRepo(outcomes=(1, 100, None), gap=(30, 1))
    assert sending_alerts._failure_rate(repo, "t1", NOW) == []
    assert sending_alerts._delivery_gap(repo, "t1", NOW) == []
    assert sending_alerts._stuck_sending(repo, "t1", NOW) == []
    assert sending_alerts._inbox_limit(repo, [_inbox("a")], NOW) == []


def test_breaker_state_is_read_only_and_shared(monkeypatch):
    written = {}
    monkeypatch.setattr(sending_alerts.platform_flags, "set_flag", lambda name, value: written.__setitem__(name, value))
    monkeypatch.setattr(sending_alerts.send_breaker.platform_breaker, "retry_in", lambda: 0.0)
    assert sending_alerts.record_breaker_state(NOW) is None and written == {}
    monkeypatch.setattr(sending_alerts.send_breaker.platform_breaker, "retry_in", lambda: 120.0)
    assert sending_alerts.record_breaker_state(NOW) == NOW + timedelta(seconds=120)
    value = written[sending_alerts.SES_BREAKER_FLAG]
    assert sending_alerts.breaker_open_until(lambda name: value) == NOW + timedelta(seconds=120)
    assert sending_alerts.breaker_open_until(lambda name: "garbage") is None
    assert sending_alerts.breaker_open_until(lambda name: None) is None


def test_activity_timeline():
    sent_at = datetime(2026, 10, 1, 9, 0)
    reply = SimpleNamespace(message_id="r1", reply_label="POSITIVE")
    message = SimpleNamespace(sent_at=sent_at, delivered_at=sent_at + timedelta(minutes=1), final_status="DELIVERED",
                              final_status_at=sent_at + timedelta(minutes=1))
    events = [
        (EmailEvent.EVENT_OPEN, sent_at + timedelta(hours=1), None),
        (EmailEvent.EVENT_CLICK, sent_at + timedelta(hours=2), None),
        (EmailEvent.EVENT_REPLY, sent_at + timedelta(hours=3), None),
        (EmailEvent.EVENT_POSITIVE_REPLY, sent_at + timedelta(hours=3), None),  # label, not a step
    ]
    steps = build_activity(message, events, {sent_at + timedelta(hours=3): reply})
    assert [s.type for s in steps] == ["SENT", "DELIVERED", "OPENED", "CLICKED", "REPLIED"]
    assert (steps[-1].reply_label, steps[-1].reply_message_id) == ("POSITIVE", "r1")

    bounced = SimpleNamespace(sent_at=sent_at, delivered_at=None, final_status="BOUNCED", final_status_at=sent_at)
    assert [s.type for s in build_activity(bounced, [], {})] == ["SENT", "BOUNCED"]
    queued = SimpleNamespace(sent_at=None, delivered_at=None, final_status=None, final_status_at=None)
    assert build_activity(queued, [], {}) == []
