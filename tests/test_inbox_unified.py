"""
Functional tests of the inbox round (S-09, S-10 / F-13, S-11, F-20) against MySQL:

  * unified inbox: server-side search (LIKE metacharacters literal), paging with has_more, campaign
    name per conversation, newest message first even when last_message_at is stale, label filter,
    owner/team scope, tenant isolation
  * thread: per-email activity timeline with the reply's label, final_status, campaign name
  * reply classification: stored label, one POSITIVE event per reply, retry job, confident
    UNSUBSCRIBE suppresses / doubtful one only flagged, LLM never called (no key)
  * sending alerts: NO_ACTIVE_MAILBOX, SEND_FAILURE_RATE, STUCK_SENDING; preference types listed
  * paused mailbox: reason + capacity on GET /inboxes, POST /inboxes/{id}/resume (permissions)
"""
import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


def _uid():
    return uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def ws(client):
    t = register_tenant(client, "ibx")
    h = t["headers"]
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@send-ibx{RUN}.example.com", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    t["inbox_id"] = r.json()["inbox_id"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Inbox Campaign {RUN}"})
    assert r.status_code == 201, r.text
    t["campaign_id"] = r.json()["campaign_id"]
    t["owner_id"] = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    return t


def _contact(client, h, first, last="Tester", email=None):
    r = client.post("/contacts", headers=h, json={
        "email": email or f"{first.lower()}.{_uid()}.{RUN}@ibxclient{RUN}.example.com",
        "first_name": first, "last_name": last})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _thread(ws, prospect_id, subject, *messages, last_message_at=None):
    """A conversation with messages: (direction, body, minutes_ago, extra fields)."""
    from app.models.conversation import Conversation
    from app.models.email_message import EmailMessage
    now = utcnow().replace(microsecond=0)
    with SessionLocal() as db:
        conv = Conversation(tenant_id=ws["tenant_id"], prospect_id=prospect_id, inbox_id=ws["inbox_id"],
                            subject=subject, last_message_at=last_message_at or now - timedelta(days=30))
        db.add(conv)
        db.flush()
        ids = []
        for direction, body, minutes_ago, extra in messages:
            m = EmailMessage(conversation_id=conv.id, prospect_id=prospect_id, inbox_id=ws["inbox_id"],
                             direction=direction, status=extra.pop("status", "SENT"), to_email="x@example.com",
                             subject=subject, body_text=body, sent_at=now - timedelta(minutes=minutes_ago), **extra)
            db.add(m)
            db.flush()
            ids.append(m.message_id)
        db.commit()
        return conv.id, ids


