"""Agent follow: on an HTML body the postal line, unsubscribe line and sign-off are their own <p> blocks.

Appended with "\\n" they ran into the last paragraph in the HTML part (HTML ignores newlines). Plain-text bodies
keep the exact lines they always had. Checked on the built MIME message (send_email over a fake SES transport)
and on the preview (email_render.finalize_for_send), which must agree.
"""
import asyncio
import email
import re
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import email_sender_service as sender_mod
from app.services.email_render import SendContext, finalize_for_send
from app.services.email_sender_service import EmailSenderService

IDENTITY = SimpleNamespace(footer_line="Acme & Co, 1 Main St, Springfield", postal_address="1 Main St, Springfield",
                           company_name="Acme & Co")
HTML_BODY = "<p>Hello Bob,</p><p>A question about <strong>your</strong> pipeline.</p>"
PLAIN_BODY = "Hello Bob,\n\nA question."
UNSUB = "https://app.example.com/api/tracking/unsubscribe/m1"


class FakeTransport:
    def __init__(self):
        self.raw = []

    def send_raw(self, **kwargs):
        self.raw.append(kwargs)
        return {"MessageId": f"ses-{len(self.raw)}"}


@pytest.fixture
def sender(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(sender_mod, "get_sender_identity", lambda tenant_id, db=None: IDENTITY)
    monkeypatch.setattr(sender_mod, "session_or_none", lambda entity: None)
    monkeypatch.setattr(sender_mod.compliance_service, "is_business_email", lambda email: True)
    monkeypatch.setattr(settings, "SPAM_SCORE_BLOCK_THRESHOLD", 0)
    monkeypatch.setattr(settings, "BASE_URL", "https://app.example.com")
    return EmailSenderService(transport=transport), transport


def _send(service, transport, body, mode):
    message = SimpleNamespace(message_id="m1", campaign_id="c1", subject="Quick question", body_text=body,
                              campaign=SimpleNamespace(cta_link=None, sender_title="CEO", unsubscribe_mode=mode))
    prospect = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="bob@client.example.com", first_name="Bob",
                               last_name=None, company_name=None, designation=None, industry=None,
                               linkedin_url=None, poc_city=None, poc_state="CA")
    result = asyncio.run(service.send_email(message, SimpleNamespace(subject="", body="", attachments=[]), prospect,
                                            sender_name="Ann Lee", from_email_address="ann@acme.example.com"))
    assert result["success"], result
    parsed = email.message_from_string(transport.raw[-1]["RawMessage"]["Data"])
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8") for p in parsed.walk()
             if p.get_content_type() in ("text/plain", "text/html")}
    return parts["text/html"], parts["text/plain"]


def _blocks(html):
    """The text of each <p> block of the HTML part."""
    return [re.sub(r"<[^>]+>", "", b).strip() for b in re.findall(r"<p>(.*?)</p>", html, re.S)]


@pytest.mark.parametrize("mode", ["plain", "html"])
def test_html_body_gets_footer_blocks_in_the_html_part(sender, mode):
    service, transport = sender
    html, text = _send(service, transport, HTML_BODY, mode)
    blocks = _blocks(html)
    assert blocks[:2] == ["Hello Bob,", "A question about your pipeline."]
    assert blocks[2] == "Acme &amp; Co, 1 Main St, Springfield"
    assert blocks[3].startswith("Please reply 'unsubscribe'" if mode == "plain" else "Please click on unsubscribe")
    assert "Regards,<br>Ann Lee<br>CEO<br>Acme &amp; Co" in html
    if mode == "html":
        assert f'<a href="{UNSUB}">unsubscribe</a>' in html
    # The text part keeps every line on its own line
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    i = lines.index("Acme & Co, 1 Main St, Springfield")
    assert lines[i - 1] == "A question about your pipeline."
    assert lines[i + 1].startswith("Please ")
    assert lines[-4:] == ["Regards,", "Ann Lee", "CEO", "Acme & Co"]


