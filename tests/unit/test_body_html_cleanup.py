"""
UAT defect 1 (body): the rich-text editor (TipTap ``getHTML``) saves a step body as ONE line of HTML, without "\\n"
between blocks. The line-based cleanups (CTA stripping without a CTA link, unsubscribe footer normalisation) then
deleted the whole body, and the sender sent an empty body. These tests pin the HTML-aware behaviour, the render
safety net and the sender's refusal of a body with no visible text. Pure: no DB.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import email_sender_service as sender_mod
from app.services.email_render import RenderContext, render_body, render_step_email
from app.services.email_scheduler_service import EmailSchedulerService
from app.services.email_sender_service import EmailSenderService
from app.utils import email_utils as eu

# What the TipTap RichTextEditor saves: one line, no "\n" between blocks
TIPTAP_URL = ('<p>Hi {{first_name}},</p><p>AccessOS is our AI platform for Market Access, Patient Access, HUB and FRM '
              'teams at specialty pharma companies like {{company_name}}. You can see it at '
              '<a target="_blank" rel="noopener noreferrer nofollow" href="https://accessos.example.com">'
              'accessos.example.com</a>.</p><ul><li><p>Benefit verification automation</p></li>'
              '<li><p>Affordability analytics</p></li></ul>'
              '<p>Would a 20 minute walkthrough next week be useful?</p><p>Regards,</p><p>{{signature_block}}</p>')
TIPTAP_SHORT = '<p>Hi {{first_name}},</p><p>Just bumping this up. Open to a quick demo?</p>'
TIPTAP_FOOTER = ('<p>Hi {{first_name}},</p><p>Last note from me about AccessOS for {{company_name}} and your team.</p>'
                 '<p>If you do not wish to receive these emails, just reply.</p>')

PROSPECT = SimpleNamespace(first_name="Amber", last_name="Corgan", company_name="QGenda", designation="VP",
                           email="amber@qgenda.example.com", industry="Health", linkedin_url="", poc_city="",
                           poc_state="")
NO_CTA = RenderContext(sender_name="Amy Parks", our_company="Neutrino")


def _text(body):
    return " ".join(eu.html_to_text(body).split())


# ── strip_cta_content_no_link on one-line HTML ──

def test_body_cta_strip_keeps_a_paragraph_that_contains_a_link():
    out = eu.strip_cta_content_no_link(TIPTAP_URL)
    text = _text(out)
    assert "AccessOS is our AI platform" in text and "You can see it at accessos.example.com." in text
    assert "Benefit verification automation" in text and "Affordability analytics" in text
    assert "Hi {{first_name}}," in text and "Regards," in text
    assert "href=" not in out and "https://" not in out  # the link markup goes, its text stays
    assert "walkthrough" not in text  # the short ask is still removed, per block
    assert "<ul>" in out and "<li>" in out


def test_body_cta_strip_removes_only_the_short_ask_block():
    out = eu.strip_cta_content_no_link(TIPTAP_SHORT)
    assert out == "<p>Hi {{first_name}},</p>"


def test_body_cta_strip_html_removes_a_bare_url_and_placeholders_but_keeps_the_text():
    out = eu.strip_cta_content_no_link('<p>Our case study lives at https://x.example.com/case and shows a 30% lift '
                                       'in approvals for teams of your size across the country.</p>'
                                       '<p>See {{cta_link}} for slots</p><p>Body text</p>')
    assert "https://" not in out and "{{cta_link}}" not in out
    assert "Our case study lives at" in out and "Body text" in out


def test_body_cta_strip_html_br_separated_lines():
    out = eu.strip_cta_content_no_link("<p>Hello Pat,<br>We help teams ship faster.<br>Open to a quick call?</p>")
    assert out == "<p>Hello Pat,<br>We help teams ship faster.</p>"


def test_body_cta_strip_html_drops_empty_lists_and_dangling_anchors():
    out = eu.strip_cta_content_no_link('<p>Body stays here.</p><ul><li><p>Book a demo</p></li></ul>'
                                       '<p><a href="">Schedule a call</a></p>')
    assert out == "<p>Body stays here.</p>"


def test_body_cta_strip_html_keeps_long_blocks_with_cta_words():
    long_block = ("<p>We work with teams who want to connect their data, explore new markets and talk to "
                  "customers more often, and we help them do that without adding headcount or tools.</p>")
    assert eu.strip_cta_content_no_link(long_block) == long_block


def test_body_cta_strip_formatter_html_matches_plain_text_rules():
    plain = "Hello Pat,\nWe help teams ship faster.\nOpen to a quick call?\nVisit https://x.example.com/book\nBest,\nSam"
    html_body = eu.format_email_body_html(plain)
    assert _text(eu.strip_cta_content_no_link(html_body)) == _text(eu.strip_cta_content_no_link(plain))


def test_body_cta_strip_plain_text_unchanged():
    assert eu.strip_cta_content_no_link("Visit https://x.example.com/book\nKeep me") == "Keep me"
    assert eu.strip_cta_content_no_link("Grab time: {{calendar_link}}\nBody text") == "Body text"


# ── normalize_unsubscribe_footer on one-line HTML ──

def test_body_footer_rule_removes_only_the_matching_block():
    out = eu.normalize_unsubscribe_footer(TIPTAP_FOOTER)
    assert out == ('<p>Hi {{first_name}},</p><p>Last note from me about AccessOS for {{company_name}} '
                   'and your team.</p>')


def test_body_footer_rule_html_unsubscribe_link_block_and_tracking_url():
    out = eu.normalize_unsubscribe_footer(
        '<p>Hi Pat,</p><p>Body text here.</p>'
        '<p>Click here to unsubscribe: <a href="https://app.example.com/api/tracking/unsubscribe/abc">link</a></p>'
        '<p>Thanks</p>')
    assert out == "<p>Hi Pat,</p><p>Body text here.</p><p>Thanks</p>"


def test_body_finalize_keeps_tiptap_bodies():
    for body in (TIPTAP_URL, TIPTAP_SHORT, TIPTAP_FOOTER):
        assert "Hi {{first_name}}," in _text(eu.finalize_email_body(body))
    assert eu.finalize_email_body(eu.finalize_email_body(TIPTAP_FOOTER)) == eu.finalize_email_body(TIPTAP_FOOTER)


# ── render_body: per block, and the safety net ──

@pytest.mark.parametrize("body,expected", [
    (TIPTAP_URL, ["Hi Amber,", "AccessOS is our AI platform", "QGenda", "Benefit verification automation",
                  "Regards,", "Amy Parks"]),
    (TIPTAP_SHORT, ["Hi Amber,"]),
    (TIPTAP_FOOTER, ["Hi Amber,", "Last note from me about AccessOS for QGenda"]),
])
def test_body_render_keeps_paragraphs_of_editor_html(body, expected):
    subject, rendered = render_step_email("Step {{first_name}}", eu.finalize_email_body(body), PROSPECT, NO_CTA)
    text = _text(rendered)
    assert subject == "Step Amber"
    for part in expected:
        assert part in text
    assert eu.conversation_snippet(rendered) != "No messages"


def test_body_render_safety_net_keeps_the_unstripped_body(caplog):
    with caplog.at_level(logging.WARNING):
        out = render_body("<p>Open to a quick demo?</p>", PROSPECT, NO_CTA)
    assert _text(out) == "Open to a quick demo?"
    assert any("no visible text" in r.getMessage() for r in caplog.records)
    assert render_body("Book a call?", PROSPECT, NO_CTA) == "<p>Book a call?</p>"
    assert render_body("", PROSPECT, NO_CTA) == ""


def test_body_visible_text_helper():
    assert eu.has_visible_text("<p>Hi</p>") and eu.has_visible_text("plain")
    assert not eu.has_visible_text("<p></p><p>&nbsp;</p><br>") and not eu.has_visible_text("  \n") \
        and not eu.has_visible_text(None)


# ── the sender never sends a body with no visible text ──

class _Transport:
    def __init__(self):
        self.raw = []

    def send_raw(self, **kwargs):
        self.raw.append(kwargs)
        return {"MessageId": "ses-1"}


@pytest.fixture
def sender(monkeypatch):
    transport = _Transport()
    monkeypatch.setattr(sender_mod, "get_sender_identity",
                        lambda tenant_id, db=None: SimpleNamespace(footer_line="Acme, 1 Main St",
                                                                  postal_address="1 Main St", company_name="Acme"))
    monkeypatch.setattr(sender_mod, "session_or_none", lambda entity: None)
    monkeypatch.setattr(sender_mod.compliance_service, "is_business_email", lambda email: True)
    monkeypatch.setattr(settings, "SPAM_SCORE_BLOCK_THRESHOLD", 0)
    return EmailSenderService(transport=transport), transport


def _send(service, body_text, template_body):
    message = SimpleNamespace(message_id="m1", campaign_id="c1", subject="Hi", body_text=body_text,
                              campaign=SimpleNamespace(cta_link=None, sender_title=None, unsubscribe_mode="plain"))
    prospect = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="bob@client.example.com", first_name="Bob",
                               last_name=None, company_name=None, designation=None, industry=None,
                               linkedin_url=None, poc_city=None, poc_state=None)
    template = SimpleNamespace(subject="Hi", body=template_body, attachments=[])
    return asyncio.run(service.send_email(message, template, prospect, sender_name="Ann",
                                          from_email_address="ann@acme.example.com")), message


@pytest.mark.parametrize("body_text,template_body", [
    ("", ""), ("<p></p>", "<p>Open to a quick demo?</p>"), (None, "<p>&nbsp;</p>"),
])
def test_body_sender_refuses_a_body_with_no_visible_text(sender, body_text, template_body):
    service, transport = sender
    result, _ = _send(service, body_text, template_body)
    assert result["success"] is False and result["status"] == "BLOCKED_EMPTY_BODY"
    assert "no visible text" in result["error"] and transport.raw == []


def test_body_sender_sends_editor_html_snapshot(sender):
    service, transport = sender
    result, _ = _send(service, render_body(eu.finalize_email_body(TIPTAP_SHORT), PROSPECT, NO_CTA), "")
    assert result["success"] is True and len(transport.raw) == 1


def test_body_sender_fallback_render_keeps_editor_html(sender):
    service, transport = sender
    result, message = _send(service, None, TIPTAP_FOOTER)
    assert result["success"] is True and "Last note from me" in message.body_text


def test_body_scheduler_marks_an_empty_body_refusal_failed():
    msg = SimpleNamespace(message_id="m1", status="SENDING", retry_count=0, max_retries=3, failure_reason=None,
                          last_error_code=None, final_status=None, final_status_at=None)
    prospect = SimpleNamespace(email="bob@client.example.com")
    result = {"success": False, "status": "BLOCKED_EMPTY_BODY", "error": "Email body has no visible text"}
    assert EmailSchedulerService._on_refused(msg, result, prospect) == "failed"
    assert msg.status == "FAILED" and msg.final_status == "FAILED" and msg.retry_count == 0
    assert msg.last_error_code == "BLOCKED_EMPTY_BODY" and "no visible text" in msg.failure_reason


def test_body_finalize_never_empties_a_body_with_its_footer_rule(caplog):
    with caplog.at_level(logging.WARNING):
        out = eu.finalize_email_body("<p>If you do not wish to receive these emails, reply STOP.</p>")
    assert _text(out) == "If you do not wish to receive these emails, reply STOP."
    assert any("no visible text" in r.getMessage() for r in caplog.records)
    assert eu.finalize_email_body("Unsubscribe here") == "<p>Unsubscribe here</p>"
    assert eu.finalize_email_body("Hi\n\nUnsubscribe here") == "<p>Hi</p>"
