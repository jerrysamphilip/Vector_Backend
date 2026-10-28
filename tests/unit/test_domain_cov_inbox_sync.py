"""Coverage of IMAP parsing helpers (app/domain/inbox_sync.py)."""
from datetime import datetime
from email.message import EmailMessage, Message

from app.domain.inbox_sync import parse_date, parse_uidvalidity, plain_body

DEFAULT = datetime(2020, 1, 1)


def test_uidvalidity_absent_or_unreadable():
    assert parse_uidvalidity([b"42"]) == 42
    assert parse_uidvalidity(None) is None
    assert parse_uidvalidity([None]) is None
    assert parse_uidvalidity([b"x"]) is None


def test_plain_body_of_multipart_picks_the_text_part():
    msg = EmailMessage()
    msg.set_content("hello there")
    msg.add_alternative("<p>hello</p>", subtype="html")
    assert plain_body(msg).strip() == "hello there"


def test_plain_body_of_multipart_without_text_part_is_empty():
    msg = EmailMessage()
    msg.set_content("<p>only html</p>", subtype="html")
    msg.add_attachment(b"\x00\x01", maintype="application", subtype="octet-stream", filename="a.bin")
    assert plain_body(msg) == ""


def test_plain_body_skips_a_text_plain_part_without_a_payload():
    inner = Message()
    inner["Content-Type"] = "text/plain"
    inner.set_payload("fallback text")
    odd = Message()
    odd["Content-Type"] = "text/plain"
    odd.set_payload([])  # a text/plain container: nothing to decode
    outer = Message()
    outer["Content-Type"] = "multipart/mixed"
    outer.set_payload([odd, inner])
    assert plain_body(outer) == "fallback text"


def test_parse_date_naive_tz_aware_and_unparseable():
    assert parse_date("Mon, 20 Nov 2023 10:00:00 +0200", DEFAULT) == datetime(2023, 11, 20, 8, 0)
    assert parse_date("Mon, 20 Nov 2023 10:00:00 -0000", DEFAULT) == datetime(2023, 11, 20, 10, 0)
    assert parse_date("not a date", DEFAULT) == DEFAULT
    assert parse_date(None, DEFAULT) == DEFAULT