def test_plain_body_is_unchanged(sender):
    service, transport = sender
    body = service._compliance_body(PLAIN_BODY, SimpleNamespace(message_id="m1", campaign=SimpleNamespace(
        unsubscribe_mode="plain")), SimpleNamespace(tenant_id="t1"), IDENTITY, False, UNSUB, "Ann Lee", "CEO")
    assert body == (PLAIN_BODY + "\n\nAcme & Co, 1 Main St, Springfield"
                    "\nPlease reply 'unsubscribe' in case you do not wish to receive such business communication"
                    " emails in the future.\n\nRegards,\nAnn Lee\nCEO\nAcme & Co")
    html, text = _send(service, transport, PLAIN_BODY, "plain")
    assert text == body


def _preview(body, mode, recipient="bob@client.example.com"):
    return finalize_for_send("Quick question", body, recipient, SendContext(
        message_id="m1", sender_email="ann@acme.example.com", base_url="https://app.example.com",
        sender_name="Ann Lee", sender_title="CEO", unsubscribe_mode=mode, postal_address=IDENTITY.postal_address,
        footer_line=IDENTITY.footer_line, company_name=IDENTITY.company_name))


def _sent(service, body, mode, is_internal=False):
    return service._compliance_body(body, SimpleNamespace(message_id="m1", campaign=SimpleNamespace(
        unsubscribe_mode=mode)), SimpleNamespace(tenant_id="t1"), IDENTITY, is_internal, UNSUB, "Ann Lee", "CEO")


@pytest.mark.parametrize("mode", ["plain", "html"])
def test_preview_matches_the_sender(sender, mode):
    service, _ = sender
    sent = _sent(service, HTML_BODY, mode)
    preview = _preview(HTML_BODY, mode)
    assert preview.body == sent and preview.is_html
    assert sent.startswith(HTML_BODY + "<p>Acme &amp; Co, 1 Main St, Springfield</p><p>Please ")
    assert sent.endswith("<p>Regards,<br>Ann Lee<br>CEO<br>Acme &amp; Co</p>")


PLAIN_BODIES = [
    PLAIN_BODY,
    "Hi {{unsubscribe_link}}\n\nBest,\nAnn",  # placeholder filled in, own sign-off kept
    "Hello Bob,\n\nAcme & Co, 1 Main St, Springfield\n\nThanks,\nAnn",  # address already there
    "Hello Bob,\n\nOpt out: https://x.example.com/api/tracking/unsubscribe/m1",  # link already there
]


@pytest.mark.parametrize("mode", ["plain", "html"])
@pytest.mark.parametrize("body", PLAIN_BODIES)
def test_preview_matches_the_sender_on_plain_text(sender, mode, body):
    service, _ = sender
    sent = _sent(service, body, mode)
    assert _preview(body, mode).body == sent
    assert "<p>" not in sent  # a plain-text body keeps plain lines
    # Same-domain (internal) mail: no postal / unsubscribe lines, still one sign-off, on both paths
    internal = _sent(service, body, mode, is_internal=True)
    assert _preview(body, mode, recipient="colleague@acme.example.com").body == internal
    assert IDENTITY.footer_line not in internal or IDENTITY.footer_line in body


def test_plain_text_footer_is_byte_identical_to_before(sender):
    """The exact lines the sender appended before the footer moved into email_render.compliance_body."""
    service, _ = sender
    assert _sent(service, PLAIN_BODY, "html") == (
        PLAIN_BODY + "\n\nAcme & Co, 1 Main St, Springfield\nPlease click on <a href=\"" + UNSUB
        + "\">unsubscribe</a> in case you do not wish to receive such business communication emails in the future."
        "\n\nRegards,\nAnn Lee\nCEO\nAcme & Co")


def test_sender_uses_the_shared_footer_function(sender, monkeypatch):
    """send_email builds the footer with email_render.compliance_body, the function the preview calls."""
    service, transport = sender
    calls = []

    def spy(body, ctx, *, is_internal):
        calls.append((body, ctx, is_internal))
        return body + "\n\n[shared footer]\n\nRegards,\nAnn"

    monkeypatch.setattr(sender_mod, "compliance_body", spy)
    html, text = _send(service, transport, PLAIN_BODY, "plain")
    [(body, ctx, is_internal)] = calls
    assert body == PLAIN_BODY and is_internal is False
    assert (ctx.unsubscribe_url, ctx.unsubscribe_mode, ctx.footer_line, ctx.sender_name, ctx.sender_title,
            ctx.company_name) == (UNSUB, "plain", IDENTITY.footer_line, "Ann Lee", "CEO", "Acme & Co")
    assert "[shared footer]" in text
