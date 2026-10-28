"""Reply classification service (app/services/reply_classifier.py, S-09) with a stubbed LLM, a fake
budget and a fake unit of work: never a real OpenAI call, never a database."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.domain.inbox import MAX_CLASSIFY_ATTEMPTS, LabelSource, ReplyClassification, ReplyLabel
from app.models.email_message import EmailEvent
from app.services import llm_budget
from app.services import reply_classifier as rc


# ── llm_classify: budget and parsing ──────────────────────────
class FakeBudget:
    def __init__(self, exceeded=False):
        self.exceeded, self.charged, self.settled = exceeded, [], []

    def charge(self, kwargs):
        if self.exceeded:
            raise llm_budget.LLMBudgetExceeded("cap")
        self.charged.append(kwargs)
        return dict(kwargs), 123

    def settle(self, reserved, response=None):
        self.settled.append((reserved, response))


class FakeClient:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))], usage=None)


def test_llm_answer_is_parsed_and_spend_settled():
    budget, client = FakeBudget(), FakeClient('{"label": "positive", "confidence": 0.91}')
    result = rc.llm_classify("Re: hi", "Let's meet", client=client, budget=budget)
    assert result == ReplyClassification(ReplyLabel.POSITIVE, 0.91, LabelSource.LLM, "LLM")
    assert budget.charged and budget.charged[0]["max_tokens"] == rc.LLM_MAX_TOKENS
    assert len(budget.settled) == 1 and budget.settled[0][0] == 123 and budget.settled[0][1] is not None
    # The reply is passed as data inside the user message, the instructions in the system message
    messages = client.calls[0][0]
    assert messages[0]["role"] == "system" and "Let's meet" in messages[1]["content"]


def test_no_key_or_over_budget_means_no_llm():
    assert rc.llm_classify("s", "b", client=None, budget=FakeBudget()) is None  # tests have no key
    client = FakeClient('{"label": "positive"}')
    assert rc.llm_classify("s", "b", client=client, budget=FakeBudget(exceeded=True)) is None
    assert client.calls == []


def test_llm_error_releases_the_reservation():
    budget = FakeBudget()
    with pytest.raises(rc.ReplyLLMError):
        rc.llm_classify("s", "b", client=FakeClient(error=TimeoutError("slow")), budget=budget)
    assert budget.settled == [(123, None)]
    with pytest.raises(rc.ReplyLLMError):
        rc.llm_classify("s", "b", client=FakeClient("I think it is good"), budget=FakeBudget())


# ── classify_reply: order and fallbacks ───────────────────────
def test_plain_opt_out_never_reaches_the_llm():
    def boom(subject, body):
        raise AssertionError("LLM called")
    result = rc.classify_reply("Re: hi", "Unsubscribe", llm=boom)
    assert result.label is ReplyLabel.UNSUBSCRIBE and result.suppresses


def test_llm_label_is_used_and_rules_are_the_fallback():
    assert rc.classify_reply("Re: hi", "hmm", llm=lambda s, b: "negative").label is ReplyLabel.NEGATIVE
    fallback = rc.classify_reply("Re: hi", "Sounds good, let's talk", llm=lambda s, b: None)
    assert (fallback.label, fallback.source) == (ReplyLabel.POSITIVE, LabelSource.RULES)
    unknown = rc.classify_reply("Re: hi", "Sounds good, let's talk", llm=lambda s, b: "maybe?")
    assert unknown.source is LabelSource.RULES


def test_default_hook_without_key_uses_rules():
    result = rc.classify_reply("Re: hi", "Not interested")
    assert (result.label, result.source) == (ReplyLabel.NEGATIVE, LabelSource.RULES)


def test_llm_error_is_deferred_until_the_last_attempt():
    def failing(subject, body):
        raise rc.ReplyLLMError("down")
    with pytest.raises(rc.ClassificationDeferred):
        rc.classify_reply("Re: hi", "Can you send pricing?", llm=failing, attempt=1)
    last = rc.classify_reply("Re: hi", "Can you send pricing?", llm=failing, attempt=MAX_CLASSIFY_ATTEMPTS)
    assert (last.label, last.source) == (ReplyLabel.POSITIVE, LabelSource.RULES)


# ── ReplyClassificationService with a fake unit of work ───────
def _msg(message_id, direction="INBOUND", **kw):
    base = dict(message_id=message_id, direction=direction, subject="Re: hi", body_text="", conversation_id="c1",
                prospect_id="p1", sent_at=datetime(2026, 10, 1, 10, 0), reply_classified_at=None,
                reply_classify_attempts=0, reply_label=None, reply_confidence=None, reply_label_source=None,
                reply_needs_review=False)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeRepo:
    def __init__(self, messages, prospect=None):
        self.messages = {m.message_id: m for m in messages}
        self.events = []
        self.prospect = prospect
        self.session = object()

    def get_reply(self, message_id, lock=False):
        m = self.messages.get(message_id)
        return m if m is not None and m.direction == "INBOUND" else None

    def get_outbound(self, message_id):
        m = self.messages.get(message_id or "")
        return m if m is not None and m.direction == "OUTBOUND" else None

    def thread_outbound(self, reply):
        return next((m for m in self.messages.values() if m.direction == "OUTBOUND"), None)

    def get_prospect(self, prospect_id):
        return self.prospect

    def event_exists(self, message_id, event_type, reply):
        return any(e[0] == message_id and e[1] == event_type and (e[3] or {}).get("reply_message_id") == reply.message_id
                   for e in self.events)

    def add_event(self, message_id, event_type, at, metadata=None):
        self.events.append((message_id, event_type, at, metadata))


class FakeUow:
    def __init__(self, repo):
        self.replies, self.commits, self.active = repo, 0, False

    def __enter__(self):
        assert not self.active
        self.active = True
        return self

    def __exit__(self, *exc):
        self.active = False

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _service(messages, llm, prospect=None):
    repo = FakeRepo(messages, prospect)
    return rc.ReplyClassificationService(FakeUow(repo), llm=llm), repo


def test_positive_reply_is_labelled_once_with_one_event():
    reply = _msg("r1", body_text="Interesting")
    out = _msg("o1", direction="OUTBOUND")
    service, repo = _service([reply, out], llm=lambda s, b: ReplyClassification(ReplyLabel.POSITIVE, 0.9, LabelSource.LLM))
    assert service.classify_and_store("r1", "o1") is ReplyLabel.POSITIVE
    assert (reply.reply_label, reply.reply_confidence, reply.reply_label_source) == ("POSITIVE", 0.9, "LLM")
    assert reply.reply_classify_attempts == 1 and reply.reply_classified_at is not None
    assert [(e[0], e[1]) for e in repo.events] == [("o1", EmailEvent.EVENT_POSITIVE_REPLY)]
    assert repo.events[0][3]["reply_message_id"] == "r1"
    # Already labelled: nothing classified or written again
    assert service.classify_and_store("r1", "o1") is None
    assert len(repo.events) == 1


def test_llm_runs_with_no_unit_of_work_open():
    reply = _msg("r1", body_text="Hmm")
    holder = {}

    def llm(subject, body):
        assert not holder["uow"].active, "transaction open during the LLM call"
        return "neutral"
    service, repo = _service([reply], llm=llm)
    holder["uow"] = service._uow
    assert service.classify_and_store("r1") is ReplyLabel.NEUTRAL
    assert repo.events == []  # neutral: label only


def test_deferred_reply_counts_the_attempt_and_stays_unlabelled():
    reply = _msg("r1", body_text="Send me details")

    def failing(subject, body):
        raise rc.ReplyLLMError("boom")
    service, _ = _service([reply], llm=failing)
    assert service.classify_and_store("r1") is None
    assert reply.reply_label is None and reply.reply_classified_at is None
    assert reply.reply_classify_attempts == 1


def test_unsubscribe_suppresses_only_when_confident(monkeypatch):
    calls = []
    monkeypatch.setattr(rc.suppression, "active_suppression", lambda session, tenant, email: None)
    monkeypatch.setattr(rc.suppression, "suppress", lambda session, tenant, email, reason, kind, source: calls.append(
        (tenant, email, kind, source)))
    prospect = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="pat@example.com")

    sure = _msg("r1", body_text="Please remove me from your list")
    service, repo = _service([sure, _msg("o1", direction="OUTBOUND")], llm=lambda s, b: None, prospect=prospect)
    assert service.classify_and_store("r1") is ReplyLabel.UNSUBSCRIBE
    assert calls == [("t1", "pat@example.com", "UNSUBSCRIBE", "reply_unsubscribe:r1")]
    assert ("o1", EmailEvent.EVENT_UNSUBSCRIBE) in [(e[0], e[1]) for e in repo.events]
    assert sure.reply_needs_review is False

    calls.clear()
    doubtful = _msg("r2", body_text="maybe")
    service, _ = _service([doubtful], prospect=prospect,
                          llm=lambda s, b: ReplyClassification(ReplyLabel.UNSUBSCRIBE, 0.55, LabelSource.LLM))
    assert service.classify_and_store("r2") is ReplyLabel.UNSUBSCRIBE
    assert calls == [] and doubtful.reply_needs_review is True


def test_already_suppressed_contact_is_not_suppressed_again(monkeypatch):
    calls = []
    monkeypatch.setattr(rc.suppression, "active_suppression", lambda session, tenant, email: object())
    monkeypatch.setattr(rc.suppression, "suppress", lambda *a, **k: calls.append(a))
    prospect = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="pat@example.com")
    service, _ = _service([_msg("r1", body_text="Unsubscribe")], llm=lambda s, b: None, prospect=prospect)
    assert service.classify_and_store("r1") is ReplyLabel.UNSUBSCRIBE
    assert calls == []


def test_classify_pending_batches_one_unit_of_work_per_reply():
    replies = [_msg(f"r{i}", body_text="Sounds good, let's talk") for i in range(3)]
    repo = FakeRepo(replies)
    repo.pending_reply_ids = lambda since, max_attempts, limit: [m.message_id for m in replies][:limit]
    stats = rc.classify_pending(limit=2, llm=lambda s, b: None, uow_factory=lambda: FakeUow(repo))
    assert stats == {"pending": 2, "classified": 2, "failed": 0}
    assert [m.reply_label for m in replies] == ["POSITIVE", "POSITIVE", None]
