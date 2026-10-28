"""
Warm-up reads peer mailboxes with the effective IMAP settings (S-26, app/domain/inbox_mailbox.effective_imap), as
the IMAP sync does: a host derived from SMTP, the SMTP password reused, saved IMAP settings first, Microsoft 365
mailboxes signing in with their OAuth token, send-only relays never read. Fakes only: a fake mail connector
(providers.override), a fake warm-up repository and session.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.adapters import providers
from app.services import warmup_imap
from app.services import warmup_service as warmup_mod
from app.services.ms365_oauth import AUTH_TYPE


def _mailbox(inbox_id="peer", **kw):
    base = dict(inbox_id=inbox_id, tenant_id="t1", email_address=f"{inbox_id}@acme.example.com",
                auth_type="PASSWORD", smtp_host="smtp.acme.example.com", smtp_port=587, smtp_username=None,
                smtp_password="smtp-pw", imap_host=None, imap_port=None, imap_username=None, imap_password=None,
                warmup_enabled=True, status="ACTIVE", is_in_cooling=False, last_sync_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeImap:
    def select(self, folder, readonly=False):
        return "NO", None  # the warm-up email is not there yet: no metrics are written

    def logout(self):
        pass


class FakeConnector:
    def __init__(self):
        self.opened = []

    def open_imap(self, host, port, *, timeout=None, starttls=False):
        self.opened.append((host, port, starttls))
        return FakeImap()

    def open_smtp(self, *a, **k):
        raise AssertionError("not used")


class FakeRepo:
    def __init__(self, *inboxes):
        self.inboxes = {i.inbox_id: i for i in inboxes}

    def inbox(self, inbox_id):
        return self.inboxes.get(inbox_id)


class FakeDb:
    def __init__(self):
        self.closed = False

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def engage(monkeypatch):
    """Runs _do_imap_engagement for a peer; returns (connections opened, logins)."""
    logins = []
    monkeypatch.setattr(warmup_imap.ms365_oauth, "imap_login",
                        lambda mail, username, secret: logins.append((username, secret)))

    def run(peer):
        connector, db = FakeConnector(), FakeDb()
        monkeypatch.setattr(warmup_mod, "SessionLocal", lambda: db)
        monkeypatch.setattr(warmup_mod, "SqlAlchemyWarmupRepository", lambda _db: FakeRepo(_mailbox("sender"), peer))
        with providers.override(mail_connector=connector):
            asyncio.run(warmup_mod.warmup_service._do_imap_engagement(
                inbox_id="sender", peer_inbox_id=peer.inbox_id, subject="Hi", sender_email="sender@acme.example.com",
                delay_seconds=0))
        assert db.closed
        return connector.opened, logins

    return run


def test_warmimap_derived_host_and_smtp_password_are_used(engage):
    opened, logins = engage(_mailbox())
    assert opened == [("imap.acme.example.com", 993, False)]
    assert logins == [("peer@acme.example.com", "smtp-pw")]


def test_warmimap_known_provider_host_and_smtp_username(engage):
    opened, logins = engage(_mailbox(smtp_host="smtp.gmail.com", smtp_username="login@acme.example.com"))
    assert opened == [("imap.gmail.com", 993, False)] and logins == [("login@acme.example.com", "smtp-pw")]


def test_warmimap_saved_imap_settings_win(engage):
    opened, logins = engage(_mailbox(imap_host="mail.other.example.com", imap_port=143, imap_username="imap-user",
                                     imap_password="imap-pw"))
    assert opened == [("mail.other.example.com", 143, True)] and logins == [("imap-user", "imap-pw")]


def test_warmimap_oauth_mailbox_signs_in_with_its_token(engage, monkeypatch):
    asked = []
    monkeypatch.setattr(warmup_mod.ms365_oauth, "imap_secret", lambda db, inbox: asked.append(inbox.inbox_id) or "tok")
    peer = _mailbox(auth_type=AUTH_TYPE, smtp_host="smtp.office365.com", smtp_password=None,
                    imap_host="outlook.office365.com", imap_port=993, imap_username="peer@acme.example.com")
    opened, logins = engage(peer)
    assert asked == ["peer"]
    assert opened == [("outlook.office365.com", 993, False)] and logins == [("peer@acme.example.com", "tok")]


def test_warmimap_oauth_mailbox_without_a_token_is_not_read(engage, monkeypatch):
    monkeypatch.setattr(warmup_mod.ms365_oauth, "imap_secret", lambda db, inbox: None)
    opened, logins = engage(_mailbox(auth_type=AUTH_TYPE, smtp_host="smtp.office365.com", smtp_password=None))
    assert opened == [] and logins == []


def test_warmimap_send_only_relay_is_not_read(engage):
    peer = _mailbox(smtp_host="email-smtp.us-east-1.amazonaws.com", smtp_username="AKIA", smtp_password="ses-pw")
    opened, logins = engage(peer)
    assert opened == [] and logins == []
    assert not warmup_mod._imap_configured(peer)


@pytest.mark.parametrize("peer, expected", [
    (_mailbox(), True),  # SMTP only: derived host, SMTP password
    (_mailbox(smtp_host="smtp.sendgrid.net"), False),
    (_mailbox(smtp_host="relay.acme.example.com"), False),  # nothing to derive
    (_mailbox(smtp_host=None, smtp_password=None, imap_host="imap.acme.example.com", imap_password="pw"), True),
    (_mailbox(auth_type=AUTH_TYPE, smtp_host="smtp.office365.com", smtp_password=None), True),
    (_mailbox(smtp_host="smtp.office365.com", smtp_password=None), False),  # password mailbox without one
])
def test_warmimap_engagement_is_scheduled_for_readable_mailboxes(peer, expected):
    assert warmup_mod._imap_configured(peer) is expected


def test_warmimap_health_no_longer_flags_a_derivable_mailbox():
    inbox = _mailbox("me")
    pool = [inbox, _mailbox("other")]
    assert warmup_mod.warmup_service._assess_health(inbox, pool) == (None, None, False)
    relay = _mailbox("relay", smtp_host="email-smtp.eu-west-1.amazonaws.com")
    assert warmup_mod.warmup_service._assess_health(relay, pool)[0] == "IMAP_NOT_CONFIGURED"
