"""Regenerating a follow-up uses that step's tone, not the first email's (21). The OpenAI client is a stub."""
import json
from types import SimpleNamespace

import pytest

from app.prompts import get_step_tone_block
from app.services import ai_email_service as svc


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


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps({"subject": "Following up", "body": "Hi {{first_name}},\n\nMore detail."})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(total_tokens=12))


@pytest.fixture
def fake_openai(monkeypatch):
    completions = _FakeCompletions()
    monkeypatch.setattr(svc, "openai_client", SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(svc, "retry_with_backoff", lambda fn, **kw: fn())
    return completions


def _blueprint():
    return SimpleNamespace(tone_rules={"style": "professional"}, openers=[], value_angles=[],
                           persona_type="OTHER")


def _user_prompt(calls):
    return next(m["content"] for m in calls[-1]["messages"] if m["role"] == "user")


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