def _list(client, h, **params):
    r = client.get("/conversations", headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_search_is_server_side_and_literal(client, ws):
    h = ws["headers"]
    tag = _uid()
    p1 = _contact(client, h, f"Zed{tag}", "Percent")
    p2 = _contact(client, h, f"Amy{tag}", "Other")
    c1, _ = _thread(ws, p1, f"100% match {tag}", ("OUTBOUND", "<p>Hi Zed</p>", 50, {}))
    c2, _ = _thread(ws, p2, f"100 match {tag}", ("OUTBOUND", "Hi Amy", 40, {}))

    by_name = _list(client, h, search=f"zed{tag} percent")
    assert [i["id"] for i in by_name["items"]] == [c1]
    # "%" is literal, not a wildcard: only the subject with a real "100%" matches
    literal = _list(client, h, search=f"100% match {tag}")
    assert [i["id"] for i in literal["items"]] == [c1]
    assert {i["id"] for i in _list(client, h, search=tag)["items"]} == {c1, c2}
    assert _list(client, h, search="no-such-%_-thing")["total"] == 0
    assert client.get("/conversations", headers=h, params={"search": "x" * 201}).status_code == 422


def test_order_by_latest_message_paging_campaign_name_and_snippet(client, ws):
    h = ws["headers"]
    tag = _uid()
    old = _contact(client, h, f"Old{tag}")
    new = _contact(client, h, f"New{tag}")
    # The scheduler sent into the old conversation a minute ago without touching last_message_at
    c_old, _ = _thread(ws, old, f"Old {tag}", ("OUTBOUND", "first", 600, {}),
                       ("OUTBOUND", "<p>Latest <b>follow-up</b> &amp; more</p>", 1,
                        {"campaign_id": ws["campaign_id"], "final_status": "DELIVERED"}))
    c_new, _ = _thread(ws, new, f"New {tag}", ("OUTBOUND", "hello", 30, {}),
                       last_message_at=utcnow())
    page1 = _list(client, h, search=tag, page_size=1)
    assert page1["total"] == 2 and page1["has_more"] is True
    first = page1["items"][0]
    assert first["id"] == c_old
    assert first["campaign_name"] == f"Inbox Campaign {RUN}" and first["campaign_id"] == ws["campaign_id"]
    assert first["last_message_snippet"] == "Latest follow-up & more"
    assert first["last_message_final_status"] == "DELIVERED"
    page2 = _list(client, h, search=tag, page_size=1, page=2)
    assert [i["id"] for i in page2["items"]] == [c_new] and page2["has_more"] is False
    # Campaign filter
    only = _list(client, h, search=tag, campaign_id=ws["campaign_id"])
    assert [i["id"] for i in only["items"]] == [c_old]


def test_thread_timeline_and_reply_label(client, ws):
    from app.models.email_message import EmailEvent
    from app.services.reply_classifier import classify_pending
    h = ws["headers"]
    pid = _contact(client, h, f"Tim{_uid()}")
    conv_id, (out_id, reply_id) = _thread(
        ws, pid, "Timeline", ("OUTBOUND", "Hi", 60, {"campaign_id": ws["campaign_id"], "final_status": "DELIVERED",
                                                     "delivered_at": utcnow() - timedelta(minutes=59)}),
        ("INBOUND", "Sounds great, let's talk next week. Can you send me pricing?", 10, {}))
    reply_at = sql("SELECT sent_at FROM email_messages WHERE message_id = :m", m=reply_id)
    with SessionLocal() as db:
        db.add(EmailEvent(message_id=out_id, event_type=EmailEvent.EVENT_OPEN, event_time=utcnow() - timedelta(minutes=30)))
        db.add(EmailEvent(message_id=out_id, event_type=EmailEvent.EVENT_REPLY, event_time=reply_at))
        db.commit()

    stats = classify_pending(limit=500)  # no OPENAI key in tests: rule-based fallback
    assert stats["classified"] >= 1
    assert sql("SELECT reply_label FROM email_messages WHERE message_id = :m", m=reply_id) == "POSITIVE"
    assert sql("SELECT reply_label_source FROM email_messages WHERE message_id = :m", m=reply_id) == "RULES"
    assert sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m AND event_type = 8", m=out_id) == 1
    classify_pending(limit=500)  # idempotent: a labelled reply is not classified again
    assert sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m AND event_type = 8", m=out_id) == 1
    assert sql("SELECT reply_classify_attempts FROM email_messages WHERE message_id = :m", m=reply_id) == 1

    thread = client.get(f"/conversations/{conv_id}", headers=h).json()
    out, reply = thread["messages"]
    assert out["final_status"] == "DELIVERED" and out["campaign_name"] == f"Inbox Campaign {RUN}"
    steps = [a["type"] for a in out["activity"]]
    assert steps == ["SENT", "DELIVERED", "OPENED", "REPLIED"], steps
    assert out["activity"][-1]["reply_label"] == "POSITIVE"
    assert out["activity"][-1]["reply_message_id"] == reply_id
    assert reply["reply_label"] == "POSITIVE" and reply["activity"] == []
    labelled = _list(client, h, label="positive", search=thread["subject"])
    assert conv_id in [i["id"] for i in labelled["items"]]
    assert labelled["items"][0]["last_reply_label"] == "POSITIVE"
    assert client.get("/conversations", headers=h, params={"label": "bogus"}).status_code == 422


def test_unsubscribe_reply_suppresses_only_when_confident(client, ws):
    from app.services.reply_classifier import classify_pending
    h = ws["headers"]
    sure = _contact(client, h, f"Sure{_uid()}")
    unsure = _contact(client, h, f"Unsure{_uid()}")
    keep = _contact(client, h, f"Keep{_uid()}")
    _, (_, r_sure) = _thread(ws, sure, "U1", ("OUTBOUND", "Hi", 60, {}), ("INBOUND", "Please stop emailing me.", 5, {}))
    long_text = ("Thanks for the note. " * 30) + "Maybe remove me from the list at some point."
    _, (_, r_unsure) = _thread(ws, unsure, "U2", ("OUTBOUND", "Hi", 60, {}), ("INBOUND", long_text, 5, {}))
    _, (_, r_keep) = _thread(ws, keep, "U3", ("OUTBOUND", "Hi", 60, {}),
                             ("INBOUND", "Please don't stop emailing me, this is useful.", 5, {}))
    classify_pending(limit=500)
    assert sql("SELECT reply_label FROM email_messages WHERE message_id = :m", m=r_sure) == "UNSUBSCRIBE"
    assert sql("SELECT consent_status FROM prospects WHERE prospect_id = :p", p=sure) == "UNSUBSCRIBED"
    assert sql("SELECT reply_label FROM email_messages WHERE message_id = :m", m=r_unsure) == "UNSUBSCRIBE"
    assert sql("SELECT reply_needs_review FROM email_messages WHERE message_id = :m", m=r_unsure) == 1
    assert sql("SELECT consent_status FROM prospects WHERE prospect_id = :p", p=unsure) != "UNSUBSCRIBED"
    assert sql("SELECT reply_label FROM email_messages WHERE message_id = :m", m=r_keep) != "UNSUBSCRIBE"
    assert sql("SELECT consent_status FROM prospects WHERE prospect_id = :p", p=keep) != "UNSUBSCRIBED"


def test_positive_repliers_lookup(client, ws):
    from app.repositories.inbox_replies import positive_replier_ids
    h = ws["headers"]
    pid = _contact(client, h, f"Pos{_uid()}")
    other = _contact(client, h, f"Neu{_uid()}")
    _thread(ws, pid, "P", ("INBOUND", "yes", 5, {"reply_label": "POSITIVE", "reply_classified_at": utcnow()}))
    _thread(ws, other, "N", ("INBOUND", "ok", 5, {"reply_label": "NEUTRAL", "reply_classified_at": utcnow()}))
    with SessionLocal() as db:
        assert positive_replier_ids(db, ws["tenant_id"], [pid, other]) == {pid}
        assert positive_replier_ids(db, str(uuid.uuid4()), [pid, other]) == set()


def _agent(client, tenant_id):
    from app.core.security import hash_password
    from app.models.user import User
    email = f"ibx-agent-{RUN}-{_uid()}@ibx{RUN}.example.com"
    password = f"ibx-agent-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name="Ina", last_name="Agent", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                    email_verified=True, sales_level=3, custom_permissions=["manage_prospects"])
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


def test_owner_scope_and_tenant_isolation(client, ws, tenant_b):
    h = ws["headers"]
    agent = _agent(client, ws["tenant_id"])
    tag = _uid()
    mine = _contact(client, h, f"Mine{tag}")
    theirs = _contact(client, h, f"Theirs{tag}")
    sql("UPDATE prospects SET owner_id = :u WHERE prospect_id = :p", u=agent["user_id"], p=mine)
    sql("UPDATE prospects SET owner_id = :u WHERE prospect_id = :p", u=ws["owner_id"], p=theirs)
    c_mine, _ = _thread(ws, mine, f"Scope {tag}", ("OUTBOUND", "a", 5, {}))
    c_theirs, _ = _thread(ws, theirs, f"Scope {tag}", ("OUTBOUND", "b", 4, {}))

    assert {i["id"] for i in _list(client, h, search=tag)["items"]} == {c_mine, c_theirs}
    assert [i["id"] for i in _list(client, agent["headers"], search=tag)["items"]] == [c_mine]
    assert client.get(f"/conversations/{c_theirs}", headers=agent["headers"]).status_code == 404
    assert client.post(f"/conversations/{c_theirs}/reply", headers=agent["headers"],
                       json={"body_text": "hi"}).status_code == 404
    assert client.get(f"/conversations/{c_mine}", headers=agent["headers"]).status_code == 200
    # Another workspace sees none of it
    assert _list(client, tenant_b["headers"], search=tag)["total"] == 0
    assert client.get(f"/conversations/{c_mine}", headers=tenant_b["headers"]).status_code == 404


def test_reply_moves_conversation_up(client, ws):
    h = ws["headers"]
    tag = _uid()
    a = _contact(client, h, f"Ra{tag}")
    b = _contact(client, h, f"Rb{tag}")
    c_a, _ = _thread(ws, a, f"R {tag}", ("OUTBOUND", "a", 120, {}))
    c_b, _ = _thread(ws, b, f"R {tag}", ("OUTBOUND", "b", 60, {}))
    assert [i["id"] for i in _list(client, h, search=tag)["items"]] == [c_b, c_a]
    r = client.post(f"/conversations/{c_a}/reply", headers=h, json={"body_text": "Following up"})
    assert r.status_code == 200, r.text
    assert r.json()["message"]["status"] == "QUEUED"
    assert [i["id"] for i in _list(client, h, search=tag)["items"]] == [c_a, c_b]


# ── Sending alerts (S-11) ─────────────────────────────────────

def test_sending_alerts_collected(client):
    from app.services.sending_alerts import collect_sending_alerts
    t = register_tenant(client, "ibxal")
    h = t["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"No Mailbox {RUN}"})
    campaign_id = r.json()["campaign_id"]
    pid = _contact(client, h, f"Al{_uid()}")
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=campaign_id)
    from app.models.email_message import EmailMessage
    now = utcnow()
    with SessionLocal() as db:
        db.add(EmailMessage(campaign_id=campaign_id, prospect_id=pid, direction="OUTBOUND", status="QUEUED",
                            to_email="a@example.com", scheduled_at=now))
        for i in range(6):
            db.add(EmailMessage(campaign_id=campaign_id, prospect_id=pid, direction="OUTBOUND", status="FAILED",
                                to_email=f"f{i}@example.com", scheduled_at=now - timedelta(hours=1),
                                failure_reason="SMTP refused"))
        for i in range(10):
            db.add(EmailMessage(campaign_id=campaign_id, prospect_id=pid, direction="OUTBOUND", status="SENT",
                                to_email=f"s{i}@example.com", sent_at=now - timedelta(hours=2),
                                delivered_at=now - timedelta(hours=2)))
        db.add(EmailMessage(campaign_id=campaign_id, prospect_id=pid, direction="OUTBOUND", status="SENDING",
                            to_email="stuck@example.com", claimed_at=now - timedelta(hours=1)))
        db.commit()
        alerts = {a["alert_type"]: a for a in collect_sending_alerts(db, t["tenant_id"], now=now)}
    assert alerts["NO_ACTIVE_MAILBOX"]["severity"] == "CRITICAL"
    assert alerts["SEND_FAILURE_RATE"]["severity"] == "CRITICAL"  # 6 of 16 = 37 %
    assert "SMTP refused" in alerts["SEND_FAILURE_RATE"]["message"]
    assert alerts["STUCK_SENDING"]["severity"] == "WARNING"
    assert "SES_UNAVAILABLE" not in alerts and "DELIVERY_EVENTS_MISSING" not in alerts

    dash = client.get("/deliverability/dashboard-alerts", headers=h)
    assert dash.status_code == 200, dash.text
    assert {"NO_ACTIVE_MAILBOX", "SEND_FAILURE_RATE"} <= {a["alert_type"] for a in dash.json()}
    r = client.get("/deliverability/alert-preferences", headers=h)
    assert r.status_code == 200, r.text
    types = {p["alert_type"] for p in r.json()["tenant_preferences"]}
    assert {"NO_ACTIVE_MAILBOX", "INBOX_LIMIT_REACHED", "SEND_FAILURE_RATE", "DELIVERY_EVENTS_MISSING",
            "SES_UNAVAILABLE", "STUCK_SENDING"} <= types


# ── Paused mailbox (F-20) ─────────────────────────────────────

def test_paused_mailbox_reason_capacity_and_resume(client, ws):
    h = ws["headers"]
    inbox_id = ws["inbox_id"]
    sql("UPDATE sending_inboxes SET daily_limit = 500, max_emails_per_day = NULL, delay_between_emails = 600, "
        "status = 'PAUSED' WHERE inbox_id = :i", i=inbox_id)
    row = next(i for i in client.get("/inboxes", headers=h).json() if i["inbox_id"] == inbox_id)
    assert row["status"] == "PAUSED" and row["paused_reason"] == "Paused by a user"
    assert row["daily_capacity"] == 144  # 86400 s / 600 s pacing caps the 500 limit
    assert row["remaining_today"] == 0

    agent = _agent(client, ws["tenant_id"])
    assert client.post(f"/inboxes/{inbox_id}/resume", headers=agent["headers"]).status_code == 403
    other = register_tenant(client, "ibxother")
    assert client.post(f"/inboxes/{inbox_id}/resume", headers=other["headers"]).status_code == 404

    r = client.post(f"/inboxes/{inbox_id}/resume", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACTIVE" and body["paused_reason"] is None and body["remaining_today"] > 0
    assert sql("SELECT COUNT(*) FROM inbox_warmup_events WHERE inbox_id = :i AND subject = 'Mailbox resumed'",
               i=inbox_id) == 1
    assert client.post(f"/inboxes/{inbox_id}/resume", headers=h).status_code == 200  # idempotent
    assert sql("SELECT COUNT(*) FROM inbox_warmup_events WHERE inbox_id = :i AND subject = 'Mailbox resumed'",
               i=inbox_id) == 1
