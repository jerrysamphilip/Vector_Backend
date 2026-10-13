"""OpenAI spend-cap arithmetic (app/services/llm_budget.py), without the database.

The reservation/settlement against platform_flags is covered by tests/test_llm_budget.py (functional);
here only the pure cost estimate is checked, with prices pinned by monkeypatch.
"""
import re

import pytest

from app.core.config import settings
from app.services import llm_budget as lb


@pytest.fixture
def mini_prices(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_PRICE_INPUT_PER_M", 0.15)
    monkeypatch.setattr(settings, "OPENAI_PRICE_OUTPUT_PER_M", 0.60)


def test_micro_dollars():
    assert lb._micro(0.20) == 200_000
    assert lb._micro(0) == 0
    assert lb._micro(0.0000004) == 0
    assert lb._micro(1.5) == 1_500_000


@pytest.mark.parametrize("messages,tokens", [
    (None, 20), ([], 20),
    ([{"role": "user", "content": "x" * 300}], 120),
    ([{"content": "abc"}, {"content": "de"}], 21),            # 5 chars // 3 + 20
    (["not a dict"], 20),                                     # non-dict messages count as empty
    ([{"content": None}], 20),
    ([{"content": ["a", "b"]}], 23),                          # str(['a', 'b']) is 10 chars
])
def test_prompt_token_estimate_is_pessimistic(messages, tokens):
    assert lb._prompt_tokens(messages) == tokens


def test_cost_uses_input_and_output_prices(mini_prices):
    assert lb._cost_micro(1_000_000, 0) == 150_000
    assert lb._cost_micro(0, 1_000_000) == 600_000
    assert lb._cost_micro(1000, 1500) == 1050
    assert lb._cost_micro(0, 0) == 0


def test_cost_follows_configured_prices(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_PRICE_INPUT_PER_M", 1.0)
    monkeypatch.setattr(settings, "OPENAI_PRICE_OUTPUT_PER_M", 2.0)
    assert lb._cost_micro(500_000, 250_000) == 1_000_000


def test_worst_case_reservation_for_an_empty_prompt(mini_prices):
    # 20 prompt tokens + the 1500-token output ceiling = $0.000903
    assert lb._cost_micro(lb._prompt_tokens([]), 1500) == 903


def test_twenty_cent_cap_allows_a_bounded_number_of_worst_case_calls(mini_prices):
    per_call = lb._cost_micro(lb._prompt_tokens([{"content": "x" * 3000}]), 1500)   # 1020 tokens in
    assert per_call == 1053
    assert lb._micro(0.20) // per_call == 189


def test_day_key_is_a_utc_date():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", lb._day())


def test_budget_exceeded_is_a_runtime_error():
    assert issubclass(lb.LLMBudgetExceeded, RuntimeError)
