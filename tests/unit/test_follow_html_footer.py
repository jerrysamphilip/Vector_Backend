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


@pytest.mark.parametrize("mode", ["plain", "html"])
def test_preview_matches_the_sender(sender, mode):
    service, _ = sender
    sent = service._compliance_body(HTML_BODY, SimpleNamespace(message_id="m1", campaign=SimpleNamespace(
        unsubscribe_mode=mode)), SimpleNamespace(tenant_id="t1"), IDENTITY, False, UNSUB, "Ann Lee", "CEO")
    preview = finalize_for_send("Quick question", HTML_BODY, "bob@client.example.com", SendContext(
        message_id="m1", sender_email="ann@acme.example.com", base_url="https://app.example.com",
        sender_name="Ann Lee", sender_title="CEO", unsubscribe_mode=mode, postal_address=IDENTITY.postal_address,
        footer_line=IDENTITY.footer_line, company_name=IDENTITY.company_name))
    assert preview.body == sent and preview.is_html
    assert sent.startswith(HTML_BODY + "<p>Acme &amp; Co, 1 Main St, Springfield</p><p>Please ")
    assert sent.endswith("<p>Regards,<br>Ann Lee<br>CEO<br>Acme &amp; Co</p>")
