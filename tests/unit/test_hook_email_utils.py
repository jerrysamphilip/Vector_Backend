"""Sender-domain parsing (18), SES Message-ID matching and subject normalisation for reply threading (17)."""
import pytest

from app.utils.email_utils import (extract_sender_address, extract_sender_domain, normalize_subject,
                                   ses_id_from_message_id, thread_message_ids)


@pytest.mark.parametrize("source,domain", [
    ("a@dom.com", "dom.com"),
    ("Name <a@dom.com>", "dom.com"),
    ('"Last, First" <a@Mail.Dom.COM>', "mail.dom.com"),
    ("Last, First <a@dom.com>", "dom.com"),          # unquoted comma: parseaddr alone gives up
    ("  A@DOM.COM  ", "dom.com"),
    ("a@dom.com.", "dom.com"),
])
def test_sender_domain_is_parsed(source, domain):
    assert extract_sender_domain(source) == domain


@pytest.mark.parametrize("source", [None, "", 42, "no-at-sign", "a@", "@dom.com", "Name <a@dom>", "<>",
                                    "a@dom com"])
def test_sender_domain_is_none_when_invalid(source):
    assert extract_sender_domain(source) is None


def test_sender_address_is_bare_and_lower_case():
    assert extract_sender_address("Jane Doe <Jane@Dom.com>") == "jane@dom.com"
    assert extract_sender_address("broken") is None


@pytest.mark.parametrize("header,ses_id", [
    ("<0100018abc-def-000000@email.amazonses.com>", "0100018abc-def-000000"),
    ("<0102018abc@eu-west-1.amazonses.com>", "0102018abc"),
    ("0102018abc@us-east-1.amazonses.com", "0102018abc"),
    (" <X1@EMAIL.AMAZONSES.COM> ", "X1"),
])
def test_ses_message_id_is_extracted(header, ses_id):
    assert ses_id_from_message_id(header) == ses_id


@pytest.mark.parametrize("header", [None, "", "<abc@send.example.com>", "<abc@amazonses.com.evil.com>",
                                    "<abc@notamazonses.com>", "<a b@email.amazonses.com>"])
def test_non_ses_message_id_gives_none(header):
    assert ses_id_from_message_id(header) is None


def test_thread_ids_in_reply_to_first_then_newest_reference():
    assert thread_message_ids("<c>", "<a> <b> <c>") == ["<c>", "<b>", "<a>"]
    assert thread_message_ids(None, None) == []
    assert thread_message_ids("  ", "<a>") == ["<a>"]


@pytest.mark.parametrize("subject,expected", [
    ("Re: Hello there", "hello there"),
    ("RE: Fwd: AW:  Hello   there ", "hello there"),
    ("Re[2]: Hello", "hello"),
    ("WG: SV: Hello", "hello"),
    ("Hello", "hello"),
    ("Regarding pricing", "regarding pricing"),   # a word starting with "re" is not a prefix
    (None, ""),
    ("Re:", ""),
])
def test_subject_normalisation(subject, expected):
    assert normalize_subject(subject) == expected
