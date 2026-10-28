"""A4 repositories against MySQL: IMAP sync (inbox_sync.py), mailboxes (inbox_mailboxes.py), deliverability
(deliverability.py, deliverability_safety.py) and the alert centre (inbox_alerts.py) -- tenant isolation,
reply matching, the no-step cancel on reply, UID position upsert, mailbox delete, domain uniqueness,
per-tenant message attribution and the snapshot retention job."""
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant, sql
from app.core.clock import utcnow
from app.domain.inbox_sync import REPLY_STOP_REASON
from app.models.campaign import Campaign
from app.models.domain_reputation import DomainHealthSnapshot, SendingDomain
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.repositories.deliverability import DeliverabilityRepository, DeliverabilityUnitOfWork
from app.repositories.deliverability_safety import SafetyRepository
from app.repositories.inbox_alerts import AlertCenterRepository
from app.repositories.inbox_mailboxes import MailboxRepository
from app.repositories.inbox_sync import ImapSyncRepository
from app.services import domain_health_jobs


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def world(client):
    a, b = register_tenant(client, "a4ra"), register_tenant(client, "a4rb")
    owner_a = sql("SELECT user_id FROM users WHERE email = :e", e=a["email"])
    domain = f"a4repo{RUN}.example.com"
    with SessionLocal() as db:
        inbox_a = SendingInbox(tenant_id=a["tenant_id"], email_address=f"s@{domain}", imap_host="127.0.0.1")
        inbox_b = SendingInbox(tenant_id=b["tenant_id"], email_address=f"t@{domain}")
        pa = Prospect(tenant_id=a["tenant_id"], email=f"pat{RUN}@client.example.com")
        pb = Prospect(tenant_id=b["tenant_id"], email=f"pat{RUN}@client.example.com")
        camp = Campaign(tenant_id=a["tenant_id"], campaign_name="A4 repo", created_by=owner_a, status="ACTIVE")
        db.add_all([inbox_a, inbox_b, pa, pb, camp])
        db.commit()
        out = dict(a=a, b=b, domain=domain, inbox_a=inbox_a.inbox_id, inbox_b=inbox_b.inbox_id, pa=pa.prospect_id,
                   pb=pb.prospect_id, camp=camp.campaign_id, email=pa.email)
    return out


def test_find_prospect_is_tenant_scoped_and_case_insensitive(world):
    with SessionLocal() as db:
        repo = ImapSyncRepository(db)
        assert repo.find_prospect(world["a"]["tenant_id"], world["email"].upper()).prospect_id == world["pa"]
        assert repo.find_prospect(world["b"]["tenant_id"], world["email"]).prospect_id == world["pb"]
        assert repo.find_prospect(None, world["email"]) is None


def test_outbound_by_header_matches_the_bare_ses_id_for_this_contact_only(world):
    ses = f"0100{_uid()}"
    with SessionLocal() as db:
        db.add(EmailMessage(prospect_id=world["pa"], direction="OUTBOUND", status="SENT", subject="Hi",
                            to_email=world["email"], ses_message_id=ses, sent_at=utcnow()))
        db.commit()
        repo = ImapSyncRepository(db)
        header = f"<{ses}@email.amazonses.com>"
        assert repo.outbound_by_header(world["pa"], [header, ses]).ses_message_id == ses
        assert repo.outbound_by_header(world["pb"], [header, ses]) is None
        assert repo.outbound_by_header(world["pa"], [header]) is None


def test_cancel_on_reply_covers_mail_of_no_step_only(world):
    with SessionLocal() as db:
        step = EmailSequence(campaign_id=world["camp"], step_number=1, stop_on_reply=False)
        db.add(step)
        db.flush()

        def pending(seq):
            m = EmailMessage(campaign_id=world["camp"], prospect_id=world["pa"], sequence_id=seq,
                             direction="OUTBOUND", status="QUEUED", to_email=world["email"])
            db.add(m)
            return m
        stepped, automation = pending(step.sequence_id), pending(None)
        db.commit()
        ids = (stepped.message_id, automation.message_id)
        assert ImapSyncRepository(db).cancel_pending_without_step(world["camp"], world["pa"], REPLY_STOP_REASON) == 1
        db.commit()
    status = {i: sql("SELECT status FROM email_messages WHERE message_id = :i", i=i) for i in ids}
    assert status == {ids[0]: "QUEUED", ids[1]: "CANCELLED"}  # step mail follows the per-step rule
    assert sql("SELECT failure_reason FROM email_messages WHERE message_id = :i", i=ids[1]) == REPLY_STOP_REASON


def test_sync_position_upsert(world):
    with SessionLocal() as db:
        repo = ImapSyncRepository(db)
        assert repo.position(world["inbox_a"], "INBOX") is None
        repo.save_position(world["inbox_a"], "INBOX", 5, 10)
        db.commit()
        repo.save_position(world["inbox_a"], "INBOX", 5, 12)
        db.commit()
        position = repo.position(world["inbox_a"], "INBOX")
        assert (position.uidvalidity, position.last_uid) == (5, 12)
        assert any(i.inbox_id == world["inbox_a"] for i in repo.inboxes_to_sync())
        assert all(i.imap_host for i in repo.inboxes_to_sync())


