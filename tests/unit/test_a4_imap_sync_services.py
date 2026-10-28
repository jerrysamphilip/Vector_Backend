"""
A4 IMAP sync service (app/services/imap_sync_service.py) with a fake unit of work, repository, mailbox,
connector and classifier: the folder steps (search after the UID position, per-message record with the
position, classification after the commit), stop on reply through the shared per-step rule plus the
no-step cancel, out-of-office replies, skips, and the T-1 rule (nothing runs inside a unit of work while
IMAP or the classifier is called).
"""
import email.message
import imaplib
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.domain.inbox_sync import REPLY_STOP_REASON, SyncPosition
from app.services import imap_sync_service as mod
from app.services.imap_sync_service import IMAPSyncService

UIDVALIDITY = 9


class FakeRepo:
    def __init__(self, uow):
        self.uow = uow
        self.prospects = {}           # email -> prospect
        self.outbound = {}            # provider id -> message
        self.conversations = {}
        self.enrollments = {}         # (campaign, prospect) -> enrollment
        self.added = []
        self.stored_ids = set()
        self.positions = {}
        self.cancelled = []
        self.inboxes = []

    def inboxes_to_sync(self):
        return self.inboxes

    def get_inbox(self, inbox_id):
        return next((i for i in self.inboxes if i.inbox_id == inbox_id), None)

    def position(self, inbox_id, folder):
        p = self.positions.get((inbox_id, folder))
        return None if p is None else SyncPosition(*p)

    def save_position(self, inbox_id, folder, uidvalidity, last_uid):
        self.uow.pending_positions[(inbox_id, folder)] = (uidvalidity, last_uid)

    def find_prospect(self, tenant_id, email_address):
        p = self.prospects.get(email_address)
        return p if p is not None and p.tenant_id == tenant_id else None

    def message_stored(self, provider_id, prospect_id):
        return provider_id in self.stored_ids

    def outbound_by_header(self, prospect_id, ids):
        return next((self.outbound[i] for i in ids if i in self.outbound), None)

    def recent_outbound(self, prospect_id, inbox_id):
        return list(self.outbound.values())

    def conversation(self, tenant_id, conversation_id):
        return self.conversations.get(conversation_id)

    def conversation_for_inbox(self, prospect_id, inbox_id):
        return None

    def conversation_with_outbound(self, tenant_id, prospect_id):
        return None

    def add_conversation(self, conversation):
        conversation.id = f"conv-{len(self.conversations) + 1}"
        self.conversations[conversation.id] = conversation
        return conversation

    def thread_last_campaign_outbound(self, conversation_id):
        return None

    def latest_enrollment(self, prospect_id):
        return None

    def enrollment(self, campaign_id, prospect_id):
        return self.enrollments.get((campaign_id, prospect_id))

    def add(self, entity):
        self.uow.pending.append(entity)
        return entity

    def cancel_pending_without_step(self, campaign_id, prospect_id, reason):
        self.cancelled.append((campaign_id, prospect_id, reason))
        return 1


class Savepoint:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUow:
    """Counts enter / commit; staged entities and positions only survive a commit."""

    def __init__(self):
        self.sync = FakeRepo(self)
        self.session = SimpleNamespace(name="session")
        self.active = False
        self.enters = self.commits = 0
        self.pending, self.pending_positions = [], {}

    def __enter__(self):
        assert not self.active, "units of work never nest"
        self.active = True
        self.enters += 1
        return self

    def __exit__(self, *exc):
        self.active = False
        self.pending, self.pending_positions = [], {}  # uncommitted work is rolled back
        return False

    def commit(self):
        self.commits += 1
        self.sync.added.extend(self.pending)
        self.sync.positions.update(self.pending_positions)
        self.pending, self.pending_positions = [], {}

    def savepoint(self):
        return Savepoint()


class FakeMailbox:
    def __init__(self, uow, messages, abort_on=None):
        self.uow, self.messages, self.abort_on = uow, messages, abort_on
        self.fetched = []

    def _no_tx(self):
        assert not self.uow.active, "IMAP round trip inside a unit of work (T-1)"

    def login(self, user, password):
        self._no_tx()

    def select(self, mailbox, readonly=False):
        self._no_tx()
        return ("OK", [b"1"]) if mailbox == "INBOX" else ("NO", [b""])

    def response(self, code):
        return code, [str(UIDVALIDITY).encode()]

    def uid(self, command, *args):
        self._no_tx()
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        uid = int(args[0])
        if uid == self.abort_on:
            raise imaplib.IMAP4.abort("reset")
        self.fetched.append(uid)
        return "OK", [(b"x", self.messages[uid])]

    def logout(self):
        pass


class Connector:
    def __init__(self, box):
        self.box = box

    def open_imap(self, host, port, *, timeout=None, starttls=None):
        return self.box


class Classifier:
    def __init__(self, uow):
        self.uow, self.calls = uow, []

    def classify_and_store(self, reply_id, replied_to_id):
        assert not self.uow.active, "LLM call inside a unit of work (T-1)"
        self.calls.append((reply_id, replied_to_id))


