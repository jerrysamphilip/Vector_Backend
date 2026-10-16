"""
Mailbox SMTP/IMAP connections verify TLS and never log in over plain text (app/core/tls.py).

imaplib / smtplib are replaced by in-memory fakes: no sockets are opened.
"""
import imaplib
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import tls
from app.core.config import settings


# ── fakes ──────────────────────────────────────────────────────

class FakeIMAP4:
    instances: list = []
    error = imaplib.IMAP4.error
    abort = imaplib.IMAP4.abort

    def __init__(self, host, port=143, timeout=None, capabilities=("IMAP4REV1", "STARTTLS")):
        self.host, self.port, self.timeout = host, port, timeout
        self.capabilities = tuple(capabilities)
        self.ssl_context = None
        self.encrypted = False
        self.logins: list = []
        self.closed = False
        FakeIMAP4.instances.append(self)

    def starttls(self, ssl_context=None):
        self.ssl_context = ssl_context
        self.encrypted = True
        return "OK", [b"Begin TLS"]

    def login(self, user, password):
        if not self.encrypted:
            raise AssertionError("password sent over an unencrypted IMAP connection")
        self.logins.append((user, password))
        return "OK", [b"logged in"]

    def logout(self):
        return "BYE", []

    def shutdown(self):
        self.closed = True


class FakeIMAP4_SSL(FakeIMAP4):
    def __init__(self, host, port=993, ssl_context=None, timeout=None):
        super().__init__(host, port, timeout)
        self.ssl_context = ssl_context
        self.encrypted = True


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port=0, timeout=None, extensions=("starttls", "auth")):
        self.host, self.port, self.timeout = host, port, timeout
        self.extensions = set(extensions)
        self.context = None
        self.encrypted = False
        self.logins: list = []
        self.sent: list = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def ehlo(self, name=""):
        return 250, b"ok"

    def has_extn(self, name):
        return name.lower() in self.extensions

    def starttls(self, context=None):
        self.context = context
        self.encrypted = True
        return 220, b"ready"

    def login(self, user, password):
        if not self.encrypted:
            raise AssertionError("password sent over an unencrypted SMTP connection")
        self.logins.append((user, password))

    def sendmail(self, frm, to, body):
        self.sent.append((frm, to))

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


class FakeSMTP_SSL(FakeSMTP):
    def __init__(self, host, port=465, timeout=None, context=None):
        super().__init__(host, port, timeout)
        self.context = context
        self.encrypted = True


