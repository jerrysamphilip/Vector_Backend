"""
HOOK fixes, in-process against MySQL:
  4   an automation rule only sends a template of its own workspace (F058), changes need manage_campaigns (F038)
  6   the SES webhook caps the body (413), parses JSON itself (400) and never echoes internals (500)
  15b a Delivery / Bounce that arrives while the email is still SENDING is recorded
  17  replies are threaded by SES's rewritten Message-ID, and by subject within contact + inbox
  19b an automation rule acts on a contact once, enforced by a unique key
  20b stop on reply honours each step's stop_on_reply flag
"""
import email.message
import threading
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, sql
from tests.test_imap_sync_transactions import FakeMailbox, _run_sync
from app.core.clock import utcnow
from app.models.automation_rule import AutomationRule
from app.models.automation_rule_run import AutomationRuleRun
from app.models.campaign import CampaignProspect
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.routers import ses_webhook_router
from app.services.automation_rule_service import automation_service, claim_rule_run

WEBHOOK = "/webhooks/ses/notifications"


# ── helpers ──

def _campaign(client, h, label):
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Hook {label} {RUN}"})
    assert r.status_code == 201, r.text
    return r.json()["campaign_id"]


def _template(cid, subject="Offer"):
    with SessionLocal() as db:
        t = EmailTemplate(campaign_id=cid, subject=subject, body="Hi {{first_name}}")
        db.add(t)
        db.commit()
        return t.template_id


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={"email": f"{label}.{uuid.uuid4().hex[:6]}@hook{RUN}.example.com",
                                                  "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"], r.json()["email"]


def _rule_body(cid, template_id, action_type="SEND_EMAIL"):
    return {"name": "Offer on click", "trigger_type": "EMAIL_CLICKED", "action_type": action_type,
            "campaign_id": cid, "action_config": {"template_id": template_id} if template_id else {}}


# ── 4. Automation templates belong to the caller's workspace ──

def test_rule_with_foreign_template_is_refused(client, tenant_a, tenant_b):
    ha = tenant_a["headers"]
    cid_a = _campaign(client, ha, "rule-a")
    own = _template(cid_a)
    foreign = _template(_campaign(client, tenant_b["headers"], "rule-b"))

    r = client.post("/automation-rules/", headers=ha, json=_rule_body(cid_a, foreign))
    assert r.status_code == 404, r.text
    r = client.post("/automation-rules/", headers=ha, json=_rule_body(cid_a, f"missing-{uuid.uuid4()}"))
    assert r.status_code == 404
    r = client.post("/automation-rules/", headers=ha, json=_rule_body(cid_a, None))
    assert r.status_code == 400  # SEND_EMAIL needs a template

    r = client.post("/automation-rules/", headers=ha, json=_rule_body(cid_a, own))
    assert r.status_code == 201, r.text
    rule_id = r.json()["rule_id"]
    r = client.put(f"/automation-rules/{rule_id}", headers=ha, json={"action_config": {"template_id": foreign}})
    assert r.status_code == 404
    assert sql("SELECT JSON_UNQUOTE(JSON_EXTRACT(action_config, '$.template_id')) FROM automation_rules "
               "WHERE rule_id = :r", r=rule_id) == own
    r = client.put(f"/automation-rules/{rule_id}", headers=ha, json={"description": "kept"})
    assert r.status_code == 200
    # Other actions need no template
    r = client.post("/automation-rules/", headers=ha, json=_rule_body(cid_a, None, "PAUSE_PROSPECT"))
    assert r.status_code == 201, r.text


def test_rule_changes_need_manage_campaigns(client, tenant_a):
    ha = tenant_a["headers"]
    cid = _campaign(client, ha, "perm")
    rule_id = client.post("/automation-rules/", headers=ha, json=_rule_body(cid, None, "PAUSE_PROSPECT")).json()["rule_id"]
    user_id = sql("SELECT user_id FROM users WHERE email = :e", e=tenant_a["email"])
    # A MANAGER without manage_campaigns (custom permissions) cannot change rules; reading still works
    sql("UPDATE users SET role = 'MANAGER', custom_permissions = JSON_ARRAY('view_analytics') "
        "WHERE user_id = :u", u=user_id)
    try:
        assert client.post("/automation-rules/", headers=ha,
                           json=_rule_body(cid, None, "PAUSE_PROSPECT")).status_code == 403
        assert client.put(f"/automation-rules/{rule_id}", headers=ha, json={"description": "x"}).status_code == 403
        assert client.delete(f"/automation-rules/{rule_id}", headers=ha).status_code == 403
        assert client.get("/automation-rules/", headers=ha, params={"campaign_id": cid}).status_code == 200
        # The role defaults include it
        sql("UPDATE users SET custom_permissions = NULL WHERE user_id = :u", u=user_id)
        assert client.put(f"/automation-rules/{rule_id}", headers=ha, json={"description": "y"}).status_code == 200
    finally:
        sql("UPDATE users SET role = 'SUPER_ADMIN', custom_permissions = NULL WHERE user_id = :u", u=user_id)


