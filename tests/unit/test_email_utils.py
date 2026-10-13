"""Pure e-mail text helpers (app/utils/email_utils.py): address parsing, reply extraction,
HTML -> text, inbox snippets, CTA / unsubscribe clean-up and body formatting."""
import pytest

from app.core.config import settings
from app.utils import email_utils as eu


# ── parse_email ──────────────────────────────────────────────

def test_parse_email_normalises_business_address():
    assert eu.parse_email("  John.Doe@Acme.COM ") == {
        "email": "john.doe@acme.com", "email_type": "BUSINESS", "email_provider": "acme.com"}


@pytest.mark.parametrize("value", [None, 123, float("nan"), "", "   ", ["a@b.com"]])
def test_parse_email_rejects_non_strings_and_blanks(value):
    assert eu.parse_email(value) is None


@pytest.mark.parametrize("value", ["pat@gmail.com", "PAT@GMAIL.COM", "x@yahoo.com", "y@outlook.com", "z@icloud.com"])
def test_parse_email_rejects_personal_domains(value):
    assert eu.parse_email(value) is None


@pytest.mark.parametrize("value", ["broken@", "has space@x.com", "no-at-sign.com", "a@b.c", "a@@b.com",
                                   "a@-bad.com", ".dot@x.com", "a..b@x.com", "a@x.com.", "@x.com"])
def test_parse_email_rejects_malformed_addresses(value):
    assert eu.parse_email(value) is None


def test_parse_email_accepts_plus_tags_and_subdomains():
    parsed = eu.parse_email("first.last+tag@sub.example.co.uk")
    assert parsed["email"] == "first.last+tag@sub.example.co.uk"
    assert parsed["email_provider"] == "sub.example.co.uk"


# ── CTA links ────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "   ", "none", "NULL", "undefined", "N/A", "na",
                                   "{{calendar_link}}", "{cta_link}", "{{ CTA_LINK }}"])
def test_normalize_cta_link_treats_placeholders_as_empty(value):
    assert eu.normalize_cta_link(value) == ""
    assert eu.has_effective_cta_link(value) is False


def test_normalize_cta_link_keeps_real_links_trimmed():
    assert eu.normalize_cta_link("  https://cal.com/pat  ") == "https://cal.com/pat"
    assert eu.has_effective_cta_link("https://cal.com/pat") is True


def test_strip_cta_removes_short_ask_lines_and_keeps_body():
    body = ("Hello Pat,\n\nWe help teams ship faster.\n\n"
            "Would you be open to a quick call next week?\n\nBest,\nSam")
    assert eu.strip_cta_content_no_link(body) == "Hello Pat,\n\nWe help teams ship faster.\n\nBest,\nSam"


def test_strip_cta_removes_url_and_placeholder_lines():
    assert eu.strip_cta_content_no_link("Visit https://x.example.com/book\nKeep me") == "Keep me"
    assert eu.strip_cta_content_no_link("Grab time: {{calendar_link}}\nBody text") == "Body text"
    assert eu.strip_cta_content_no_link("Pick a slot {cta_link}\nBody text") == "Body text"


def test_strip_cta_removes_question_style_asks():
    assert eu.strip_cta_content_no_link("Body stays.\nWorth a look?") == "Body stays."


def test_strip_cta_never_removes_long_paragraphs():
    long_line = ("When we spoke with other operations leaders they told us the weekly call with finance "
                 "took most of a day, so we built reporting that removes that work entirely for good.")
    assert len(long_line.split()) > 20
    assert eu.strip_cta_content_no_link(long_line) == long_line


def test_strip_cta_empty_input():
    assert eu.strip_cta_content_no_link("") == ""
    assert eu.strip_cta_content_no_link(None) == ""


# ── Unsubscribe footer ───────────────────────────────────────

def test_unsubscribe_footer_lines_and_placeholders_are_removed():
    out = eu.normalize_unsubscribe_footer("Hi\n\nUnsubscribe here\nThanks {{unsubscribe_link}}")
    assert "unsubscribe" not in out.lower()
    assert out.startswith("Hi") and "Thanks" in out


def test_unsubscribe_tracking_urls_are_removed():
    out = eu.normalize_unsubscribe_footer("See https://app.example.com/api/tracking/unsubscribe/abc123 now")
    assert "tracking/unsubscribe" not in out
    assert out.startswith("See") and out.endswith("now")


def test_unsubscribe_footer_handles_none_and_trailing_spaces():
    assert eu.normalize_unsubscribe_footer(None) == ""
    assert eu.normalize_unsubscribe_footer("a  \t\nb") == "a\nb"
    assert eu.normalize_unsubscribe_footer("a\n\n\n\nb") == "a\n\nb"


def test_ensure_core_personalization_tokens_only_normalises_newlines():
    assert eu.ensure_core_personalization_tokens("  a\r\nb  ") == "a\nb"
    assert eu.ensure_core_personalization_tokens("") == ""


# ── Paragraph spacing ────────────────────────────────────────

def test_paragraph_spacing_separates_paragraphs_by_one_blank_line():
    assert eu.normalize_paragraph_spacing("Para one\nPara two") == "Para one\n\nPara two"
    assert eu.normalize_paragraph_spacing("A\n\n\n\nB") == "A\n\nB"