def _assert_verifying(ctx):
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.fixture(autouse=True)
def fake_mail(monkeypatch):
    FakeIMAP4.instances.clear()
    FakeSMTP.instances.clear()
    monkeypatch.setattr(imaplib, "IMAP4", FakeIMAP4)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP4_SSL)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP_SSL)
    monkeypatch.setattr(settings, "MAIL_TLS_VERIFY", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _inbox(**kw):
    base = dict(inbox_id="i1", tenant_id="t1", email_address="me@example.com", auth_type="PASSWORD",
                smtp_host="smtp.example.com", smtp_port=587, smtp_username=None, smtp_password="pw",
                smtp_use_ssl=False, imap_host="imap.example.com", imap_port=993, imap_username=None,
                imap_password="pw", last_sync_at=None, imap_last_error=None, oauth_error=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _db_returning(inbox):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = inbox
    return db


# ── the helper ─────────────────────────────────────────────────

def test_tls_context_verifies_certificates_and_hostnames():
    _assert_verifying(tls.tls_context())


def test_unverified_escape_hatch_outside_production(monkeypatch, caplog):
    monkeypatch.setattr(settings, "MAIL_TLS_VERIFY", False)
    with caplog.at_level("WARNING", logger="app.core.tls"):
        ctx = tls.tls_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    assert "NOT verified" in caplog.text


def test_escape_hatch_ignored_in_production(monkeypatch):
    monkeypatch.setattr(settings, "MAIL_TLS_VERIFY", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    _assert_verifying(tls.tls_context())


def test_open_imap_implicit_tls_by_default():
    imap = tls.open_imap("imap.example.com", None, timeout=5)
    assert isinstance(imap, FakeIMAP4_SSL) and imap.port == 993
    _assert_verifying(imap.ssl_context)


def test_open_imap_port_143_uses_verified_starttls():
    imap = tls.open_imap("imap.example.com", 143)
    assert type(imap) is FakeIMAP4 and imap.encrypted
    _assert_verifying(imap.ssl_context)


def test_open_imap_refuses_server_without_starttls(monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4", lambda *a, **k: FakeIMAP4(*a, capabilities=("IMAP4REV1",), **k))
    with pytest.raises(tls.MailTLSError, match="STARTTLS"):
        tls.open_imap("imap.example.com", 143)
    assert FakeIMAP4.instances[-1].closed and not FakeIMAP4.instances[-1].logins


def test_open_smtp_starttls_required(monkeypatch):
    smtp = tls.open_smtp("smtp.example.com", 587, use_ssl=False, timeout=5)
    _assert_verifying(smtp.context)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: FakeSMTP(*a, extensions=("auth",), **k))
    with pytest.raises(tls.MailTLSError, match="STARTTLS"):
        tls.open_smtp("smtp.example.com", 25, use_ssl=False, timeout=5)
    assert FakeSMTP.instances[-1].closed and not FakeSMTP.instances[-1].logins


def test_tls_error_message():
    err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    err.verify_message = "self-signed certificate"
    assert "could not be verified (self-signed certificate)" in tls.tls_error_message(err)
    assert tls.tls_error_message(ValueError("x")) is None


# ── call sites ─────────────────────────────────────────────────

def test_warmup_smtp_passes_context():
    from app.services import warmup_smtp
    warmup_smtp._smtp_send_blocking(_inbox(), "to@example.com", MIMEMultipart())
    smtp = FakeSMTP.instances[-1]
    _assert_verifying(smtp.context)
    assert smtp.logins and smtp.sent
    warmup_smtp._smtp_send_blocking(_inbox(smtp_use_ssl=True, smtp_port=465), "to@example.com", MIMEMultipart())
    assert isinstance(FakeSMTP.instances[-1], FakeSMTP_SSL)
    _assert_verifying(FakeSMTP.instances[-1].context)


def test_warmup_imap_ssl_and_starttls(monkeypatch):
    from app.services import warmup_imap
    imap = warmup_imap._connect("imap.example.com", 993, "u", "p")
    assert isinstance(imap, FakeIMAP4_SSL) and imap.logins == [("u", "p")]
    _assert_verifying(imap.ssl_context)

    imap = warmup_imap._connect("imap.example.com", 143, "u", "p")
    assert type(imap) is FakeIMAP4 and imap.logins == [("u", "p")]
    _assert_verifying(imap.ssl_context)

    # A plain-port server without STARTTLS: refused before LOGIN (formerly a plain-text login)
    monkeypatch.setattr(imaplib, "IMAP4", lambda *a, **k: FakeIMAP4(*a, capabilities=("IMAP4REV1",), **k))
    with pytest.raises(tls.MailTLSError):
        warmup_imap._connect("imap.example.com", 1143, "u", "p")
    assert not FakeIMAP4.instances[-1].logins


def test_imap_sync_passes_context(monkeypatch):
    from app.services.imap_sync_service import IMAPSyncService
    svc = IMAPSyncService(MagicMock())
    monkeypatch.setattr(svc, "_sync_folder", MagicMock(return_value=True))
    assert svc.sync_inbox(_inbox()) is True
    imap = FakeIMAP4.instances[-1]
    assert isinstance(imap, FakeIMAP4_SSL) and imap.logins
    _assert_verifying(imap.ssl_context)


def test_inbox_router_test_endpoints_pass_context(monkeypatch):
    from app.routers import inbox_router
    monkeypatch.setattr(inbox_router, "_check_mail_host", lambda host: None)
    user = SimpleNamespace(tenant_id="t1")

    out = inbox_router.test_smtp_connection("i1", db=_db_returning(_inbox()), current_user=user)
    assert out["status"] == "connected"
    _assert_verifying(FakeSMTP.instances[-1].context)

    monkeypatch.setattr(FakeIMAP4, "select", lambda self, *a, **k: ("NO", []), raising=False)
    out = inbox_router.test_imap_connection("i1", db=_db_returning(_inbox()), current_user=user)
    assert out["status"] == "connected"
    _assert_verifying(FakeIMAP4.instances[-1].ssl_context)


def test_inbox_router_reports_certificate_errors(monkeypatch):
    from app.routers import inbox_router
    monkeypatch.setattr(inbox_router, "_check_mail_host", lambda host: None)
    user = SimpleNamespace(tenant_id="t1")

    def bad_cert(*a, **k):
        err = ssl.SSLCertVerificationError(1, "certificate verify failed")
        err.verify_message = "Hostname mismatch"
        raise err

    monkeypatch.setattr(imaplib, "IMAP4_SSL", bad_cert)
    monkeypatch.setattr(smtplib, "SMTP_SSL", bad_cert)
    out = inbox_router.test_imap_connection("i1", db=_db_returning(_inbox()), current_user=user)
    assert out["status"] == "connection_failed" and "certificate could not be verified" in out["error"]
    out = inbox_router.test_smtp_connection("i1", db=_db_returning(_inbox(smtp_use_ssl=True, smtp_port=465)),
                                            current_user=user)
    assert out["status"] == "connection_failed" and "Hostname mismatch" in out["error"]


def test_production_refuses_unverified_setting():
    import subprocess
    import sys
    code = "import app.core.config"
    env = {"ENVIRONMENT": "production", "MAIL_TLS_VERIFY": "false", "OPENAI_API_KEY": "",
           "JWT_SECRET_KEY": "x" * 40, "CREDENTIALS_ENCRYPTION_KEY": "unit-prod-key-not-the-dev-one",
           "PATH": "", "APP_PROFILE": "unit-none"}
    root = __file__.rsplit("/tests/", 1)[0]
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=root, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0 and "MAIL_TLS_VERIFY" in proc.stderr
