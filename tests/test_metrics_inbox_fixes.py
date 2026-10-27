"""
Regression tests: clicked / replied / bounced counts agree on every screen (E2E-J1-44),
and the unified inbox list shows the reply as the latest message with a plain-text
snippet (OUT-052).
"""


from tests.conftest import RUN, SessionLocal, register_tenant
from app.core.clock import utcnow


def _contact(client, h, label, domain):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{RUN}@{domain}{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def test_clicked_is_unique_prospects_on_every_screen(client):
    from app.models.email_message import EmailEvent, EmailMessage
    from app.models.email_sequence import EmailSequence

    t = register_tenant(client, "clk")
    h = t["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Clicks {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    p1, p2, p3 = (_contact(client, h, n, "clicks") for n in ("one", "two", "three"))

    now = utcnow()
    with SessionLocal() as db:
        s1 = EmailSequence(campaign_id=cid, step_number=1, wait_days=0)
        db.add(s1)
        db.flush()

        def msg(pid, status):
            m = EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=s1.sequence_id,
                             direction="OUTBOUND", status=status, to_email="x@example.com", sent_at=now)
            db.add(m)
            db.flush()
            return m

        m1, m2, m3 = msg(p1, "SENT"), msg(p2, "SENT"), msg(p3, "BOUNCED")

        def ev(m, etype):
            db.add(EmailEvent(message_id=m.message_id, event_type=etype, event_time=now))

        for m in (m1, m1, m2):            # p1 opens twice, p2 once
            ev(m, EmailEvent.EVENT_OPEN)
        for m in (m1, m1, m1, m2):        # p1 clicks three links, p2 one: 2 clickers
            ev(m, EmailEvent.EVENT_CLICK)
        for m in (m3, m3):                # duplicate bounce notification: 1 bounced
            ev(m, EmailEvent.EVENT_BOUNCE)
        for m in (m2, m2):                # two reply events on one message: 1 replied
            ev(m, EmailEvent.EVENT_REPLY)
        db.commit()

    m = client.get(f"/campaigns/{cid}/metrics", headers=h).json()
    assert (m["sent_count"], m["opened_count"], m["bounced_count"]) == (3, 2, 1), m

    a = client.get(f"/campaigns/{cid}/analytics", headers=h).json()
    am = a["metrics"]
    assert (am["sent_count"], am["opened_count"], am["clicked_count"], am["bounced_count"]) == (3, 2, 2, 1), am
    assert am["click_rate"] == 100.0
    steps = {s["step"]: s for s in a["sequences"]}
    assert steps[1]["click_rate"] == 100.0   # 2 clickers of 2 openers

    for k in (client.get("/reports/global-analytics", headers=h).json()["kpis"],
              client.get("/reports/dashboard-summary", headers=h).json()["kpis"]):
        assert (k["total_emails_sent"], k["total_opened"], k["total_clicked"],
                k["total_bounced"], k["total_replied"]) == (3, 2, 2, 1, 1), k

    ts = client.get("/reports/global-analytics", headers=h).json()["timeseries"]
    today = next(p for p in ts if p["date"] == now.date().isoformat())
    assert (today["sent"], today["opened"], today["bounced"], today["replied"]) == (3, 2, 1, 1), today


def test_unified_inbox_lists_reply_last_with_plain_text_snippet(client):
    from app.models.conversation import Conversation
    from app.models.email_message import EmailMessage

    t = register_tenant(client, "cnv")
    h = t["headers"]
    domain = f"send-cnv{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    pid = _contact(client, h, "pat", "conv")
    pid2 = _contact(client, h, "sam", "conv")

    same_second = utcnow().replace(microsecond=0)
    with SessionLocal() as db:
        conv = Conversation(tenant_id=t["tenant_id"], prospect_id=pid, inbox_id=inbox_id, subject="Hello")
        conv2 = Conversation(tenant_id=t["tenant_id"], prospect_id=pid2, inbox_id=inbox_id, subject="Hi")
        db.add_all([conv, conv2])
        db.flush()
        # The reply is ingested in the same second as the send (sent_at has second precision).
        db.add(EmailMessage(conversation_id=conv.id, prospect_id=pid, inbox_id=inbox_id,
                            direction="INBOUND", status="SENT", to_email="x@example.com",
                            body_text="Sounds interesting, let's talk next week.", sent_at=same_second))
        db.add(EmailMessage(conversation_id=conv.id, prospect_id=pid, inbox_id=inbox_id,
                            direction="OUTBOUND", status="SENT", to_email="x@example.com",
                            body_text="<p>Hi Pat,</p>\n\n<p>Step one.</p>", sent_at=same_second))
        db.add(EmailMessage(conversation_id=conv2.id, prospect_id=pid2, inbox_id=inbox_id,
                            direction="OUTBOUND", status="SENT", to_email="y@example.com",
                            body_text="<p>Hi Sam,</p>\n<p>Q&amp;A &nbsp;<b>today</b></p>", sent_at=same_second))
        db.commit()
        conv_id, conv2_id = conv.id, conv2.id

    items = {c["id"]: c for c in client.get("/conversations?page_size=100", headers=h).json()["items"]}
    assert items[conv_id]["last_message_direction"] == "INBOUND", items[conv_id]
    assert items[conv_id]["last_message_snippet"] == "Sounds interesting, let's talk next week."
    assert items[conv2_id]["last_message_snippet"] == "Hi Sam, Q&A today"

    thread = client.get(f"/conversations/{conv_id}", headers=h).json()
    assert [m["direction"] for m in thread["messages"]] == ["OUTBOUND", "INBOUND"]
