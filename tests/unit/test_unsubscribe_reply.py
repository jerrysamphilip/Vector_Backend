"""Reply-based unsubscribe detection (app/services/imap_sync_service._is_unsubscribe_reply, BR-DF-08)."""
import pytest

from app.services.imap_sync_service import _UNSUB_PHRASES, _is_unsubscribe_reply
from app.utils.email_utils import extract_latest_message_text


@pytest.mark.parametrize("text", [
    "unsubscribe", "Unsubscribe!", "UNSUBSCRIBE.", "Please unsubscribe me.", "unsubscribe me", "remove me",
    "Please remove me", "Remove me from your list", "opt out", "Opt-out", "STOP", "  stop  ",
    "unsubscribe\n\n--\nPat Smith\nAcme Inc.",            # signature after the first line
    "\n\nPlease unsubscribe\n> quoted original mail",       # leading blank lines are ignored
])
def test_short_unsubscribe_requests_are_detected(text):
    assert _is_unsubscribe_reply(text) is True


@pytest.mark.parametrize("text", [
    None, "", "   \n  ",
    "Please don't unsubscribe me, I love these",
    "stop sending these",
    "I'd like to unsubscribe",
    "Re: unsubscribe",
    "Thanks, sounds interesting.\nunsubscribe",           # only the first line counts
    "stop" + "!" * 60,                                    # first line longer than 60 characters
])
def test_other_replies_are_not_unsubscribes(text):
    assert _is_unsubscribe_reply(text) is False


def test_every_listed_phrase_is_detected_on_its_own():
    for phrase in _UNSUB_PHRASES:
        assert _is_unsubscribe_reply(phrase), phrase


def test_detection_needs_the_boilerplate_kept():
    """The IMAP sync extracts the reply with strip_boilerplate=False; with stripping, the word is lost."""
    raw = "unsubscribe\n\nOn Mon, Oct 12, 2026 at 9:00 AM Sam <sam@x.example> wrote:\n> Hi Pat"
    assert _is_unsubscribe_reply(extract_latest_message_text(raw, strip_boilerplate=False)) is True
    assert _is_unsubscribe_reply(extract_latest_message_text(raw)) is False