def test_stored_rule_with_foreign_template_does_not_send(client, tenant_a, tenant_b):
    ha = tenant_a["headers"]
    cid = _campaign(client, ha, "exec")
    foreign = _template(_campaign(client, tenant_b["headers"], "exec-b"))
    pid, _ = _contact(client, ha, "exec")
    with SessionLocal() as db:
        # Saved before the API checked templates
        rule = AutomationRule(campaign_id=cid, name="Legacy", trigger_type="EMAIL_CLICKED",
                              action_type="SEND_EMAIL", action_config={"template_id": foreign})
        db.add(rule)
        db.flush()
        automation_service._action_send_email(rule, pid, db)
        db.commit()
        rule_id = rule.rule_id
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c", c=cid) == 0
    assert sql("SELECT COUNT(*) FROM automation_rule_runs WHERE rule_id = :r", r=rule_id) == 0


# ── 19b. Once per rule and contact ──

def test_rule_runs_once_per_contact_under_two_inserts(client, tenant_a):
    ha = tenant_a["headers"]
    cid = _campaign(client, ha, "once")
    pid, _ = _contact(client, ha, "once")
    rule_id = str(uuid.uuid4())
    tid = tenant_a["tenant_id"]

    # Two sessions at the same time: the second insert waits for the first, then loses
    first = SessionLocal()
    result = {}
    try:
        assert claim_rule_run(first, rule_id, pid, tid) is True

        def second():
            with SessionLocal() as db:
                result["second"] = claim_rule_run(db, rule_id, pid, tid)
                db.commit()
        t = threading.Thread(target=second)
        t.start()
        t.join(timeout=1.0)
        assert t.is_alive()  # blocked on the unique key held by the uncommitted first insert
        first.commit()
        t.join(timeout=30)
        assert result["second"] is False
    finally:
        first.close()
    assert sql("SELECT COUNT(*) FROM automation_rule_runs WHERE rule_id = :r AND prospect_id = :p",
               r=rule_id, p=pid) == 1

    # A lost claim undoes only the run row: the caller's other work in the transaction survives
    with SessionLocal() as db:
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, status="CANCELLED", to_email="x@example.com"))
        assert claim_rule_run(db, rule_id, pid, tid) is False
        db.commit()
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c", c=cid) == 1


def test_send_email_rule_queues_once_even_after_the_first_mail_is_cancelled(client, tenant_a):
    ha = tenant_a["headers"]
    cid = _campaign(client, ha, "once-send")
    tpl = _template(cid)
    pid, _ = _contact(client, ha, "oncesend")
    with SessionLocal() as db:
        rule = AutomationRule(campaign_id=cid, name="Offer", trigger_type="EMAIL_CLICKED",
                              action_type="SEND_EMAIL", action_config={"template_id": tpl})
        db.add(rule)
        db.flush()
        automation_service._action_send_email(rule, pid, db)
        db.commit()
        # The template check alone would queue again once the first mail is CANCELLED
        db.query(EmailMessage).filter(EmailMessage.campaign_id == cid).update({"status": "CANCELLED"})
        db.commit()
        automation_service._action_send_email(rule, pid, db)
        db.commit()
        assert db.query(AutomationRuleRun).filter(AutomationRuleRun.rule_id == rule.rule_id).count() == 1
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c", c=cid) == 1


# ── 6. Webhook body cap, JSON, generic errors ──

