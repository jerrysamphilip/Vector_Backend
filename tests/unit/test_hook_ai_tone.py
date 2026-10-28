"""Regenerating a follow-up uses that step's tone, not the first email's (21). The OpenAI client is a stub."""
import json
from types import SimpleNamespace

import pytest

from app.adapters import providers
from app.prompts import get_step_tone_block
from app.services import ai_email_service as svc
from app.services import llm_budget
from app.services.sender_identity import SENDER_COMPANY_TOKEN, current_company_name


def test_step_email_type():
    assert svc.step_email_type(1) == "intro"
    assert svc.step_email_type(3) == "followup_2"
    assert svc.step_email_type(None) == "intro"
    assert svc.step_email_type(0) == "intro"


def test_step_one_is_the_intro_tone():
    assert svc.step_tone_block(1) == get_step_tone_block(1)
    assert svc.step_tone_block(None, ["ignored"]) == get_step_tone_block(1)


def test_follow_up_tone_and_previous_subjects():
    block = svc.step_tone_block(3, ["First touch", "  ", "Second touch"])
    assert block.startswith(get_step_tone_block(3))
    assert get_step_tone_block(3) != get_step_tone_block(1)
    assert "follow-up email 3" in block
    assert '"First touch"; "Second touch"' in block


class _FakeChat:
    """A ChatClient (app/adapters/openai_chat.py) that records what it is asked."""

    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        content = json.dumps({"subject": "Following up", "body": "Hi {{first_name}},\n\nMore detail."})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(total_tokens=12))


@pytest.fixture
def budget(monkeypatch):
    """Every generation call is still reserved and settled against the daily LLM budget."""
    ledger = []

    def charge(kwargs):
        ledger.append(("charge", kwargs.get("max_tokens")))
        return kwargs, 7

    monkeypatch.setattr(llm_budget, "charge", charge)
    monkeypatch.setattr(llm_budget, "settle", lambda reserved, response=None: ledger.append(("settle", reserved)))
    return ledger


@pytest.fixture
def fake_openai(monkeypatch, budget):
    chat = _FakeChat()
    monkeypatch.setattr(svc, "retry_with_backoff", lambda fn, **kw: fn())
    with providers.override(generation_chat_client=chat):
        yield chat


def _blueprint():
    return SimpleNamespace(tone_rules={"style": "professional"}, openers=[], value_angles=[],
                           persona_type="OTHER")


def _user_prompt(calls):
    """The prompt as sent; the sender-company token is filled in by the branded client, so the tone
    blocks are compared un-branded."""
    content = next(m["content"] for m in calls[-1]["messages"] if m["role"] == "user")
    return content.replace(current_company_name(), SENDER_COMPANY_TOKEN)


def test_regenerated_follow_up_prompt_uses_its_step_tone(fake_openai):
    out = svc.generate_email_with_llm({"designation": "CTO", "industry": "software"}, _blueprint(),
                                      product_name="Widget", product_description="We build widgets",
                                      step_number=2, previous_subjects=["Widgets for your team"])
    assert out and out["subject"] == "Following up"
    prompt = _user_prompt(fake_openai.calls)
    assert get_step_tone_block(2) in prompt
    assert get_step_tone_block(1) not in prompt
    assert "Widgets for your team" in prompt


def test_default_is_still_the_intro_tone(fake_openai):
    svc.generate_email_with_llm({"designation": "CTO", "industry": "software"}, _blueprint(),
                                product_name="Widget", product_description="We build widgets")
    assert get_step_tone_block(1) in _user_prompt(fake_openai.calls)


def test_generation_is_budgeted_and_uses_the_configured_model(fake_openai, budget):
    svc.generate_email_with_llm({"designation": "CTO", "industry": "software"}, _blueprint(),
                                product_name="Widget", product_description="We build widgets")
    call = fake_openai.calls[-1]
    assert call["model"] == svc.settings.OPENAI_MODEL and call["temperature"] == 0.7
    assert call["response_format"] == {"type": "json_object"}
    assert budget == [("charge", 800), ("settle", 7)]


def test_no_key_means_no_llm_call():
    with providers.override(generation_chat_client=None):
        assert svc.llm_client() is None
        assert svc.generate_email_with_llm({"designation": "CTO"}, _blueprint(), product_name="Widget") is None
