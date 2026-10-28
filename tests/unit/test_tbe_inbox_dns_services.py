"""S-26 / S-25 service layer with fakes: IMAP derived from SMTP in InboxService (connection test, history
sync, ``imap_configured`` on responses) and IMAPSyncService (sign-in details); SendingDomainService.dns_records
comparing each record with (fake) live DNS, and the dnspython adapter's result mapping. No network."""
from datetime import datetime
from types import SimpleNamespace

import dns.resolver
import pytest

from app.core.errors import BadRequestError
from app.schemas.inbox_schema import SendingInboxUpdate
from app.services import deliverability_service as dsvc
from app.services import imap_sync_service, inbox_service
from app.services.deliverability_service import SendingDomainService, compare_with_live_dns, live_dns_values
from app.services.imap_sync_service import IMAPSyncService
from app.services.inbox_service import InboxService

USER = SimpleNamespace(user_id="u1", tenant_id="t1", role="ADMIN")


class FakeUow:
    def __init__(self, **repos):
        self.__dict__.update(repos)
        self.session = SimpleNamespace()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1


class Mailboxes:
    def __init__(self, *rows):
        self.rows = {r.inbox_id: r for r in rows}

    def get(self, tenant_id, inbox_id):
        row = self.rows.get(inbox_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def refresh(self, row):
        pass


def _mailbox(**kw):
    base = dict(inbox_id="i1", tenant_id="t1", email_address="me@acme.io", provider="SMTP", status="ACTIVE",
                imap_host=None, imap_port=None, imap_username=None, imap_password=None, auth_type="PASSWORD",
                smtp_host="smtp.zoho.in", smtp_port=587, smtp_username="me@acme.io", smtp_password="app-pw",
                smtp_use_ssl=False, last_sync_at=None, imap_last_error=None, warmup_start_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    checked = []
    monkeypatch.setattr(inbox_service, "check_mail_host", lambda host: checked.append(host))
    return checked


class FakeImap:
    def __init__(self):
        self.logins = []

    def login(self, user, password):
        self.logins.append((user, password))

    def select(self, *a, **k):
        return "OK", None

    def search(self, *a):
        return "OK", [b""]

    def logout(self):
        pass


class Connector:
    def __init__(self):
        self.opened, self.mail = [], FakeImap()

    def open_imap(self, host, port, timeout=None):
        self.opened.append((host, port))
        return self.mail


def test_tbe_connection_test_uses_imap_derived_from_smtp(public_hosts):
    connector = Connector()
    svc = InboxService(FakeUow(inboxes=Mailboxes(_mailbox())), connector=connector)
    out = svc.test_imap(USER, "i1")
    assert out["status"] == "connected" and out["imap_derived_from_smtp"] is True
    assert (out["imap_host"], out["imap_port"], out["imap_username"]) == ("imap.zoho.in", 993, "me@acme.io")
    assert connector.opened == [("imap.zoho.in", 993)] and connector.mail.logins == [("me@acme.io", "app-pw")]
    assert public_hosts == ["imap.zoho.in"]  # the derived host is safety-checked too


def test_tbe_connection_test_reports_what_cannot_be_derived():
    relay = _mailbox(smtp_host="email-smtp.us-east-1.amazonaws.com")
    out = InboxService(FakeUow(inboxes=Mailboxes(relay)), connector=Connector()).test_imap(USER, "i1")
    assert out["status"] == "not_configured" and "imap_host is not set" in out["error"]
    no_pw = _mailbox(smtp_password=None)
    out = InboxService(FakeUow(inboxes=Mailboxes(no_pw)), connector=Connector()).test_imap(USER, "i1")
    assert out["status"] == "not_configured" and "imap_password" in out["error"]


def test_tbe_history_sync_accepts_derived_settings_and_refuses_none():
    class Sync:
        def sync_inbox(self, row, days_back):
            row.last_sync_at = datetime(2026, 10, 1)
            return True
    out = InboxService(FakeUow(inboxes=Mailboxes(_mailbox())), imap_sync=Sync).sync_history(USER, "i1", 3)
    assert out["imap_host"] == "imap.zoho.in" and out["status"] == "success"
    with pytest.raises(BadRequestError, match="IMAP is not configured"):
        InboxService(FakeUow(inboxes=Mailboxes(_mailbox(smtp_password=None))), imap_sync=Sync).sync_history(
            USER, "i1", 3)


def test_tbe_responses_say_whether_imap_is_configured(monkeypatch):
    monkeypatch.setattr(inbox_service.sending_alerts, "mailbox_status", lambda session, tenant, rows: {})
    rows = Mailboxes(_mailbox(), _mailbox(inbox_id="i2", email_address="two@acme.io", smtp_password=None),
                     _mailbox(inbox_id="i3", email_address="three@acme.io", imap_host="imap.acme.io",
                              imap_password="pw"))
    svc = InboxService(FakeUow(inboxes=rows))
    flags = {r.inbox_id: (r.imap_configured, r.imap_derived_from_smtp) for r in
             svc._with_status(None, "t1", list(rows.rows.values()))}
    assert flags == {"i1": (True, True), "i2": (False, True), "i3": (True, False)}
    assert svc.get(USER, "i3").imap_configured is True


def test_tbe_update_response_carries_imap_configured():
    svc = InboxService(FakeUow(inboxes=Mailboxes(_mailbox(smtp_password=None))))
    out = svc.update(USER, "i1", SendingInboxUpdate(smtp_password="new-pw"))
    assert out.imap_configured is True and out.imap_derived_from_smtp is True


def test_tbe_imap_sync_signs_in_with_the_effective_settings(monkeypatch):
    class Uow:
        session = SimpleNamespace()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass
    svc = IMAPSyncService(uow=Uow())
    login = svc._login_details(_mailbox())
    assert (login.host, login.port, login.username, login.secret) == ("imap.zoho.in", 993, "me@acme.io", "app-pw")
    assert imap_sync_service._imap_configured(_mailbox()) is True
    assert imap_sync_service._imap_configured(_mailbox(smtp_password=None)) is False
    assert imap_sync_service._imap_configured(None) is False
    monkeypatch.setattr(imap_sync_service.ms365_oauth, "imap_secret", lambda session, inbox: "token")
    oauth = _mailbox(auth_type=imap_sync_service.ms365_oauth.AUTH_TYPE, imap_host="outlook.office365.com",
                     smtp_password=None)
    assert svc._login_details(oauth).secret == "token" and imap_sync_service._imap_configured(oauth)


# ── S-25 ──

class Domains:
    def get_domain(self, tenant_id, name):
        return SimpleNamespace(spf_status="WARNING", dkim_status="PASS", dmarc_status="FAIL", updated_at=None)


class Engine:
    def dkim_tokens(self, name, create):
        return ["tok"]


LIVE = {("CNAME", "tok._domainkey.acme.io"): ["tok.dkim.amazonses.com."],
        ("TXT", "mail.acme.io"): ["v=spf1 include:_spf.google.com ~all"],
        ("MX", "mail.acme.io"): [],
        ("TXT", "_dmarc.acme.io"): ["v=DMARC1; p=reject"]}


def test_tbe_dns_records_compare_with_live_dns(monkeypatch):
    monkeypatch.setattr(dsvc.settings, "AWS_REGION", "us-east-1")
    asked = []
    svc = SendingDomainService(FakeUow(domains=Domains()), engine=Engine(), has_domain=lambda s, t, n: True,
                               dns_lookup=lambda rtype, host: asked.append((rtype, host)) or LIVE[(rtype, host)])
    out = svc.dns_records(USER, "acme.io")
    status = {(r["kind"], r["host"]): (r["status"], r["current_value"], r["suggested_value"])
              for r in out["dns_records"]}
    assert status[("DKIM", "tok._domainkey.acme.io")][0] == "OK"
    assert status[("SPF", "mail.acme.io")] == ("UPDATE", "v=spf1 include:_spf.google.com ~all",
                                               "v=spf1 include:_spf.google.com include:amazonses.com ~all")
    assert status[("MAIL_FROM", "mail.acme.io")][0] == "ADD"
    assert status[("DMARC", "_dmarc.acme.io")][:2] == ("OK", "v=DMARC1; p=reject")
    assert out["live_summary"] == {"OK": 2, "ADD": 1, "UPDATE": 1, "UNKNOWN": 0}
    assert out["live_checked_at"] is not None and out["spf_status"] == "WARNING" and len(asked) == 4
    # ?live=false: guidance only, no lookups
    asked.clear()
    plain = svc.dns_records(USER, "acme.io", live=False)
    assert asked == [] and "status" not in plain["dns_records"][0] and "live_summary" not in plain


def test_tbe_unreachable_dns_marks_the_rest_unknown_without_more_lookups():
    calls = []
    records = [{"kind": "DKIM", "type": "CNAME", "host": "<token>._domainkey.acme.io", "value": "<token>"},
               {"kind": "SPF", "type": "TXT", "host": "mail.acme.io", "value": "v=spf1 include:amazonses.com ~all"},
               {"kind": "DMARC", "type": "TXT", "host": "_dmarc.acme.io", "value": "v=DMARC1; p=none"}]
    out = compare_with_live_dns(records, lambda rtype, host: calls.append(host))  # returns None: failed
    assert [r["status"] for r in out] == ["UNKNOWN"] * 3 and calls == ["mail.acme.io"]


class _Rd:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_text(self):
        return "raw"


def test_tbe_live_dns_values_maps_answers_and_failures(monkeypatch):
    answers = {"TXT": [_Rd(strings=[b"v=spf1 ", b"-all"])],
               "MX": [_Rd(preference=10, exchange=SimpleNamespace(to_text=lambda: "mx.acme.io."))],
               "CNAME": [_Rd(target=SimpleNamespace(to_text=lambda: "t.dkim.amazonses.com."))],
               "A": [_Rd()]}
    errors = {"nx.acme.io": dns.resolver.NXDOMAIN(), "none.acme.io": dns.resolver.NoAnswer(),
              "slow.acme.io": dns.resolver.LifetimeTimeout(timeout=2.0, errors={})}

    class Resolver:
        lifetime = None

        def resolve(self, host, rtype):
            assert self.lifetime == dsvc.LIVE_DNS_TIMEOUT_SECONDS
            if host in errors:
                raise errors[host]
            return answers[rtype]
    monkeypatch.setattr(dsvc.dns.resolver, "Resolver", Resolver)
    assert live_dns_values("TXT", "acme.io") == ["v=spf1 -all"]
    assert live_dns_values("MX", "acme.io") == ["10 mx.acme.io."]
    assert live_dns_values("CNAME", "acme.io") == ["t.dkim.amazonses.com."]
    assert live_dns_values("A", "acme.io") == ["raw"]
    assert live_dns_values("TXT", "nx.acme.io") == [] and live_dns_values("TXT", "none.acme.io") == []
    assert live_dns_values("TXT", "slow.acme.io") is None
