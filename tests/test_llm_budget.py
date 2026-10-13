"""The OpenAI spend cap: refuses past the daily dollar limit, reserves and settles cost, clamps tokens."""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import llm_budget
from app.services.llm_budget import LLMBudgetExceeded, charge, settle, spent_today_usd


@pytest.fixture(autouse=True)
def _fresh_day(monkeypatch):
    monkeypatch.setattr(llm_budget, "_day", lambda: "2001-01-01")
    with SessionLocal() as db:
        db.execute(text("DELETE FROM platform_flags WHERE name LIKE '%2001-01-01'"))
        db.commit()


def test_default_cap_is_twenty_cents():
    assert settings.OPENAI_MAX_USD_PER_DAY == pytest.approx(0.20)


def test_clamps_output_tokens(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 300)
    kw, _ = charge({"max_tokens": 5000, "messages": [{"role": "user", "content": "hi"}]})
    assert kw["max_tokens"] == 300
    kw, _ = charge({"messages": []})
    assert kw["max_tokens"] == 300


def test_refuses_when_the_dollar_cap_would_be_exceeded(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_MAX_USD_PER_DAY", 0.001)
    monkeypatch.setattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 1000)   # ~ $0.0006 each (+ prompt)
    msgs = [{"role": "user", "content": "x" * 300}]
    charge({"messages": msgs})
    with pytest.raises(LLMBudgetExceeded):
        charge({"messages": msgs})
        charge({"messages": msgs})


def test_settle_replaces_the_reservation_with_actual_usage(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_MAX_USD_PER_DAY", 0.20)
    _, reserved = charge({"max_tokens": 1000, "messages": [{"role": "user", "content": "hello"}]})
    assert spent_today_usd() > 0
    settle(reserved, SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10)))
    assert spent_today_usd() < 0.0001


def test_zero_cap_blocks_everything(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_MAX_USD_PER_DAY", 0)
    with pytest.raises(LLMBudgetExceeded):
        charge({"messages": [{"role": "user", "content": "hi"}]})


def test_failed_call_releases_its_reservation():
    _, reserved = charge({"max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]})
    settle(reserved, None)
    assert spent_today_usd() == 0
