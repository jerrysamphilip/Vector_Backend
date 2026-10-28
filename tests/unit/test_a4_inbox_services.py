"""
A4 services with fake units of work (count enter / commit) and dict-backed repositories:
InboxService (app/services/inbox_service.py), SendingDomainService (deliverability_service.py),
ConversationService and the domain-health snapshot job (domain_health_jobs.py).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, ValidationFailed
from app.domain.inbox import ConversationQuery, OwnerScope
from app.schemas.inbox_schema import SendingInboxCreate, SendingInboxUpdate
from app.services import domain_health_jobs, inbox_service
from app.services.conversation_service import ConversationService
from app.services.deliverability_service import DomainCreateRejected, SendingDomainService
from app.services.inbox_service import InboxService, MailSyncFailed

USER = SimpleNamespace(user_id="u1", tenant_id="t1", role="ADMIN", email="a@x.io")


class FakeUow:
    def __init__(self, **repos):
        self.__dict__.update(repos)
        self.session = SimpleNamespace()
        self.enters = self.commits = self.rollbacks = 0
        self.active = False

    def __enter__(self):
        self.enters += 1
        self.active = True
        return self

    def __exit__(self, *exc):
        self.active = False
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ── InboxService ──────────────────────────────────────────────────────────────
class Mailboxes:
    def __init__(self, *inboxes):
        self.rows = {i.inbox_id: i for i in inboxes}
        self.deleted = []

    def get(self, tenant_id, inbox_id):
        row = self.rows.get(inbox_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def by_email(self, tenant_id, address):
        return next((r for r in self.rows.values() if r.email_address == address and r.tenant_id == tenant_id), None)

    def add(self, inbox):
        self.rows[inbox.inbox_id] = inbox
        return inbox

    def refresh(self, entity):
        pass

    def delete(self, inbox):
        self.deleted.append(inbox.inbox_id)


def _mailbox(**kw):
    base = dict(inbox_id="i1", tenant_id="t1", email_address="me@acme.io", imap_host="imap.acme.io",
                imap_password="pw", imap_port=993, imap_username=None, last_sync_at=None, auth_type="PASSWORD",
                smtp_host="smtp.acme.io", smtp_password="pw", smtp_port=587, smtp_use_ssl=False,
                smtp_username=None, warmup_start_date=None, imap_last_error="old")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    checked = []
    monkeypatch.setattr(inbox_service, "check_mail_host", lambda host: checked.append(host))
    return checked


def test_create_refuses_duplicates_and_verifies_domain(public_hosts):
    uow = FakeUow(inboxes=Mailboxes(_mailbox()))
    verified = []
    svc = InboxService(uow, domain_verifier=lambda session, tenant, domain: verified.append(domain) or {"ok": 1})
    with pytest.raises(BadRequestError, match="already exists"):
        svc.create(USER, SendingInboxCreate(email_address="me@acme.io", smtp_host="s.io", imap_host="i.io"))
    assert public_hosts == ["s.io", "i.io"] and uow.commits == 0
    out = svc.create(USER, SendingInboxCreate(email_address="new@other.io"))
    assert out.email_address == "new@other.io" and out.domain_verification == {"ok": 1}
    assert verified == ["other.io"] and uow.commits == 1


def test_update_keeps_secrets_checks_ports_and_hosts(public_hosts):
    inbox = _mailbox()
    uow = FakeUow(inboxes=Mailboxes(inbox))
    svc = InboxService(uow)
    with pytest.raises(ValidationFailed, match="smtp_port"):
        svc.update(USER, "i1", SendingInboxUpdate(smtp_port=0))
    assert uow.commits == 0
    svc.update(USER, "i1", SendingInboxUpdate(imap_password="", imap_host=" new.acme.io ", warmup_enabled=True))
    assert inbox.imap_password == "pw" and inbox.imap_host == "new.acme.io" and inbox.imap_last_error is None
    assert inbox.warmup_status == "ACTIVE" and inbox.warmup_day == 0 and public_hosts == ["new.acme.io"]
    with pytest.raises(NotFoundError):
        svc.update(SimpleNamespace(tenant_id="t2"), "i1", SendingInboxUpdate())


def test_sync_history_runs_outside_the_unit_of_work():
    inbox = _mailbox()
    uow = FakeUow(inboxes=Mailboxes(inbox))

    class Sync:
        def sync_inbox(self, row, days_back):
            assert not uow.active and days_back == 5
            row.last_sync_at = datetime(2026, 10, 1)
            return True
    out = InboxService(uow, imap_sync=Sync).sync_history(USER, "i1", 5)
    assert out["last_sync_at"] == "2026-10-01T00:00:00" and out["inbox"] == "me@acme.io"

    class Failing:
        def sync_inbox(self, row, days_back):
            return False
    with pytest.raises(MailSyncFailed) as exc:
        InboxService(uow, imap_sync=Failing).sync_history(USER, "i1", 5)
    assert exc.value.status_code == 502
    with pytest.raises(BadRequestError, match="IMAP is not configured"):
        InboxService(FakeUow(inboxes=Mailboxes(_mailbox(imap_password=None))), imap_sync=Sync).sync_history(USER, "i1", 5)


def test_test_endpoints_report_missing_settings_without_connecting():
    class NoConnect:
        def open_imap(self, *a, **k):
            raise AssertionError("not configured: no connection")
        open_smtp = open_imap
    svc = InboxService(FakeUow(inboxes=Mailboxes(_mailbox(imap_host=None, smtp_password=None))), connector=NoConnect())
    assert svc.test_imap(USER, "i1")["status"] == "not_configured"
    assert svc.test_smtp(USER, "i1") == {"status": "not_configured", "error": "SMTP password is not set."}


def test_delete_goes_through_the_repository():
    boxes = Mailboxes(_mailbox())
    uow = FakeUow(inboxes=boxes)
    InboxService(uow).delete(USER, "i1")
    assert boxes.deleted == ["i1"] and uow.commits == 1
    with pytest.raises(NotFoundError):
        InboxService(uow).delete(USER, "nope")


# ── SendingDomainService ──────────────────────────────────────────────────────
class Domains:
    def __init__(self, *names, count=0, clash=False):
        self.rows = {n: SimpleNamespace(domain_name=n, dkim_status="UNKNOWN") for n in names}
        self.count, self.clash, self.deleted, self.inserted = count, clash, [], []

    def get_domain(self, tenant_id, name):
        return self.rows.get(name)

    def count_domains(self, tenant_id):
        return self.count

    def insert_domain(self, row):
        if self.clash:
            return False
        self.inserted.append(row.domain_name)
        self.rows[row.domain_name] = row
        return True

    def delete_domain(self, row):
        self.deleted.append(row.domain_name)

    def external_metrics(self, tenant_id, provider, name, limit):
        return [(tenant_id, provider, name, limit)]


class Engine:
    def __init__(self, tokens=()):
        self.tokens = list(tokens)

    def dkim_tokens(self, name, create):
        return self.tokens

    def get_all_domains_enriched(self, session, tenant_id):
        return [{"domain_name": "acme.io"}]


def test_add_domain_validates_and_marks_dkim_pending():
    repo = Domains()
    uow = FakeUow(domains=repo)
    svc = SendingDomainService(uow, engine=Engine(["tok"]))
    with pytest.raises(DomainCreateRejected):
        svc.add(USER, "http://acme.io/path")
    out = svc.add(USER, " ACME.io ")
    assert repo.inserted == ["acme.io"] and repo.rows["acme.io"].dkim_status == "PENDING"
    assert out["domain"] == {"domain_name": "acme.io"} and out["dkim_tokens_available"] is True
    assert uow.commits == 2
    with pytest.raises(ConflictError):
        svc.add(USER, "acme.io")
    with pytest.raises(ConflictError):
        SendingDomainService(FakeUow(domains=Domains(clash=True)), engine=Engine()).add(USER, "b.io")
    with pytest.raises(BadRequestError, match="at most 200"):
        SendingDomainService(FakeUow(domains=Domains(count=200)), engine=Engine()).add(USER, "c.io")


def test_domain_reads_require_the_tenant_domain():
    svc = SendingDomainService(FakeUow(domains=Domains()), engine=Engine(),
                               has_domain=lambda session, tenant, name: name == "acme.io")
    assert svc.external_metrics(USER, "snds", " ACME.io", 5) == [("t1", "SNDS", "acme.io", 5)]
    with pytest.raises(NotFoundError):
        svc.external_metrics(USER, "snds", "other.io", 5)
    with pytest.raises(NotFoundError):
        SendingDomainService(FakeUow(domains=Domains()), engine=Engine()).delete(USER, "acme.io")


def test_alert_preference_errors_are_400():
    class Alerts:
        def upsert_preferences(self, session, tenant_id, user_id, items):
            raise ValueError("Unsupported alert_type: X")
    svc = SendingDomainService(FakeUow(domains=Domains()), alerts=Alerts())
    with pytest.raises(BadRequestError, match="Unsupported alert_type"):
        svc.save_alert_preferences(USER, [{"alert_type": "X"}])


# ── ConversationService ───────────────────────────────────────────────────────
class Conversations:
    def __init__(self):
        self.seen = None

    def page(self, tenant_id, query, scope):
        self.seen = (tenant_id, query, scope)
        return [], 0

    def last_messages(self, ids):
        return {}

    latest_campaigns = latest_reply_labels = last_messages

    def get(self, tenant_id, conversation_id, lock=False):
        prospect = SimpleNamespace(owner_id="someone-else")
        return SimpleNamespace(id="c1", prospect=prospect, is_unread=True)


def test_conversations_use_the_owner_scope():
    repo = Conversations()
    scope = OwnerScope(frozenset({"u1"}), include_unowned=False)
    svc = ConversationService(FakeUow(conversations=repo), scope_of=lambda session, user: scope)
    out = svc.list_conversations(USER, ConversationQuery())
    assert out.total == 0 and repo.seen == ("t1", ConversationQuery(), scope)
    assert svc.get_conversation_thread(USER, "c1") is None  # another owner's contact
    assert svc.create_reply(USER, "c1", "hi") is None


# ── Domain-health snapshots (C-10) ────────────────────────────────────────────
def test_snapshot_job_skips_recent_and_purges_old():
    now = datetime(2026, 10, 27, 12, 0)

    class Repo:
        def __init__(self):
            self.added, self.cutoffs = [], []

        def snapshotted_since(self, since):
            assert since == now - timedelta(minutes=55)
            return {"d1"}

        def owned_domains(self):
            return [SimpleNamespace(domain_id=d, tenant_id="t1", domain_name=f"{d}.io", current_reputation_score=80,
                                    bounce_rate_24h=None, complaint_rate_24h=0.01) for d in ("d1", "d2")]

        def add(self, entity):
            self.added.append(entity)

        def delete_snapshots_before(self, cutoff):
            self.cutoffs.append(cutoff)
            return 3

    repo = Repo()
    uow = FakeUow(domains=repo)
    assert domain_health_jobs.take_snapshots(uow, now) == {"snapshots": 1, "purged": 3}
    snap = repo.added[0]
    assert (snap.domain_id, snap.reputation_score, snap.bounce_rate_24h, snap.complaint_rate_24h) == ("d2", 80, 0.0, 0.01)
    assert repo.cutoffs == [now - timedelta(days=90)] and uow.commits == 1
