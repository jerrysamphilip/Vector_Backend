"""
A8: the sender's named steps (content snapshot, refusals, compliance body, SES request) and pure content helpers;
warm-up SMTP through the mail connector (DNS-rebinding pinning, S2); the warm-up service's scoring wiring.
Fakes only: a fake SES transport, a fake mail connector, a fake warm-up repository.
"""
import asyncio
import email
from email.mime.multipart import MIMEMultipart
from types import SimpleNamespace

import pytest

from app.adapters import providers
from app.core.config import settings
from app.services import email_sender_service as sender_mod
from app.services import warmup_service as warmup_mod
from app.services import warmup_smtp
from app.services.email_sender_service import EmailSenderService


class FakeTransport:
    def __init__(self):
        self.raw = []

    def send_raw(self, **kwargs):
        self.raw.append(kwargs)
        return {"MessageId": f"ses-{len(self.raw)}"}


IDENTITY = SimpleNamespace(footer_line="Acme Inc, 1 Main St, Springfield", postal_address="1 Main St, Springfield",
                           company_name="Acme Inc")


def _prospect(email="bob@client.example.com", **kw):
    base = dict(prospect_id="p1", tenant_id="t1", email=email, first_name="Bob", last_name=None,
                company_name=None, designation=None, industry=None, linkedin_url=None, poc_city=None,
                poc_state="CA")
    base.update(kw)
    return SimpleNamespace(**base)


