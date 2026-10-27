"""Rule-based reply classification (app/domain/inbox_reply_rules.py, S-09) and the label value objects."""
import pytest

from app.domain import inbox_reply_rules as rules
from app.domain.inbox import (UNSUBSCRIBE_AUTO_CONFIDENCE, ConversationQuery, LabelSource, OwnerScope,
                              ReplyClassification, ReplyLabel)


@pytest.mark.parametrize("text", [
    "Unsubscribe", "please unsubscribe me", "STOP", "Remove me from your list.", "Please stop emailing me.",
    "Do not contact me again", "I no longer wish to receive these", "opt out", "Take me off your list please",
    "Not interested, please remove me.", "Never email me again!",
    "unsubscribe\n\n--\nPat Smith\nAcme Inc.",
])
def test_unsubscribe_requests(text):
    assert rules.is_unsubscribe_request(text)
    result = rules.classify_by_rules("Re: hi", text)
    assert result.label is ReplyLabel.UNSUBSCRIBE and result.confidence >= UNSUBSCRIBE_AUTO_CONFIDENCE


@pytest.mark.parametrize("text", [
    "Please don't stop emailing me, this is useful.",
    "No need to unsubscribe me, I like these.",
    "I do not want to unsubscribe.",
    "Don't remove me from the list!",
    "Thanks, sounds interesting.",
    "Don't hesitate to contact me next week.",
    "",
    None,
])
def test_not_unsubscribe(text):
    assert not rules.is_unsubscribe_request(text)
    assert rules.classify_by_rules("Re: hi", text).label is not ReplyLabel.UNSUBSCRIBE


def test_own_footer_and_quoted_lines_are_not_the_persons_request():
    body = ("Thanks, happy to chat on Tuesday.\n"
            "Please reply 'unsubscribe' in case you do not wish to receive such business communication emails.\n"
            "> To unsubscribe click here")
    assert not rules.is_unsubscribe_request(body)
    assert rules.classify_by_rules("Re: hi", body).label is ReplyLabel.POSITIVE


def test_long_reply_with_opt_out_words_is_only_flagged():
    body = ("We have been reviewing vendors for a while. " * 12) + "Perhaps remove me from the list for now."
    result = rules.classify_by_rules("Re: hi", body)
    assert result.label is ReplyLabel.UNSUBSCRIBE
    assert result.confidence < UNSUBSCRIBE_AUTO_CONFIDENCE
    assert result.needs_review and not result.suppresses
    assert rules.pre_classify("Re: hi", body) is None  # not decided without the LLM


@pytest.mark.parametrize("headers", [
    {"Auto-Submitted": "auto-replied"}, {"auto-submitted": "Auto-Generated"}, {"X-Autoreply": "yes"},
    {"Precedence": "auto_reply"},
])
def test_auto_reply_headers(headers):
    result = rules.classify_by_rules("Re: hi", "Thanks for your email", headers)
    assert (result.label, result.confidence, result.source) == (ReplyLabel.OUT_OF_OFFICE, 1.0, LabelSource.HEADER)


def test_auto_submitted_no_is_a_person():
    assert not rules.is_auto_reply_header({"Auto-Submitted": "no"})


def test_ooo_phrases():
    result = rules.classify_by_rules("Automatic reply: Hello", "I am out of the office until Monday.")
    assert result.label is ReplyLabel.OUT_OF_OFFICE and result.source is LabelSource.RULES


@pytest.mark.parametrize("text,label", [
    ("Not interested, thanks.", ReplyLabel.NEGATIVE),
    ("No thanks, we're all set.", ReplyLabel.NEGATIVE),
    ("We already use another tool.", ReplyLabel.NEGATIVE),
    ("Sounds good, let's talk next week.", ReplyLabel.POSITIVE),
    ("Can you send me pricing?", ReplyLabel.POSITIVE),
    ("I'm interested - book a call for Thursday", ReplyLabel.POSITIVE),
    ("Forwarding to my colleague.", ReplyLabel.NEUTRAL),
])
def test_positive_negative_neutral(text, label):
    assert rules.classify_by_rules("Re: hi", text).label is label


def test_label_parse():
    assert ReplyLabel.parse("positive") is ReplyLabel.POSITIVE
    assert ReplyLabel.parse("Out of office") is ReplyLabel.OUT_OF_OFFICE
    assert ReplyLabel.parse("ooo") is ReplyLabel.OUT_OF_OFFICE
    assert ReplyLabel.parse("maybe") is None


def test_classification_confidence_bounds():
    with pytest.raises(ValueError):
        ReplyClassification(ReplyLabel.POSITIVE, 1.5, LabelSource.LLM)


def test_conversation_query_normalises_and_bounds():
    q = ConversationQuery(search="  pat  ", campaign_id=" ")
    assert q.search == "pat" and q.campaign_id is None
    assert ConversationQuery(search="   ").search is None
    with pytest.raises(ValueError):
        ConversationQuery(search="x" * 201)


def test_owner_scope():
    assert OwnerScope().allows("anyone") and OwnerScope().allows(None)
    scope = OwnerScope(frozenset({"u1"}), include_unowned=False)
    assert scope.allows("u1") and not scope.allows("u2") and not scope.allows(None)
    assert OwnerScope(frozenset({"u1"}), include_unowned=True).allows(None)