def test_mailboxes_are_tenant_scoped_and_delete_keeps_mail(world):
    with SessionLocal() as db:
        repo = MailboxRepository(db)
        assert repo.get(world["b"]["tenant_id"], world["inbox_a"]) is None
        assert [i.inbox_id for i in repo.list(world["b"]["tenant_id"])] == [world["inbox_b"]]
        box = SendingInbox(tenant_id=world["a"]["tenant_id"], email_address=f"gone{_uid()}@{world['domain']}")
        db.add(box)
        db.flush()
        msg = EmailMessage(prospect_id=world["pa"], inbox_id=box.inbox_id, direction="OUTBOUND", status="SENT",
                           to_email=world["email"])
        db.add(msg)
        db.commit()
        repo.delete(repo.get(world["a"]["tenant_id"], box.inbox_id))
        db.commit()
        assert sql("SELECT inbox_id FROM email_messages WHERE message_id = :m", m=msg.message_id) is None
        assert repo.get(world["a"]["tenant_id"], box.inbox_id) is None


def test_domains_per_tenant_unique_and_sent_log_attribution(world):
    name = f"d{_uid()}.example.com"
    with SessionLocal() as db:
        repo = DeliverabilityRepository(db)
        assert repo.insert_domain(SendingDomain(tenant_id=world["a"]["tenant_id"], domain_name=name))
        assert repo.insert_domain(SendingDomain(tenant_id=world["b"]["tenant_id"], domain_name=name))
        db.commit()
        assert not repo.insert_domain(SendingDomain(tenant_id=world["a"]["tenant_id"], domain_name=name))
        db.rollback()
        assert repo.get_domain(world["a"]["tenant_id"], name.upper()).tenant_id == world["a"]["tenant_id"]
        assert sorted(repo.tenants_with_domain(name)) == sorted([world["a"]["tenant_id"], world["b"]["tenant_id"]])
        db.add_all([EmailMessage(prospect_id=world["pa"], inbox_id=world["inbox_a"], direction="OUTBOUND",
                                 status="SENT", to_email=world["email"], from_email=f"s@{world['domain']}",
                                 sent_at=utcnow()),
                    EmailMessage(prospect_id=world["pb"], inbox_id=world["inbox_b"], direction="OUTBOUND",
                                 status="SENT", to_email=world["email"], from_email=f"t@{world['domain']}",
                                 sent_at=utcnow())])
        db.commit()
        log_a = repo.sent_log(world["a"]["tenant_id"], world["domain"], 50)
        assert log_a and all(m.inbox_id == world["inbox_a"] for m, _ in log_a)
        sends = dict(repo.daily_sends(world["a"]["tenant_id"], world["domain"], utcnow() - timedelta(days=1)))
        assert sum(sends.values()) == len([m for m, _ in log_a if m.sent_at])
        outcomes = SafetyRepository(db).domain_outcomes(world["b"]["tenant_id"], world["domain"],
                                                        utcnow() - timedelta(days=1))
        assert outcomes[0] == 1


def test_snapshot_job_writes_once_per_hour_and_purges_after_90_days(world):
    name = f"snap{_uid()}.example.com"
    with SessionLocal() as db:
        row = SendingDomain(tenant_id=world["a"]["tenant_id"], domain_name=name, current_reputation_score=77)
        db.add(row)
        db.flush()
        db.add(DomainHealthSnapshot(domain_id=row.domain_id, tenant_id=row.tenant_id, domain_name=name,
                                    reputation_score=50, snapshot_at=utcnow() - timedelta(days=91)))
        db.commit()
        domain_id = row.domain_id
    first = domain_health_jobs.take_snapshots(DeliverabilityUnitOfWork(SessionLocal))
    assert first["snapshots"] >= 1 and first["purged"] >= 1
    domain_health_jobs.take_snapshots(DeliverabilityUnitOfWork(SessionLocal))
    rows = sql("SELECT COUNT(*) FROM domain_health_snapshots WHERE domain_id = :d", d=domain_id)
    assert rows == 1  # the old one purged, one new one, none doubled within the hour
    assert sql("SELECT reputation_score FROM domain_health_snapshots WHERE domain_id = :d", d=domain_id) == 77


def test_alert_centre_reads_are_tenant_scoped(world):
    with SessionLocal() as db:
        repo = AlertCenterRepository(db)
        assert repo.inbox_ids(world["a"]["tenant_id"]) & {world["inbox_b"]} == set()
        assert [e for _, e in repo.admin_recipients(world["b"]["tenant_id"])] == [world["b"]["email"]]
        assert {world["a"]["tenant_id"], world["b"]["tenant_id"]} <= repo.alerting_tenant_ids()
