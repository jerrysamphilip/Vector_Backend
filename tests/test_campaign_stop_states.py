"""
A contact who replied and then unsubscribed: the campaign keeps the reply as the enrolment's status (history is
not rewritten), the Prospects list also carries the contact's consent state so the page can show the
unsubscribe, and the follow-ups the reply cancelled are counted as cancelled on the Schedule tab, not as
pending (UAT gaps D1 and D3).
"""
import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from tests.conftest import RUN, SessionLocal, register_tenant


@pytest.fixture(scope="module")
def replied(client):
    from app.models.campaign import CampaignProspect
    from app.models.email_message import EmailMessage
    from app.models.email_sequence import EmailSequence
    from app.models.prospect import Prospect

    t = register_tenant(client, "stopstates")
    tid, h = t["tenant_id"], t["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Stop states {uuid.uuid4().hex[:6]} {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    sent = utcnow() - timedelta(days=1)
    with SessionLocal() as db:
        step1 = EmailSequence(campaign_id=cid, step_number=1, wait_days=0)
        step2 = EmailSequence(campaign_id=cid, step_number=2, wait_days=3)
        db.add_all([step1, step2])
        db.flush()
        p = Prospect(tenant_id=tid, email=f"replied.{uuid.uuid4().hex[:6]}.{RUN}@stop{RUN}.example.com",
                     first_name="Rae", consent_status="OPT_IN")
        db.add(p)
        db.flush()
        db.add(CampaignProspect(campaign_id=cid, prospect_id=p.prospect_id, tenant_id=tid, status="REPLIED"))
        db.add(EmailMessage(campaign_id=cid, prospect_id=p.prospect_id, sequence_id=step1.sequence_id,
                            direction="OUTBOUND", status="SENT", subject="Hi", body_text="Hello", to_email=p.email,
                            sent_at=sent, scheduled_at=sent))
        db.add(EmailMessage(campaign_id=cid, prospect_id=p.prospect_id, sequence_id=step2.sequence_id,
                            direction="OUTBOUND", status="CANCELLED", failure_reason="Prospect replied",
                            subject="Follow up", body_text="Again", to_email=p.email,
                            scheduled_at=sent + timedelta(days=3)))
        db.commit()
        pid, email = p.prospect_id, p.email
    return {"tid": tid, "h": h, "cid": cid, "pid": pid, "email": email}


def test_schedule_counts_the_cancelled_follow_up_as_cancelled(client, replied):
    r = client.get(f"/campaigns/{replied['cid']}/schedule", headers=replied["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["total_sent"], body["total_scheduled"], body["total_cancelled"], body["total_failed"]) == (1, 0, 1, 0)
    assert sorted((b["step_number"], b["status"]) for b in body["schedule"]) == [(1, "SENT"), (2, "CANCELLED")]


def test_replied_then_unsubscribed_keeps_replied_and_shows_the_unsubscribe(client, replied):
    from app.services.suppression import suppress
    with SessionLocal() as db:
        suppress(db, replied["tid"], replied["email"], "Clicked unsubscribe", kind="UNSUBSCRIBE", source="test")
        db.commit()
    r = client.get(f"/campaigns/{replied['cid']}/prospects", headers=replied["h"])
    assert r.status_code == 200, r.text
    row = next(p for p in r.json() if p["prospect_id"] == replied["pid"])
    assert (row["status"], row["consent_status"]) == ("REPLIED", "UNSUBSCRIBED")