def _message(**kw):
    base = dict(message_id="m1", campaign_id="c1", subject="Quick question", body_text="Hello Bob,\n\nA question.",
                campaign=SimpleNamespace(cta_link=None, sender_title="CEO", unsubscribe_mode="plain"))
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def sender(monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(sender_mod, "get_sender_identity", lambda tenant_id, db=None: IDENTITY)
    monkeypatch.setattr(sender_mod, "session_or_none", lambda entity: None)
    monkeypatch.setattr(sender_mod.compliance_service, "is_business_email", lambda email: True)
    monkeypatch.setattr(settings, "SPAM_SCORE_BLOCK_THRESHOLD", 0)
    monkeypatch.setattr(settings, "AWS_SES_CONFIGURATION_SET", "events")
    return EmailSenderService(transport=transport), transport


def _send(service, message, prospect, **kw):
    return asyncio.run(service.send_email(message, SimpleNamespace(subject="", body="", attachments=[]), prospect,
                                          sender_name="Ann Lee", from_email_address="ann@acme.example.com", **kw))


# ── send_email, end to end over a fake transport ──

def test_a8_send_email_builds_the_compliant_ses_request(sender):
    service, transport = sender
    result = _send(service, _message(), _prospect())
    [call] = transport.raw
    assert result["success"] and result["ses_message_id"] == "ses-1" and result["status"] == "SENT"
    assert result["internet_message_id"].endswith("@acme.example.com>")
    assert call["Source"] == "Ann Lee <ann@acme.example.com>" and call["Destinations"] == ["bob@client.example.com"]
    assert call["ConfigurationSetName"] == "events" and call["Tags"] == [{"Name": "message_id", "Value": "m1"}]
    raw = call["RawMessage"]["Data"]
    assert "List-Unsubscribe:" in raw and "Subject: Quick question" in raw
    text = _plain_part(raw)
    assert IDENTITY.footer_line in text and "reply 'unsubscribe'" in text and text.count("Regards,") == 1


def test_a8_compliance_body_adds_address_unsubscribe_and_one_sign_off(sender):
    service, _ = sender
    body = service._compliance_body("Hello Bob,\n\nA question.", _message(), _prospect(), IDENTITY, False,
                                    "https://app.example.com/api/tracking/unsubscribe/m1", "Ann Lee", "CEO")
    assert IDENTITY.footer_line in body and "reply 'unsubscribe'" in body and body.count("Regards,") == 1
    html = service._compliance_body("Hi {{unsubscribe_link}}\n\nBest,\nAnn", _message(), _prospect(), IDENTITY,
                                    False, "https://u.example.com/api/tracking/unsubscribe/m1", "Ann", None)
    assert html.startswith("Hi https://u.example.com/api/tracking/unsubscribe/m1") and "Regards," not in html


def test_a8_internal_mail_has_no_unsubscribe_footer_or_list_headers(sender):
    service, transport = sender
    _send(service, _message(), _prospect(email="colleague@acme.example.com"))
    raw = transport.raw[0]["RawMessage"]["Data"]
    text = _plain_part(raw)
    assert "List-Unsubscribe" not in raw and "unsubscribe" not in text and IDENTITY.footer_line not in text
    assert "Regards," in text  # the sign-off still applies


def _plain_part(raw):
    message = email.message_from_string(raw)
    part = next(p for p in message.walk() if p.get_content_type() == "text/plain")
    return part.get_payload(decode=True).decode("utf-8")


def test_a8_personal_address_is_refused_before_ses(sender, monkeypatch):
    service, transport = sender
    monkeypatch.setattr(sender_mod.compliance_service, "is_business_email", lambda email: False)
    result = _send(service, _message(), _prospect(email="bob@gmail.example.com"))
    assert result["success"] is False and result["status"] == "BLOCKED_PERSONAL_EMAIL" and transport.raw == []


def test_a8_spam_lint_over_the_threshold_is_refused(sender, monkeypatch):
    service, transport = sender
    monkeypatch.setattr(settings, "SPAM_SCORE_BLOCK_THRESHOLD", 25)
    seen = {}

    def lint(subject, body, domain):
        seen["domain"] = domain
        return SimpleNamespace(warnings=[SimpleNamespace(message="FREE!!!")], composite_score=40,
                               trigger_names=["free"])

    monkeypatch.setattr(sender_mod.compliance_service, "lint_email_content", lint)
    result = _send(service, _message(), _prospect())
    assert result["status"] == "BLOCKED_SPAM" and "Lint score 40" in result["error"] and transport.raw == []
    assert seen["domain"] == "acme.example.com"


def test_a8_message_content_substitutes_the_template_without_a_snapshot(sender):
    service, _ = sender
    message = _message(subject="", body_text=None)
    template = SimpleNamespace(subject="Hi {{first_name}}\n<b>now</b>", body="Hello {{FIRST_NAME}} in {{city}}, team")
    subject, body = service._message_content(message, template, _prospect(), "Ann", None, None)
    assert subject == "Hi Bob now" and message.subject == subject
    assert body.startswith("Hello Bob in team") and message.body_text == body


# ── Pure content helpers ──

def test_a8_placeholder_values_defaults():
    values = sender_mod.placeholder_values(_prospect(first_name=None), None, "https://cal.example.com", None, None)
    assert values["{{first_name}}"] == "there" and values["{{full_name}}"] == "there"
    assert values["{{company}}"] == "your company" and values["{{title}}"] == "Professional"
    assert values["{{your_name}}"] == settings.SENDER_NAME and values["{{cta_link}}"] == "https://cal.example.com"


def test_a8_tidy_substitution_keeps_paragraphs():
    assert sender_mod.tidy_substitution("providers in , , Texas") == "providers in Texas"
    assert sender_mod.tidy_substitution("a ,  b , .\n\nnext") == "a, b.\n\nnext"


def test_a8_clean_subject_internal_mail_and_unsubscribe_line():
    assert sender_mod.clean_subject(" <i>Hi</i>\r\n  there ") == "Hi there"
    assert sender_mod.is_internal_mail("a@Acme.example.com", "b@acme.example.com")
    assert not sender_mod.is_internal_mail("a@acme.example.com", "b@other.example.com")
    assert not sender_mod.is_internal_mail("nobody", "nobody")
    assert "<a href=\"https://u/x\">unsubscribe</a>" in sender_mod.with_unsubscribe_line("Hi", "https://u/x", "html")
    assert "reply 'unsubscribe'" in sender_mod.with_unsubscribe_line("Hi", "https://u/x", None)
    linked = "see /api/tracking/unsubscribe/m1"
    assert sender_mod.with_unsubscribe_line(linked, "https://u/x", "html") == linked


def test_a8_ses_request_without_a_configuration_set(monkeypatch):
    monkeypatch.setattr(settings, "AWS_SES_CONFIGURATION_SET", "")
    msg = MIMEMultipart()
    request = EmailSenderService._ses_request("A <a@x.example.com>", "b@y.example.com", msg, _message())
    assert "ConfigurationSetName" not in request and request["Tags"][0]["Value"] == "m1"


# ── Warm-up SMTP goes through the mail connector (S2: host checked and pinned) ──

class FakeSmtp:
    def __init__(self):
        self.sent, self.quit_called = [], False

    def sendmail(self, from_addr, to_addrs, data):
        self.sent.append((from_addr, to_addrs))

    def quit(self):
        self.quit_called = True


def test_a8_warmup_smtp_uses_the_mail_connector(monkeypatch):
    opened, logins, smtp = [], [], FakeSmtp()

    class Connector:
        def open_smtp(self, host, port, *, use_ssl, timeout):
            opened.append((host, port, use_ssl, timeout))
            return smtp

        def open_imap(self, *a, **k):
            raise AssertionError("not used")

    monkeypatch.setattr(warmup_smtp.ms365_oauth, "smtp_login", lambda s, user, pw: logins.append((user, pw)))
    inbox = SimpleNamespace(smtp_host="smtp.acme.example.com", smtp_port=None, smtp_username=None,
                            smtp_password="pw", smtp_use_ssl=False, email_address="me@acme.example.com")
    with providers.override(mail_connector=Connector()):
        warmup_smtp._smtp_send_blocking(inbox, "peer@acme.example.com", MIMEMultipart())
    assert opened == [("smtp.acme.example.com", 587, False, 30)] and logins == [("me@acme.example.com", "pw")]
    assert smtp.sent == [("me@acme.example.com", ["peer@acme.example.com"])] and smtp.quit_called


def test_a8_warmup_smtp_failure_falls_back_to_ses(monkeypatch):
    class Refusing:
        def open_smtp(self, host, port, *, use_ssl, timeout):
            raise OSError("Mail server not allowed: private address")

    async def ses(**kw):
        return {"success": True, "internet_message_id": "<ses@x>"}

    monkeypatch.setattr(warmup_smtp.email_sender, "send_plain_email", ses)
    inbox = SimpleNamespace(smtp_host="10.0.0.5", smtp_port=587, smtp_username="me", smtp_password="pw",
                            smtp_use_ssl=False, email_address="me@acme.example.com", auth_type="PASSWORD")
    with providers.override(mail_connector=Refusing()):
        result = asyncio.run(warmup_smtp.send_via_inbox(inbox, "peer@acme.example.com", "Hi", "Body"))
    assert result == {"success": True, "internet_message_id": "<ses@x>", "error": None}


# ── Warm-up service: scoring wired to the repository ──

class FakeWarmupRepo:
    def __init__(self, history=(), external=None, inboxes=()):
        self.history, self.external, self.inboxes = list(history), external, list(inboxes)
        self.asked = []

    def recent_metrics(self, inbox_id, excluding_date, limit):
        self.asked.append(("history", inbox_id, excluding_date, limit))
        return self.history

    def external_reputation(self, tenant_id, domain):
        self.asked.append(("external", tenant_id, domain))
        return self.external

    def tenant_inboxes(self, tenant_id):
        return self.inboxes


def test_a8_warmup_reputation_reads_a_week_and_the_domain(monkeypatch):
    repo = FakeWarmupRepo(external=SimpleNamespace(complaint_rate=0, bounce_rate=0, reputation_score=100))
    monkeypatch.setattr(warmup_mod, "SqlAlchemyWarmupRepository", lambda db: repo)
    metric = SimpleNamespace(metric_date="2026-10-27", actual_sends=10, open_count=5, reply_count=2,
                             saved_from_spam_count=0, bounce_count=0, complaint_count=0)
    inbox = SimpleNamespace(inbox_id="i1", tenant_id="t1", email_address="me@Acme.example.com", warmup_day=10,
                            warmup_issue_code=None)
    score = warmup_mod.warmup_service._score_reputation(None, inbox, metric)
    assert repo.asked == [("history", "i1", "2026-10-27", 6), ("external", "t1", "acme.example.com")]
    assert score == round((58 + 16 + 50 * 0.18 + 20 * 0.25) * 0.7 + 30, 1)


def test_a8_warmup_overview_uses_the_tenants_inboxes(monkeypatch):
    monkeypatch.setattr(warmup_mod, "SqlAlchemyWarmupRepository", lambda db: FakeWarmupRepo())
    assert warmup_mod.warmup_service.get_overview(None, "t1")["total_accounts"] == 0
