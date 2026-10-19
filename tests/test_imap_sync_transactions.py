"""IMAP sync never holds a DB transaction across external calls (T-1), in-process against MySQL.

imaplib is replaced by an in-memory mailbox and the OpenAI reply classifier by a stub; both assert
that the sync's session has no open transaction (and no pooled connection is checked out) while
they run. Each message is committed on its own together with the UID position, so a failure on
message N keeps messages 1..N-1 and the next sync resumes from there.
"""
import email.message
import imaplib
import uuid
from datetime import datetime

import pytest

from tests.conftest import RUN, SessionLocal, engine, register_tenant, sql

UIDVALIDITY = 777


def assert_no_open_transaction(db, where: str) -> None:
    """Fails when the session (or any pooled connection) is inside a transaction."""
    assert not db.in_transaction(), f"DB transaction open during {where}"
    assert engine.pool.checkedout() == 0, f"DB connection checked out during {where}"


class FakeMailbox:
    """Just enough of imaplib.IMAP4 for IMAPSyncService: one INBOX, UID SEARCH / FETCH."""

    def __init__(self, messages, db, fail_fetch_uid=None):
        self.messages = messages          # {uid: bytes}
        self.db = db
        self.fail_fetch_uid = fail_fetch_uid
        self.fetched: list = []
        self.selected = None

    def _check(self, what):
        assert_no_open_transaction(self.db, f"IMAP {what}")

    def login(self, user, password):
        self._check("LOGIN")
        return "OK", [b"Logged in"]

    def select(self, mailbox, readonly=False):
        self._check("SELECT")
        if mailbox != "INBOX":
            return "NO", [b"no such folder"]
        self.selected = mailbox
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(UIDVALIDITY).encode()]

    def uid(self, command, *args):
        self._check(f"UID {command}")
        if command == "SEARCH":
            criteria = args[-1]
            low = 1
            if criteria.startswith("(UID "):
                low = int(criteria[5:].split(":")[0])
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if u >= low).encode()]
        if command == "FETCH":
            uid = int(args[0])
            if uid == self.fail_fetch_uid:
                raise imaplib.IMAP4.abort("connection reset by peer")
            self.fetched.append(uid)
            return "OK", [(f"{uid} (BODY[] {{}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]


def _reply(uid: int, prospect_email: str, inbox_email: str, in_reply_to: str, subject: str) -> bytes:
    m = email.message.EmailMessage()
    m["From"] = prospect_email
    m["To"] = inbox_email
    m["Subject"] = subject
    m["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
    m["Message-ID"] = f"<reply-{uid}-{RUN}-{uuid.uuid4().hex[:6]}@client.example.com>"
    m["In-Reply-To"] = in_reply_to
    m.set_content(f"Sounds interesting, tell me more ({uid}).")
    return m.as_bytes()


@pytest.fixture(scope="module")
def mailbox_env(client):
    t = register_tenant(client, "imaptx")
    h = t["headers"]
    domain = f"imaptx{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass",
        "imap_host": "127.0.0.1", "imap_port": 993, "imap_username": "sender",
        "imap_password": "fixture-imap-pass", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    return {"tenant": t, "inbox_id": r.json()["inbox_id"], "inbox_email": f"sender@{domain}"}


@pytest.fixture
def thread(client, mailbox_env):
    """A fresh contact with one sent email (the message the fake replies answer)."""
    from app.models.email_message import EmailMessage
    t = mailbox_env["tenant"]
    prospect_email = f"p-{uuid.uuid4().hex[:8]}@client{RUN}.example.com"
    r = client.post("/contacts", headers=t["headers"], json={"email": prospect_email, "first_name": "Ima"})
    assert r.status_code == 201, r.text
    prospect_id = r.json()["prospect_id"]
    provider_id = f"<out-{uuid.uuid4().hex}@send.example.com>"
    with SessionLocal() as db:
        out = EmailMessage(prospect_id=prospect_id, inbox_id=mailbox_env["inbox_id"], direction="OUTBOUND",
                           status="SENT", subject="Hello", body_text="Hi", to_email=prospect_email,
                           from_email=mailbox_env["inbox_email"], provider_message_id=provider_id,
                           sent_at=datetime.utcnow())
        db.add(out)
        db.commit()
        out_id = out.message_id
    # Each test starts from an empty UID position
    sql("DELETE FROM imap_sync_state WHERE inbox_id = :i", i=mailbox_env["inbox_id"])
    sql("UPDATE sending_inboxes SET last_sync_at = NULL, imap_last_error = NULL WHERE inbox_id = :i",
        i=mailbox_env["inbox_id"])
    return {"prospect_id": prospect_id, "prospect_email": prospect_email, "provider_id": provider_id,
            "out_id": out_id}


def _messages(env, thread, n, prefix="Re: Hello"):
    return {uid: _reply(uid, thread["prospect_email"], env["inbox_email"], thread["provider_id"],
                        f"{prefix} {uid}") for uid in range(1, n + 1)}


def _run_sync(monkeypatch, env, box_factory, classifier=None):
    from app.models.sending_inbox import SendingInbox
    from app.services import imap_sync_service as mod
    with SessionLocal() as db:
        box = box_factory(db)
        monkeypatch.setattr(mod, "open_imap", lambda host, port, timeout=None: box)
        calls: list = []

        def classify(subject, body):
            assert_no_open_transaction(db, "reply classification")
            calls.append(subject)
            return classifier(subject) if classifier else "positive"
        monkeypatch.setattr(mod, "classify_reply_intent", classify)
        svc = mod.IMAPSyncService(db)
        inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == env["inbox_id"]).one()
        ok = svc.sync_inbox(inbox, days_back=7)
        return ok, box, calls


def _stored(thread):
    return sql("SELECT COUNT(*) FROM email_messages WHERE prospect_id = :p AND direction = 'INBOUND'",
               p=thread["prospect_id"])


def _position(env):
    return sql("SELECT last_uid FROM imap_sync_state WHERE inbox_id = :i AND folder = 'INBOX'", i=env["inbox_id"])


def test_no_transaction_open_during_imap_and_classifier(monkeypatch, mailbox_env, thread):
    msgs = _messages(mailbox_env, thread, 3)
    ok, box, calls = _run_sync(monkeypatch, mailbox_env, lambda db: FakeMailbox(msgs, db))
    assert ok is True
    assert box.fetched == [1, 2, 3]
    assert len(calls) == 3  # every reply classified, each outside a transaction
    assert _stored(thread) == 3
    assert _position(mailbox_env) == 3
    # Behaviour kept: REPLY + POSITIVE_REPLY events on the sent email, campaign-less reply
    assert sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m AND event_type = 4",
               m=thread["out_id"]) == 3
    assert sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m AND event_type = 8",
               m=thread["out_id"]) == 3
    assert sql("SELECT imap_last_error FROM sending_inboxes WHERE inbox_id = :i", i=mailbox_env["inbox_id"]) is None