def _reply(uid, sender, subject="Re: Hello", in_reply_to="<out-1@x>", body="Sounds good", **headers):
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "me@sender.io", subject
    m["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
    m["Message-ID"] = f"<r{uid}@client.io>"
    m["In-Reply-To"] = in_reply_to
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


INBOX = SimpleNamespace(inbox_id="i1", tenant_id="t1", email_address="me@sender.io", auth_type="PASSWORD",
                        imap_host="imap.sender.io", imap_port=993, imap_username=None, imap_password="pw",
                        last_sync_at=None, imap_last_error=None)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(mod, "set_prospect_status", lambda enrollment, status: setattr(enrollment, "status", status))
    monkeypatch.setattr(mod.webhooks, "email_replied", lambda *a, **k: None)
    INBOX.last_sync_at = INBOX.imap_last_error = None  # a successful sync stamps the shared inbox
    uow = FakeUow()
    prospect = SimpleNamespace(prospect_id="p1", tenant_id="t1", email="pat@client.io")
    uow.sync.prospects["pat@client.io"] = prospect
    out = SimpleNamespace(message_id="out-1", conversation_id="c1", campaign_id="camp1", subject="Hello")
    uow.sync.outbound["<out-1@x>"] = out
    uow.sync.conversations["c1"] = SimpleNamespace(id="c1", last_message_at=datetime(2026, 1, 1), is_unread=False)
    uow.sync.enrollments[("camp1", "p1")] = SimpleNamespace(status="ENROLLED")
    stops = []

    def stop_steps(session, campaign_id, prospect_id, status, failure_reason=None):
        stops.append((session, campaign_id, prospect_id, status, failure_reason))
        return 2

    def run(messages, abort_on=None):
        box = FakeMailbox(uow, messages, abort_on)
        classifier = Classifier(uow)
        svc = IMAPSyncService(uow=uow, classifier=classifier, connector=Connector(box), stop_steps=stop_steps)
        ok = svc.sync_inbox(INBOX, days_back=7)
        return ok, box, classifier
    return SimpleNamespace(uow=uow, run=run, stops=stops, out=out)


def _of(uow, kind):
    return [e for e in uow.sync.added if type(e).__name__ == kind]


def test_reply_is_recorded_with_position_then_classified(env):
    ok, box, classifier = env.run({1: _reply(1, "Pat <pat@client.io>"), 2: _reply(2, "pat@client.io")})
    assert ok is True and box.fetched == [1, 2]
    messages = _of(env.uow, "EmailMessage")
    assert [(m.direction, m.conversation_id, m.campaign_id) for m in messages] == [("INBOUND", "c1", "camp1")] * 2
    events = _of(env.uow, "EmailEvent")
    assert [(e.message_id, e.event_type) for e in events] == [("out-1", 4), ("out-1", 4)]
    assert env.uow.sync.positions[("i1", "INBOX")] == (UIDVALIDITY, 2)
    assert classifier.calls == [(messages[0].message_id, "out-1"), (messages[1].message_id, "out-1")]


def test_reply_stops_steps_and_no_step_mail(env):
    env.run({1: _reply(1, "pat@client.io")})
    assert env.stops == [(env.uow.session, "camp1", "p1", "REPLIED", REPLY_STOP_REASON)] * 1
    assert env.uow.sync.cancelled == [("camp1", "p1", REPLY_STOP_REASON)]
    assert env.uow.sync.enrollments[("camp1", "p1")].status == "REPLIED"


def test_out_of_office_is_labelled_not_counted(env):
    ok, _, classifier = env.run({1: _reply(1, "pat@client.io", subject="Out of office",
                                           body="I am out of the office until Monday.",
                                           Auto_Submitted="auto-replied")})
    assert ok is True
    msg = _of(env.uow, "EmailMessage")[0]
    assert msg.reply_label == "OUT_OF_OFFICE" and classifier.calls == []
    assert sorted(e.event_type for e in _of(env.uow, "EmailEvent")) == [4, 9]
    assert env.stops == [] and env.uow.sync.cancelled == []


def test_unknown_sender_and_duplicates_are_skipped(env):
    env.uow.sync.stored_ids.add("<r2@client.io>")
    ok, box, classifier = env.run({1: _reply(1, "stranger@else.io"), 2: _reply(2, "pat@client.io")})
    assert ok is True and box.fetched == [1, 2]
    assert _of(env.uow, "EmailMessage") == [] and classifier.calls == []
    assert env.uow.sync.positions[("i1", "INBOX")] == (UIDVALIDITY, 2)  # the cursor still moves on


def test_connection_lost_keeps_position_and_records_error(env):
    ok, box, _ = env.run({1: _reply(1, "pat@client.io"), 2: _reply(2, "pat@client.io")}, abort_on=2)
    assert ok is False and box.fetched == [1]
    assert len(_of(env.uow, "EmailMessage")) == 1
    assert env.uow.sync.positions[("i1", "INBOX")] == (UIDVALIDITY, 1)
    assert "reset" in INBOX.imap_last_error


def test_resumes_after_the_stored_position(env):
    env.uow.sync.positions[("i1", "INBOX")] = (UIDVALIDITY, 1)
    ok, box, _ = env.run({1: _reply(1, "pat@client.io"), 2: _reply(2, "pat@client.io")})
    assert ok is True and box.fetched == [2]


def test_not_configured_inbox_is_refused(env):
    svc = IMAPSyncService(uow=env.uow, classifier=Classifier(env.uow), connector=None)
    assert svc.sync_inbox(SimpleNamespace(**{**INBOX.__dict__, "imap_password": None})) is False


def test_background_job_skips_recently_synced(env, monkeypatch):
    seen = []
    svc = IMAPSyncService(uow=env.uow, classifier=Classifier(env.uow))
    monkeypatch.setattr(svc, "sync_inbox", lambda inbox, days_back: seen.append((inbox.inbox_id, days_back)))
    fresh = SimpleNamespace(**{**INBOX.__dict__, "inbox_id": "fresh", "last_sync_at": mod.utcnow()})
    env.uow.sync.inboxes = [INBOX, fresh]
    svc.sync_all_active_inboxes()
    assert seen == [("i1", 7)]
