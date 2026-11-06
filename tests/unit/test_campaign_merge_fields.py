"""Every merge field a library template can hold is filled in a campaign email, and no raw {{placeholder}}
is left in one (app/services/email_render.py)."""
import re
from types import SimpleNamespace

import pytest

from app.services.email_render import (KNOWN_TOKENS, RenderContext, render_step_email, substitute_placeholders,
                                       unknown_placeholders)
from app.services.sales_admin import MERGE_FIELDS

PAT = SimpleNamespace(first_name="Pat", last_name="Lee", company_name="Acme", designation="CFO", email="pat@acme.test",
                      industry="Software", linkedin_url="", poc_city="Austin", poc_state="TX")
CTX = RenderContext(sender_name="Ann Seller", cta_link="https://cal.example.com/ann", default_sender_name="Team")
RAW = re.compile(r"\{\{.*?\}\}")


def test_every_library_merge_field_is_filled_in_a_campaign_email():
    template = " ".join(token for token, _ in MERGE_FIELDS)
    out = substitute_placeholders(template, PAT, CTX)
    assert not RAW.search(out), out
    assert out == "Pat Lee Pat Lee Acme CFO pat@acme.test Software Ann Seller https://cal.example.com/ann"
    assert {token.strip("{}") for token, _ in MERGE_FIELDS} <= KNOWN_TOKENS


def test_seeded_library_templates_render_without_raw_placeholders():
    from app.db import seed_sales_demo
    bodies = [value for value in vars(seed_sales_demo).values() if isinstance(value, (list, tuple))]
    texts = [s for group in bodies for row in group if isinstance(row, (list, tuple)) for s in row
             if isinstance(s, str) and "{{" in s]
    assert texts, "the sales demo seed has library templates with merge fields"
    for text in texts:
        assert not RAW.search(substitute_placeholders(text, PAT, CTX)), text
        assert unknown_placeholders(text) == []


@pytest.mark.parametrize("token", ["{{First_Name}}", "{{ first_name }}", "{{FIRST_NAME}}", "{{first_name}}"])
def test_tokens_match_in_any_case_and_with_spaces(token):
    assert substitute_placeholders(f"Hi {token},", PAT, CTX) == "Hi Pat,"


def test_an_unknown_merge_field_is_removed_and_reported_but_the_unsubscribe_link_is_kept():
    subject, body = render_step_email("Hi {{first_name}} {{nickname}}", "<p>Call {{phone}} today</p>", PAT, CTX)
    assert subject == "Hi Pat " and not RAW.search(body) and "Call  today" in body
    assert substitute_placeholders("{{unsubscribe_link}}", PAT, CTX) == "{{unsubscribe_link}}"  # filled at send time
    assert unknown_placeholders("Hi {{Nickname}}", "{{phone}} {{nickname}} {{your_name}} {{unsubscribe_link}}") == [
        "nickname", "phone"]


def test_missing_values_fall_back_without_leaving_placeholders():
    nobody = SimpleNamespace(**{k: None for k in vars(PAT)})
    out = substitute_placeholders("{{full_name}} {{your_name}} {{calendar_link}}", nobody, RenderContext(default_sender_name="Team"))
    assert out == "there Team "


NO_LINK = RenderContext(sender_name="Ann Seller", cta_link="", default_sender_name="Team")


@pytest.mark.parametrize("body", [
    "Hi {{first_name}},\nThanks for sharing Acme's rollout plans for next quarter and the budget.\n"
    "Grab a time here: {{calendar_link}}\nBest, {{your_name}}",
    "<p>Hi {{first_name}},</p><p>Thanks for sharing Acme's rollout plans for next quarter and the budget.</p>"
    "<p>Grab a time here: {{ Calendar_Link }}</p><p>Best, {{your_name}}</p>",
])
def test_with_no_booking_link_the_sentence_around_calendar_link_goes_too(body):
    _, out = render_step_email("Hello", body, PAT, NO_LINK)
    assert "Grab a time" not in out and not RAW.search(out)
    assert "Hi Pat" in out and "rollout plans" in out and "Ann Seller" in out  # the rest of the email stays


def test_with_a_booking_link_the_sentence_and_link_stay():
    _, out = render_step_email("Hello", "<p>Grab a time here: {{calendar_link}}</p>", PAT, CTX)
    assert "Grab a time here: https://cal.example.com/ann" in out