def test_connection_lost_on_message_n_keeps_earlier_and_resumes(monkeypatch, mailbox_env, thread):
    msgs = _messages(mailbox_env, thread, 5)
    ok, box, _ = _run_sync(monkeypatch, mailbox_env, lambda db: FakeMailbox(msgs, db, fail_fetch_uid=3))
    assert ok is False
    assert box.fetched == [1, 2]
    assert _stored(thread) == 2          # messages 1..N-1 survive the failure
    assert _position(mailbox_env) == 2   # the cursor points at the last committed message
    assert "connection reset" in (sql("SELECT imap_last_error FROM sending_inboxes WHERE inbox_id = :i",
                                      i=mailbox_env["inbox_id"]) or "")

    # The next sync resumes at message N: nothing re-fetched, nothing duplicated
    ok, box, _ = _run_sync(monkeypatch, mailbox_env, lambda db: FakeMailbox(msgs, db))
    assert ok is True
    assert box.fetched == [3, 4, 5]
    assert _stored(thread) == 5
    assert _position(mailbox_env) == 5


def test_processing_error_on_message_n_keeps_others(monkeypatch, mailbox_env, thread):
    from app.services import webhooks
    real = webhooks.email_replied

    def flaky(db, tenant_id, reply, replied_to, prospect):
        if reply.subject.endswith(" 2"):
            raise RuntimeError("boom")
        return real(db, tenant_id, reply, replied_to, prospect)
    monkeypatch.setattr(webhooks, "email_replied", flaky)

    msgs = _messages(mailbox_env, thread, 3)
    ok, box, calls = _run_sync(monkeypatch, mailbox_env, lambda db: FakeMailbox(msgs, db))
    assert ok is True
    assert box.fetched == [1, 2, 3]
    assert _stored(thread) == 2  # message 2 rolled back alone; 1 and 3 committed
    assert sql("SELECT COUNT(*) FROM email_messages WHERE prospect_id = :p AND subject = 'Re: Hello 2'",
               p=thread["prospect_id"]) == 0
    assert len(calls) == 2
    assert _position(mailbox_env) == 3


def test_duplicate_messages_are_stored_once(monkeypatch, mailbox_env, thread):
    msgs = _messages(mailbox_env, thread, 2)
    _run_sync(monkeypatch, mailbox_env, lambda db: FakeMailbox(msgs, db))
    sql("DELETE FROM imap_sync_state WHERE inbox_id = :i", i=mailbox_env["inbox_id"])  # force a re-fetch
    ok, box, calls = _run_sync(monkeypatch, mailbox_env, lambda db: FakeMailbox(msgs, db))
    assert ok is True and box.fetched == [1, 2]
    assert calls == []  # deduplicated before any classification
    assert _stored(thread) == 2
