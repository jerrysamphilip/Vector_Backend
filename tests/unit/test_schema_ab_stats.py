"""SubjectVariantService.stats: open / reply rates over delivered (sent - bounced), bounce over sent,
click over opened; the winner uses the same open rate. Fake unit of work, no database."""
from types import SimpleNamespace

from app.models.email_message import EmailEvent
from app.services import ab_testing
from app.services.subject_variant_service import SubjectVariantService


class FakeMessages:
    def __init__(self, sent, opened, clicked, replied, bounced):
        self._sent, self._replied, self._bounced = sent, replied, bounced
        self._unique = {EmailEvent.EVENT_OPEN: opened, EmailEvent.EVENT_CLICK: clicked}

    def variant_sent_counts(self, sid):
        return self._sent

    def variant_scheduled_counts(self, sid):
        return {"A": 1}

    def variant_unique_contacts(self, sid, event_type):
        return self._unique[event_type]

    def variant_replied(self, sid):
        return self._replied

    def variant_bounced(self, sid):
        return self._bounced

    def unassigned_sent(self, sid):
        return 0


class FakeContent:
    def variants(self, sid):
        return [SimpleNamespace(label=lb, subject=f"Subject {lb}", weight=1, is_winner=False) for lb in "BA"]


def _stats(monkeypatch, **counts):
    monkeypatch.setattr(ab_testing, "MIN_SENDS_PER_VARIANT", 30)
    uow = SimpleNamespace(content=FakeContent(), messages=FakeMessages(**counts))
    return SubjectVariantService(uow=uow).stats(SimpleNamespace(sequence_id="s1", step_number=1))


def test_schema_ab_stats_rates_over_delivered(monkeypatch):
    body = _stats(monkeypatch, sent={"A": 30, "B": 30}, opened={"A": 9, "B": 8}, clicked={"A": 3, "B": 0},
                  replied={"A": 3, "B": 6}, bounced={"B": 6})
    a, b = body["variants"]
    assert (a["label"], a["sent"], a["bounced"], a["delivered"]) == ("A", 30, 0, 30)
    assert (a["open_rate"], a["reply_rate"], a["click_rate"], a["bounce_rate"]) == (30.0, 10.0, 33.33, 0.0)
    assert (b["label"], b["sent"], b["bounced"], b["delivered"]) == ("B", 30, 6, 24)
    assert (b["open_rate"], b["reply_rate"], b["click_rate"], b["bounce_rate"]) == (33.33, 25.0, 0.0, 20.0)
    # the winner follows the displayed open rate: B (8/24), not A (9/30) as with sent as the denominator
    assert body["winner"]["status"] == ab_testing.WINNER_FOUND and body["winner"]["label"] == "B"


def test_schema_ab_stats_all_bounced_has_zero_rates(monkeypatch):
    body = _stats(monkeypatch, sent={"A": 2}, opened={}, clicked={}, replied={}, bounced={"A": 2})
    a, b = body["variants"]
    assert (a["delivered"], a["open_rate"], a["reply_rate"], a["bounce_rate"]) == (0, 0.0, 0.0, 100.0)
    assert (b["sent"], b["delivered"], b["open_rate"]) == (0, 0, 0.0)
    assert body["winner"]["status"] == ab_testing.WINNER_INSUFFICIENT
