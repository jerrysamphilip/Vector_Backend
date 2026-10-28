"""Agent follow: the SES "Delivery Failed" note is a system notice everywhere, not only in the thread view.

One contact gets one sent step email, one real reply (POSITIVE) and three bounce notes: the current form
(MAILER-DAEMON sender, reply_label_source SYSTEM), a MAILER-DAEMON note with a stray classifier label, and a
legacy note (the contact as sender, classified NEGATIVE, recognised by its fixed wording). Every reply count,
reply list and label lookup in SQL must see exactly the one real reply; the schedule lists only the sent email.
"""
from datetime import datetime, timedelta

import pytest

from app.domain.inbox import SYSTEM_NOTICE_SENDER
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.repositories.accounts import SqlAlchemyAccountRepository
from app.repositories.campaigns_reads import CampaignReadsRepository
from app.repositories.contact_scoring import SqlAlchemyScoringRepository
from app.repositories.contacts import SqlAlchemyContactRepository
from app.repositories.inbox import ConversationRepository
from app.repositories.reports_sales import SqlAlchemySalesReportRepository
from app.repositories.sales import SqlAlchemySalesLookupRepository
from tests.conftest import RUN, SessionLocal, register_tenant, sql

T0 = datetime(2026, 10, 20, 15, 0, 0)
REPLY_AT = T0 + timedelta(hours=1)


def _note(conv, pid, inbox_id, campaign_id, at, **kw):
    return EmailMessage(conversation_id=conv.id, prospect_id=pid, inbox_id=inbox_id, campaign_id=campaign_id,
                        direction="INBOUND", status="RECEIVED", sent_at=at, to_email="amy@example.com",
                        subject="Delivery Failed: Vera, a quick idea",
                        body_text="Email delivery failed (Permanent: General). smtp; 550 5.1.1", **kw)


@pytest.fixture(scope="module")
def thread(client):
    t = register_tenant(client, "fsn")
    h = t["headers"]
    sender = f"amy@fsn{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": sender, "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "s", "smtp_password": "fixture-smtp-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    campaign_id = client.post("/campaigns", headers=h, json={"campaign_name": f"FSN {RUN}"}).json()["campaign_id"]
    contact = f"vera.{RUN}@fsn-contact.example.com"
    r = client.post("/contacts", headers=h, json={"email": contact, "first_name": "Vera", "last_name": "N",
                                                  "company": f"FSN Co {RUN}"})
    assert r.status_code == 201, r.text
    pid = r.json()["prospect_id"]
    with SessionLocal() as db:
        seq = EmailSequence(campaign_id=campaign_id, tenant_id=t["tenant_id"], step_number=1)
        db.add(seq)
        conv = Conversation(tenant_id=t["tenant_id"], prospect_id=pid, inbox_id=inbox_id,
                            subject="Vera, a quick idea", last_message_at=T0)
        db.add(conv)
        db.flush()
        sent = EmailMessage(conversation_id=conv.id, prospect_id=pid, inbox_id=inbox_id, campaign_id=campaign_id,
                            sequence_id=seq.sequence_id, direction="OUTBOUND", status="SENT", to_email=contact,
                            from_email=sender, subject="Vera, a quick idea", body_text="Hi", sent_at=T0)
        reply = EmailMessage(conversation_id=conv.id, prospect_id=pid, inbox_id=inbox_id, campaign_id=campaign_id,
                             direction="INBOUND", status="RECEIVED", from_email=contact, to_email=sender,
                             subject="Re: Vera, a quick idea", body_text="Sounds good, call me", sent_at=REPLY_AT,
                             reply_label="POSITIVE", reply_label_source="RULES", reply_classified_at=REPLY_AT)
        notes = [
            _note(conv, pid, inbox_id, campaign_id, T0 + timedelta(hours=2), from_email=SYSTEM_NOTICE_SENDER,
                  reply_label_source="SYSTEM", reply_classified_at=T0),
            _note(conv, pid, inbox_id, campaign_id, T0 + timedelta(hours=3), from_email=SYSTEM_NOTICE_SENDER.lower(),
                  reply_label="NEGATIVE", reply_label_source="RULES"),
            _note(conv, pid, inbox_id, campaign_id, T0 + timedelta(hours=4), from_email=contact,
                  reply_label="NEGATIVE", reply_label_source="RULES"),
        ]
        db.add_all([sent, reply, *notes])
        db.commit()
        ids = {"seq": seq.sequence_id, "conv": conv.id, "sent": sent.message_id, "reply": reply.message_id,
               "notes": {n.message_id for n in notes}}
    return {"t": t, "h": h, "pid": pid, "campaign_id": campaign_id, **ids}