def test_webhook_rejects_oversized_body(client):
    big = b'{"notificationType": "Delivery", "pad": "' + b"a" * (ses_webhook_router.MAX_WEBHOOK_BODY_BYTES + 1) + b'"}'
    r = client.post(WEBHOOK, content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_webhook_rejects_invalid_json(client):
    r = client.post(WEBHOOK, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid JSON payload"


def test_webhook_error_is_generic(client, monkeypatch):
    async def boom(message, db):
        raise RuntimeError("Unknown column 'secret_col' in /srv/app/x.py")
    monkeypatch.setattr(ses_webhook_router, "_handle_delivery", boom)
    r = client.post(WEBHOOK, json={"notificationType": "Delivery", "mail": {}, "delivery": {}})
    assert r.status_code == 500
    assert "secret_col" not in r.text and r.json()["detail"] == "Error processing notification"


# ── 15b. Outcome events while the email is still SENDING ──

def _sending_message(client, h, label):
    pid, addr = _contact(client, h, label)
    ses_id = f"0100-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        m = EmailMessage(prospect_id=pid, direction="OUTBOUND", status="SENDING", to_email=addr,
                         from_email=f"Sender Name <sender@hook{RUN}.example.com>", ses_message_id=ses_id,
                         claimed_at=utcnow())
        db.add(m)
        db.commit()
        return m.message_id, ses_id, addr


def _mail(ses_id):
    return {"messageId": ses_id, "source": f"Sender Name <sender@hook{RUN}.example.com>", "tags": {}}


def test_delivery_during_sending_is_recorded(client, tenant_a):
    mid, ses_id, addr = _sending_message(client, tenant_a["headers"], "deliv")
    r = client.post(WEBHOOK, json={"notificationType": "Delivery", "mail": _mail(ses_id),
                                   "delivery": {"recipients": [addr], "timestamp": "2026-10-27T10:00:00.000Z"}})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        m = db.get(EmailMessage, mid)
        assert m.status == "SENDING"           # the scheduler moves it to SENT itself
        assert m.delivered_at is not None and m.final_status == "DELIVERED"
        assert db.query(EmailEvent).filter(EmailEvent.message_id == mid,
                                           EmailEvent.event_type == EmailEvent.EVENT_DELIVERED).count() == 1


def test_bounce_during_sending_is_recorded(client, tenant_a):
    mid, ses_id, addr = _sending_message(client, tenant_a["headers"], "bounce")
    r = client.post(WEBHOOK, json={"notificationType": "Bounce", "mail": _mail(ses_id), "bounce": {
        "bounceType": "Permanent", "bounceSubType": "General", "timestamp": "2026-10-27T10:00:01.000Z",
        "bouncedRecipients": [{"emailAddress": addr, "diagnosticCode": "550 5.1.1 user unknown"}]}})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        m = db.get(EmailMessage, mid)
        assert (m.status, m.final_status) == ("BOUNCED", "BOUNCED")


# ── 17 / 20b. Reply threading and per-step stop on reply ──

@pytest.fixture(scope="module")
def hook_inbox(client, tenant_a):
    domain = f"hookimap{RUN}.example.com"
    r = client.post("/inboxes", headers=tenant_a["headers"], json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass",
        "imap_host": "127.0.0.1", "imap_port": 993, "imap_username": "sender",
        "imap_password": "fixture-imap-pass", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    return {"tenant": tenant_a, "inbox_id": r.json()["inbox_id"], "inbox_email": f"sender@{domain}"}


def _reply(prospect_email, inbox_email, subject, in_reply_to=None, references=None) -> bytes:
    m = email.message.EmailMessage()
    m["From"] = prospect_email
    m["To"] = inbox_email
    m["Subject"] = subject
    m["Date"] = "Mon, 26 Oct 2026 10:00:00 +0000"
    m["Message-ID"] = f"<reply-{uuid.uuid4().hex}@client.example.com>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content("Thanks, tell me more.")
    return m.as_bytes()


def _thread_two_sent(client, env, campaign_id=None, sequences=(None, None), subjects=("Hello", "Other")):
    """A contact with a conversation holding two sent emails: the older one is the one replied to."""
    pid, addr = _contact(client, env["tenant"]["headers"], "thread")
    ses_id = f"0102018{uuid.uuid4().hex}"
    with SessionLocal() as db:
        conv = Conversation(tenant_id=env["tenant"]["tenant_id"], prospect_id=pid, inbox_id=env["inbox_id"],
                            subject=subjects[0], last_message_at=utcnow())
        db.add(conv)
        db.flush()
        now = utcnow()
        older = EmailMessage(prospect_id=pid, inbox_id=env["inbox_id"], conversation_id=conv.id,
                             campaign_id=campaign_id, sequence_id=sequences[0], direction="OUTBOUND",
                             status="SENT", subject=subjects[0], to_email=addr, from_email=env["inbox_email"],
                             provider_message_id=f"<our-{uuid.uuid4().hex}@hookimap.example.com>",
                             ses_message_id=ses_id, sent_at=now - timedelta(days=2))
        newer = EmailMessage(prospect_id=pid, inbox_id=env["inbox_id"], conversation_id=conv.id,
                             campaign_id=campaign_id, sequence_id=sequences[1], direction="OUTBOUND",
                             status="SENT", subject=subjects[1], to_email=addr, from_email=env["inbox_email"],
                             provider_message_id=f"<our-{uuid.uuid4().hex}@hookimap.example.com>",
                             ses_message_id=f"0102018{uuid.uuid4().hex}", sent_at=now)
        db.add_all([older, newer])
        db.commit()
        ids = {"pid": pid, "addr": addr, "ses_id": ses_id, "older": older.message_id, "newer": newer.message_id,
               "older_provider": older.provider_message_id, "conv": conv.id}
    sql("DELETE FROM imap_sync_state WHERE inbox_id = :i", i=env["inbox_id"])
    sql("UPDATE sending_inboxes SET last_sync_at = NULL WHERE inbox_id = :i", i=env["inbox_id"])
    return ids


def _reply_events(message_id):
    return sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m AND event_type = :t",
               m=message_id, t=EmailEvent.EVENT_REPLY)


def _sync(monkeypatch, env, raw):
    ok, _, _ = _run_sync(monkeypatch, env, lambda db: FakeMailbox({1: raw}, db))
    assert ok is True


@pytest.mark.parametrize("domain", ["email.amazonses.com", "eu-west-1.amazonses.com"])
def test_reply_threaded_by_ses_message_id(client, monkeypatch, hook_inbox, domain):
    t = _thread_two_sent(client, hook_inbox)
    raw = _reply(t["addr"], hook_inbox["inbox_email"], "Re: Hello", in_reply_to=f"<{t['ses_id']}@{domain}>")
    _sync(monkeypatch, hook_inbox, raw)
    assert _reply_events(t["older"]) == 1 and _reply_events(t["newer"]) == 0
    assert sql("SELECT conversation_id FROM email_messages WHERE prospect_id = :p AND direction = 'INBOUND'",
               p=t["pid"]) == t["conv"]


def test_reply_threaded_by_bare_ses_id_from_send_now(client, monkeypatch, hook_inbox):
    t = _thread_two_sent(client, hook_inbox)
    sql("UPDATE email_messages SET provider_message_id = :s, ses_message_id = NULL WHERE message_id = :m",
        s=t["ses_id"], m=t["older"])
    raw = _reply(t["addr"], hook_inbox["inbox_email"], "Re: something else",
                 references=f"<x@elsewhere.example> <{t['ses_id']}@email.amazonses.com>")
    _sync(monkeypatch, hook_inbox, raw)
    assert _reply_events(t["older"]) == 1 and _reply_events(t["newer"]) == 0


def test_reply_threaded_by_subject_without_headers(client, monkeypatch, hook_inbox):
    t = _thread_two_sent(client, hook_inbox, subjects=("Pricing for Q4", "Checking in"))
    raw = _reply(t["addr"], hook_inbox["inbox_email"], "RE: Fwd: Pricing for  Q4")
    _sync(monkeypatch, hook_inbox, raw)
    assert _reply_events(t["older"]) == 1 and _reply_events(t["newer"]) == 0


def test_subject_fallback_stays_within_the_contact(client, monkeypatch, hook_inbox):
    """Another contact's email with the same subject is never the one replied to."""
    other = _thread_two_sent(client, hook_inbox, subjects=("Shared subject", "x"))
    t = _thread_two_sent(client, hook_inbox, subjects=("Different", "Newest"))
    raw = _reply(t["addr"], hook_inbox["inbox_email"], "Re: Shared subject")
    _sync(monkeypatch, hook_inbox, raw)
    assert _reply_events(other["older"]) == 0
    assert sql("SELECT conversation_id FROM email_messages WHERE prospect_id = :p AND direction = 'INBOUND'",
               p=t["pid"]) == t["conv"]


def test_stop_on_reply_honours_step_flags(client, monkeypatch, hook_inbox):
    h = hook_inbox["tenant"]["headers"]
    cid = _campaign(client, h, "stopflags")
    with SessionLocal() as db:
        steps = [EmailSequence(campaign_id=cid, step_number=i, wait_days=0, stop_on_reply=flag)
                 for i, flag in ((1, True), (2, True), (3, False), (4, None))]
        db.add_all(steps)
        db.commit()
        seq = [s.sequence_id for s in steps]
    t = _thread_two_sent(client, hook_inbox, campaign_id=cid, sequences=(seq[0], seq[1]))
    with SessionLocal() as db:
        db.add(CampaignProspect(campaign_id=cid, prospect_id=t["pid"], status="ACTIVE"))
        pending = {}
        for name, sid in (("keep", seq[2]), ("null_flag", seq[3]), ("no_step", None)):
            m = EmailMessage(campaign_id=cid, prospect_id=t["pid"], sequence_id=sid, direction="OUTBOUND",
                             status="SCHEDULED", to_email=t["addr"])
            db.add(m)
            db.flush()
            pending[name] = m.message_id
        db.commit()
    raw = _reply(t["addr"], hook_inbox["inbox_email"], "Re: Hello", in_reply_to=t["older_provider"])
    _sync(monkeypatch, hook_inbox, raw)
    status = {k: sql("SELECT status FROM email_messages WHERE message_id = :m", m=v) for k, v in pending.items()}
    assert status == {"keep": "SCHEDULED", "null_flag": "CANCELLED", "no_step": "CANCELLED"}
    assert sql("SELECT status FROM campaign_prospects WHERE campaign_id = :c AND prospect_id = :p",
               c=cid, p=t["pid"]) == "REPLIED"
