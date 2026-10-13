"""Out-of-office detection and the no-LLM path of reply classification (app/utils/reply_classifier.py).

classify_reply_intent must return None (unknown) without an OpenAI client, so tests and keyless
environments never spend money; the unit conftest also blocks the network.
"""
import pytest

from app.utils import reply_classifier as rc


@pytest.mark.parametrize("header", ["auto-replied", "Auto-Generated", "  AUTO-REPLIED "])
def test_auto_submitted_header_wins(header):
    assert rc.is_out_of_office("Re: pricing", "Sounds great", header) is True


@pytest.mark.parametrize("subject,body", [
    ("Automatic reply: Away", ""),
    ("Out of Office", ""),
    ("Re: hello", "I am currently out of the office until Monday."),
    ("Re: hello", "I'm on vacation with limited access to email."),
    ("Re: hello", "Thanks, I am on annual leave and returning on 3 Nov."),
    ("Autoreply", ""),
])
def test_ooo_text_patterns(subject, body):
    assert rc.is_out_of_office(subject, body) is True


@pytest.mark.parametrize("subject,body,header", [
    ("Re: pricing", "Sounds great, let's meet Tuesday.", None),
    ("Re: pricing", "Sounds great", "no"),
    ("", "", None),
    (None, None, None),
    ("Re: hello", "x" * 600 + " out of office", None),     # only the first 500 body characters are read
])
def test_normal_replies_are_not_ooo(subject, body, header):
    assert rc.is_out_of_office(subject, body, header) is False


def test_classification_is_unknown_without_a_client(monkeypatch):
    monkeypatch.setattr(rc, "_openai_client", None)
    assert rc.classify_reply_intent("Re: hi", "Yes, let's talk next week") is None


def test_classification_is_unknown_for_an_empty_body(monkeypatch):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("the OpenAI client must not be used for an empty body")
    monkeypatch.setattr(rc, "_openai_client", Boom())
    assert rc.classify_reply_intent("Re: hi", "   ") is None
    assert rc.classify_reply_intent("Re: hi", None) is None