def test_paragraph_spacing_keeps_bullets_together_after_an_intro():
    assert eu.normalize_paragraph_spacing("Intro:\n- a\n\n- b\nAfter") == "Intro:\n- a\n- b\n\nAfter"


def test_paragraph_spacing_splits_the_greeting():
    assert eu.normalize_paragraph_spacing("Hi Pat, thanks for the note.") == "Hi Pat,\n\nthanks for the note."


def test_paragraph_spacing_empty_values_pass_through():
    assert eu.normalize_paragraph_spacing("") == ""
    assert eu.normalize_paragraph_spacing(None) is None


# ── Latest message extraction ────────────────────────────────

def test_extract_latest_cuts_quoted_history():
    body = "Sounds good, let's talk.\n\nOn Mon, Oct 5, 2026 at 9:00 AM Sam <sam@x.example> wrote:\n> Hi Pat"
    assert eu.extract_latest_message_text(body) == "Sounds good, let's talk."


@pytest.mark.parametrize("marker", ["From: Sam <sam@x.example>", "-----Original Message-----", "Sent: Monday",
                                    "Subject: Re: hello", "_" * 12])
def test_extract_latest_recognises_quote_markers(marker):
    assert eu.extract_latest_message_text(f"Yes please\n{marker}\nolder text") == "Yes please"


def test_extract_latest_keeps_a_marker_on_the_first_line():
    # A marker at position 0 is not quoted history (match.start() must be > 0)
    assert eu.extract_latest_message_text("From: the team, thanks").startswith("From:")


def test_extract_latest_strip_boilerplate_switch():
    assert eu.extract_latest_message_text("unsubscribe") == ""
    assert eu.extract_latest_message_text("unsubscribe", strip_boilerplate=False) == "unsubscribe"


def test_extract_latest_removes_signoffs_links_and_nul():
    assert eu.extract_latest_message_text("Thanks,\nPat") == "Pat"
    assert eu.extract_latest_message_text("Great\nhttps://example.com/x\n") == "Great"
    assert eu.extract_latest_message_text("Hi\x00 there") == "Hi there"
    assert eu.extract_latest_message_text("See [cid:image001.png@01D] <https://t.example/x>ok") == "See  ok"
    assert eu.extract_latest_message_text("") == ""
    assert eu.extract_latest_message_text(None) == ""


# ── HTML -> text and snippets ────────────────────────────────

def test_html_to_text_keeps_paragraph_breaks_and_decodes_entities():
    assert eu.html_to_text("<p>Hello</p><p>World&nbsp;&amp; co</p>") == "Hello\nWorld & co\n"


def test_html_to_text_drops_style_script_and_comments():
    assert eu.html_to_text("<style>p{}</style><script>x()</script>Hi<br/>there<!-- c -->") == "Hi\nthere"


def test_html_to_text_leaves_plain_text_alone():
    assert eu.html_to_text("a < b and c > d") == "a < b and c > d"
    assert eu.html_to_text("") == ""
    assert eu.html_to_text(None) == ""


def test_conversation_snippet_collapses_whitespace():
    assert eu.conversation_snippet("<p>Hi   there</p>\n\nfriend") == "Hi there friend"


def test_conversation_snippet_truncates_with_ellipsis():
    assert eu.conversation_snippet("x" * 150) == "x" * 100 + "..."
    assert eu.conversation_snippet("abcdef", limit=5) == "abcde..."
    assert eu.conversation_snippet("abcde", limit=5) == "abcde"


def test_conversation_snippet_empty_and_quoted():
    assert eu.conversation_snippet(None) == "No messages"
    assert eu.conversation_snippet("") == "No messages"
    assert eu.conversation_snippet("Sure\nOn Tue, Sam wrote:\n> old") == "Sure"


# ── Body formatting ──────────────────────────────────────────

def test_format_email_body_html_builds_paragraphs_and_lists():
    out = eu.format_email_body_html("Tom & Jerry\n• one\n- two\nBye")
    assert out == "<p>Tom &amp; Jerry</p>\n\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>\n\n<p>Bye</p>"


def test_format_email_body_html_leaves_html_and_empty_alone():
    assert eu.format_email_body_html("<p>already</p>") == "<p>already</p>"
    assert eu.format_email_body_html("") == ""
    assert eu.format_email_body_html(None) == ""


def test_finalize_email_body_is_idempotent():
    once = eu.finalize_email_body("Hi Pat, hope all is well.\n- a\n- b")
    assert "<p>Hi Pat,</p>" in once
    assert "<ul>\n<li>a</li>\n<li>b</li>\n</ul>" in once
    assert eu.finalize_email_body(once) == once


def test_build_signature_block(monkeypatch):
    assert eu.build_signature_block("Sam", " CEO ", " Acme ") == "Sam\nCEO\nAcme"
    assert eu.build_signature_block("Sam", "  ", None) == "Sam"
    monkeypatch.setattr(settings, "SENDER_NAME", "Outreach Bot")
    assert eu.build_signature_block(None) == "Outreach Bot"