def test_contact_stats_count_only_the_real_reply(thread):
    with SessionLocal() as db:
        stats = SqlAlchemyContactRepository(db).stats(thread["pid"])
    assert (stats.replies, stats.last_reply_at) == (1, REPLY_AT)


def test_contact_emails_leave_out_notices(thread):
    with SessionLocal() as db:
        rows = SqlAlchemyContactRepository(db).emails_of(thread["pid"], 50)
    assert {m.message_id for m, _ in rows} == {thread["sent"], thread["reply"]}


def test_account_email_counts(thread):
    with SessionLocal() as db:
        assert SqlAlchemyAccountRepository(db).email_counts([thread["pid"]]) == (1, 1)


def test_sales_report_replied_contacts(thread):
    with SessionLocal() as db:
        repo = SqlAlchemySalesReportRepository(db)
        assert repo.email_counts(thread["t"]["tenant_id"], None, T0, T0 + timedelta(days=1)) == (1, 1)
        # Only the notes in the window: nobody replied
        assert repo.email_counts(thread["t"]["tenant_id"], None, T0 + timedelta(hours=2),
                                 T0 + timedelta(days=1)) == (0, 0)


def test_sales_latest_reply_is_the_contact_reply(thread):
    with SessionLocal() as db:
        latest = SqlAlchemySalesLookupRepository(db).latest_reply(thread["pid"])
    assert latest is not None and latest.message_id == thread["reply"]


def test_scoring_reads_only_replies(thread):
    with SessionLocal() as db:
        rows = list(SqlAlchemyScoringRepository(db).inbound_replies([thread["pid"]], T0, 200, 50))
    assert [(r[0], r[1]) for r in rows] == [(thread["pid"], REPLY_AT)]


def test_step_replies_leave_out_notices(client, thread):
    with SessionLocal() as db:
        rows = CampaignReadsRepository(db).recent_step_replies(thread["campaign_id"], thread["seq"], 10)
    assert [m.message_id for m in rows] == [thread["reply"]]
    r = client.get(f"/campaigns/{thread['campaign_id']}/sequences/1/emails", headers=thread["h"])
    assert r.status_code == 200, r.text
    assert [x["received_at"] for x in r.json()["recent_replies"]] == ["2026-10-20T16:00:00Z"]


def test_latest_reply_label_ignores_labelled_notices(thread):
    with SessionLocal() as db:
        labels = ConversationRepository(db).latest_reply_labels([thread["conv"]])
    assert labels == {thread["conv"]: ("POSITIVE", False)}


def test_schedule_lists_only_the_sent_email(client, thread):
    r = client.get(f"/campaigns/{thread['campaign_id']}/schedule", headers=thread["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    prospects = [p for bucket in body["schedule"] for p in bucket["prospects"]]
    assert [p["message_id"] for p in prospects] == [thread["sent"]]
    assert prospects[0]["sent_at"] == "2026-10-20T15:00:00Z"
    assert (body["total_sent"], body["total_scheduled"]) == (1, 0)


def test_opening_a_thread_keeps_last_message_at(client, thread):
    """Marking the thread read is an UPDATE of the conversation; it must not move last_message_at to now."""
    assert sql("SELECT is_unread FROM conversations WHERE id = :c", c=thread["conv"]) == 1
    r = client.get(f"/conversations/{thread['conv']}", headers=thread["h"])
    assert r.status_code == 200, r.text
    assert sql("SELECT is_unread FROM conversations WHERE id = :c", c=thread["conv"]) == 0
    assert sql("SELECT last_message_at FROM conversations WHERE id = :c", c=thread["conv"]) == T0
