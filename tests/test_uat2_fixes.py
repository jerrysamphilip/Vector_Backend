"""
UAT round 2 backend fixes, functional (MySQL):

  * B-01  POST /campaigns keeps unsubscribe_mode 'html' (the wizard's choice); anything but plain / html is a 422
  * C-08  Reports 'Follow-up reply rate' = replies to step > 1 emails / delivered step > 1 emails
  * E-02  a deal created on a contact moves its lifecycle forward to Opportunity, never backward

The import preview company counts (D-02) are in tests/test_a7_imports.py.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app.models.email_message import EmailEvent
from app.models.email_sequence import EmailSequence
from tests.conftest import RUN, SessionLocal, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "uatb")


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={"email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@uatb{RUN}.example.com",
                                                  "first_name": label.capitalize()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


# ── B-01 ──

def test_create_campaign_keeps_the_unsubscribe_mode(client, ws):
    h = ws["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Unsub html {RUN}", "unsubscribe_mode": "html"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    assert r.json()["unsubscribe_mode"] == "html"
    assert client.get(f"/campaigns/{cid}", headers=h).json()["unsubscribe_mode"] == "html"
    assert sql("SELECT unsubscribe_mode FROM campaigns WHERE campaign_id = :c", c=cid) == "html"

    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Unsub default {RUN}"})
    assert r.status_code == 201 and r.json()["unsubscribe_mode"] == "plain", r.text
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Unsub bad {RUN}", "unsubscribe_mode": "link"})
    assert r.status_code == 422, r.text
    assert client.put(f"/campaigns/{cid}", headers=h, json={"unsubscribe_mode": "link"}).status_code == 422
    assert sql("SELECT unsubscribe_mode FROM campaigns WHERE campaign_id = :c", c=cid) == "html"


# ── C-08 ──

def _steps(cid, n):
    ids = []
    with SessionLocal() as db:
        for i in range(1, n + 1):
            seq = EmailSequence(campaign_id=cid, step_number=i, wait_days=0 if i == 1 else 2)
            db.add(seq)
            db.flush()
            ids.append(seq.sequence_id)
        db.commit()
    return ids


def _mail(tenant, cid, pid, sequence_id, status="SENT", direction="OUTBOUND", events=(), day=0):
    mid = str(uuid.uuid4())
    sent_at = datetime(2026, 9, 1, 9) + timedelta(days=day)
    sql("INSERT INTO email_messages (message_id, prospect_id, campaign_id, tenant_id, sequence_id, direction, status, "
        "to_email, subject, sent_at) VALUES (:m, :p, :c, :t, :s, :d, :st, 'x@example.com', 'Hi', :at)",
        m=mid, p=pid, c=cid, t=tenant, s=sequence_id, d=direction, st=status, at=sent_at)
    for event_type in events:
        _event(tenant, mid, event_type, sent_at)
    return mid


def _event(tenant, mid, event_type, at):
    sql("INSERT INTO email_events (event_id, message_id, tenant_id, event_type, event_time) "
        "VALUES (:e, :m, :t, :k, :at)", e=str(uuid.uuid4()), m=mid, t=tenant, k=event_type,
        at=at + timedelta(hours=2))


def _follow_up_rate(client, h):
    r = client.get("/reports/dashboard-summary", headers=h,
                   params={"start_date": "2026-09-01", "end_date": "2026-09-30"})
    assert r.status_code == 200, r.text
    return r.json()["optimization"]["follow_up_reply_rate"]


def test_follow_up_reply_rate_counts_only_replies_to_follow_up_emails(client, ws):
    h, tenant = ws["headers"], ws["tenant_id"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Follow-ups {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    s1, s2 = _steps(cid, 2)
    p1, p2, p3 = (_contact(client, h, n) for n in ("fua", "fub", "fuc"))
    reply, bounce = EmailEvent.EVENT_REPLY, EmailEvent.EVENT_BOUNCE
    _mail(tenant, cid, p1, s1, events=[reply])              # the only reply answers the first email
    _mail(tenant, cid, p1, s2, day=3)
    _mail(tenant, cid, p1, s2, direction="INBOUND", day=4)  # an inbound message is never a sent follow-up
    _mail(tenant, cid, p2, s1)
    p2_follow_up = _mail(tenant, cid, p2, s2, day=3)
    _mail(tenant, cid, p3, s1)
    _mail(tenant, cid, p3, s2, status="BOUNCED", events=[bounce], day=3)
    # UAT2 C-08: was 1.96 % (every reply of contacts past step 1 over all their emails); nobody answered a follow-up
    assert _follow_up_rate(client, h) == 0.0

    _event(tenant, p2_follow_up, reply, datetime(2026, 9, 4, 9))
    # 1 reply / (3 follow-ups sent - 1 bounced)
    assert _follow_up_rate(client, h) == 50.0


# ── E-02 ──

def _lifecycle(pid):
    return sql("SELECT lifecycle_stage FROM prospects WHERE prospect_id = :p", p=pid)


def _lifecycle_changes(pid):
    return sql("SELECT COUNT(*) FROM property_changes WHERE object_type = 'CONTACT' AND object_id = :p "
               "AND field = 'lifecycle_stage' AND new_value = 'OPPORTUNITY'", p=pid)


def test_deal_on_a_contact_makes_it_an_opportunity_but_never_downgrades(client, ws):
    h = ws["headers"]
    lead_contact, customer = _contact(client, h, "dealee"), _contact(client, h, "client")
    sql("UPDATE prospects SET lifecycle_stage = 'LEAD' WHERE prospect_id = :p", p=lead_contact)
    sql("UPDATE prospects SET lifecycle_stage = 'CUSTOMER' WHERE prospect_id = :p", p=customer)

    r = client.post("/opportunities", headers=h, json={"name": f"Deal on lead {RUN}", "prospect_id": lead_contact})
    assert r.status_code == 201, r.text
    assert _lifecycle(lead_contact) == "OPPORTUNITY"
    assert _lifecycle_changes(lead_contact) == 1  # in the contact's history, like a lead conversion

    r = client.post("/opportunities", headers=h, json={"name": f"Deal on customer {RUN}", "prospect_id": customer})
    assert r.status_code == 201, r.text
    assert _lifecycle(customer) == "CUSTOMER" and _lifecycle_changes(customer) == 0
