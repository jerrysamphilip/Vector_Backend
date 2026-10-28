"""
UAT defects 3-5 (campaign analytics): one open-rate denominator, an honest daily chart, "sent today" that
counts only mail that went out, and enrolments that opened an email still advancing / completing.

The seed mirrors the UAT campaign: 415 enrolled, 36 step-1 sends two days ago, 7 of them bounced, 16 unique
openers with 24 open events (8 opened twice), the campaign PAUSED. Rates:
    open rate   = unique openers / delivered (sent - bounced) = 16 / 29 = 55.17 %
    bounce rate = bounced / sent                               =  7 / 36 = 19.44 %
"""

import asyncio
from datetime import timedelta

from app.core.clock import utcnow
from tests.conftest import RUN, SessionLocal, register_tenant, sql

ENROLLED, SENT, BOUNCED, OPENERS, REPEAT_OPENERS = 415, 36, 7, 16, 8
DELIVERED = SENT - BOUNCED
OPEN_RATE = round(OPENERS / DELIVERED * 100, 2)          # 55.17
BOUNCE_RATE = round(BOUNCED / SENT * 100, 2)             # 19.44


def _today_start():
    now = utcnow().replace(tzinfo=None)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _seed_uat_campaign(client, label):
    """The UAT campaign; returns (headers, campaign_id, send_day)."""
    from app.models.campaign import Campaign, CampaignProspect
    from app.models.email_message import EmailEvent, EmailMessage
    from app.models.email_sequence import EmailSequence
    from app.models.prospect import Prospect

    t = register_tenant(client, label)
    h = t["headers"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"UAT analytics {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    send_day = _today_start() - timedelta(days=2) + timedelta(hours=9)
    with SessionLocal() as db:
        steps = {}
        for n in (1, 2, 3):
            s = EmailSequence(campaign_id=cid, tenant_id=t["tenant_id"], step_number=n, wait_days=3)
            db.add(s)
            db.flush()
            steps[n] = s.sequence_id
        prospects = [Prospect(tenant_id=t["tenant_id"], email=f"p{i}-{label}{RUN}@lead{RUN}.example.com")
                     for i in range(ENROLLED)]
        db.add_all(prospects)
        db.flush()
        sent = []
        for i, p in enumerate(prospects):
            status = "ACTIVE"
            if i < SENT:
                m = EmailMessage(campaign_id=cid, prospect_id=p.prospect_id, sequence_id=steps[1],
                                 direction="OUTBOUND", status="BOUNCED" if i < BOUNCED else "SENT",
                                 to_email=p.email, sent_at=send_day + timedelta(minutes=i))
                db.add(m)
                sent.append(m)
                status = "BOUNCED" if i < BOUNCED else ("OPENED" if i < BOUNCED + OPENERS else "ACTIVE")
            db.add(CampaignProspect(campaign_id=cid, prospect_id=p.prospect_id, tenant_id=t["tenant_id"],
                                    status=status, current_step=2 if BOUNCED <= i < SENT else 1))
        db.flush()
        for m in sent[:BOUNCED]:
            db.add(EmailEvent(message_id=m.message_id, event_type=EmailEvent.EVENT_BOUNCE,
                              event_time=send_day + timedelta(minutes=50)))
        for i, m in enumerate(sent[BOUNCED:BOUNCED + OPENERS]):
            db.add(EmailEvent(message_id=m.message_id, event_type=EmailEvent.EVENT_OPEN,
                              event_time=send_day + timedelta(hours=1, minutes=i)))
            if i < REPEAT_OPENERS:
                db.add(EmailEvent(message_id=m.message_id, event_type=EmailEvent.EVENT_OPEN,
                                  event_time=send_day + timedelta(hours=5, minutes=i)))
        db.query(Campaign).filter(Campaign.campaign_id == cid).update({"status": "PAUSED"})
        db.commit()
    return h, cid, send_day


def test_metrics_uat_campaign_counts_and_open_rate_on_delivered(client):
    h, cid, send_day = _seed_uat_campaign(client, "mta")

    row = next(c for c in client.get("/campaigns", headers=h, params={"page_size": 100}).json()["items"]
               if c["campaign_id"] == cid)
    assert (row["prospect_count"], row["in_progress_count"], row["sent_count"], row["opened_count"],
            row["bounced_count"]) == (ENROLLED, ENROLLED - BOUNCED, SENT, OPENERS, BOUNCED), row

    a = client.get(f"/campaigns/{cid}/analytics", headers=h, params={"days": 30}).json()
    m = a["metrics"]
    assert (m["sent_count"], m["opened_count"], m["bounced_count"]) == (SENT, OPENERS, BOUNCED), m
    assert m["open_rate"] == OPEN_RATE                      # was 44.44 (16 / 36 sent)
    assert m["bounce_rate"] == BOUNCE_RATE
    step1 = next(s for s in a["sequences"] if s["step"] == 1)
    assert (step1["sent"], step1["opened_count"], step1["bounced_count"]) == (SENT, OPENERS, BOUNCED)
    assert step1["open_rate"] == OPEN_RATE                  # was 44.44
    assert step1["bounce_rate"] == BOUNCE_RATE

    # /campaigns/{id}/metrics (metrics_service) agrees
    mm = client.get(f"/campaigns/{cid}/metrics", headers=h).json()
    assert round(mm["open_rate"], 2) == OPEN_RATE, mm
    assert round(mm["bounce_rate"], 2) == BOUNCE_RATE, mm

    # Reports: the campaign comparison and the dashboard KPIs use the same denominator
    comp = client.get("/reports/campaign-comparison", headers=h).json()["items"]
    item = next(i for i in comp if i["campaign_id"] == cid)
    assert (item["open_rate"], item["bounce_rate"]) == (OPEN_RATE, BOUNCE_RATE), item
    start = (send_day - timedelta(days=1)).date().isoformat()
    kpis = client.get("/reports/global-analytics", headers=h, params={"start_date": start}).json()["kpis"]
    assert (kpis["overall_open_rate"], kpis["overall_bounce_rate"]) == (OPEN_RATE, BOUNCE_RATE), kpis


def test_metrics_daily_chart_counts_unique_openers_per_day_without_a_lifetime_last_bar(client):
    h, cid, send_day = _seed_uat_campaign(client, "mtb")
    bars = client.get(f"/campaigns/{cid}/analytics", headers=h, params={"days": 30}).json()["bar_data"]
    by_day = {b["date"]: b for b in bars}
    day = by_day[send_day.date().isoformat()]
    assert (day["sent"], day["opened"], day["bounced"]) == (SENT, OPENERS, BOUNCED), day   # 16 openers, not 24 events
    today = bars[-1]
    assert today["date"] == _today_start().date().isoformat()
    # Nothing happened today: the 16 enrolments with status OPENED must not show up as today's opens
    assert (today["sent"], today["opened"], today["replied"]) == (0, 0, 0), today
    assert sum(b["opened"] for b in bars) == OPENERS


def test_metrics_sent_today_counts_only_mail_that_went_out(client):
    from app.models.email_message import EmailMessage
    from app.models.prospect import Prospect
    from app.models.sending_inbox import SendingInbox

    t = register_tenant(client, "mtc")
    h = t["headers"]
    today = _today_start()
    with SessionLocal() as db:
        inbox = SendingInbox(tenant_id=t["tenant_id"], email_address=f"box@mtc{RUN}.example.com",
                             status="ACTIVE", daily_limit=50, warmup_enabled=False)
        p = Prospect(tenant_id=t["tenant_id"], email=f"lead@mtc{RUN}.example.com")
        db.add_all([inbox, p])
        db.flush()
        for i, status in enumerate(["SENT", "SENT", "SENT", "BOUNCED", "COMPLAINED", "FAILED", "FAILED", "QUEUED"]):
            db.add(EmailMessage(prospect_id=p.prospect_id, inbox_id=inbox.inbox_id, direction="OUTBOUND",
                                status=status, to_email=p.email, sent_at=today + timedelta(seconds=i + 1)))
        # yesterday (UTC) does not count
        db.add(EmailMessage(prospect_id=p.prospect_id, inbox_id=inbox.inbox_id, direction="OUTBOUND",
                            status="SENT", to_email=p.email, sent_at=today - timedelta(minutes=5)))
        db.commit()
        inbox_id = inbox.inbox_id
    listed = client.get("/inboxes", headers=h).json()
    listed = listed if isinstance(listed, list) else listed["items"]
    box = next(i for i in listed if i["inbox_id"] == inbox_id)
    assert box["sent_today"] == 5, box          # SENT x3 + BOUNCED + COMPLAINED; FAILED / QUEUED excluded
    assert box["remaining_today"] == box["daily_capacity"] - 5, box


def _one_contact_campaign(client, label, enrolment_status, steps):
    """An ACTIVE campaign with one contact (enrolment_status) and `steps` steps; step `steps` just went out.
    Returns (campaign_id, prospect_id, sent message id)."""
    from app.models.campaign import Campaign, CampaignProspect
    from app.models.email_message import EmailMessage
    from app.models.email_sequence import EmailSequence
    from app.models.prospect import Prospect

    t = register_tenant(client, label)
    tenant_id = t["tenant_id"]
    owner = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    with SessionLocal() as db:
        camp = Campaign(tenant_id=tenant_id, campaign_name=f"{label} {RUN}", status="ACTIVE", created_by=owner)
        p = Prospect(tenant_id=tenant_id, email=f"{label}{RUN}@lead{RUN}.example.com")
        db.add_all([camp, p])
        db.flush()
        seqs = []
        for n in range(1, steps + 1):
            s = EmailSequence(campaign_id=camp.campaign_id, tenant_id=tenant_id, step_number=n, wait_days=2)
            db.add(s)
            db.flush()
            seqs.append(s)
        sent_step = seqs[0]
        db.add(CampaignProspect(campaign_id=camp.campaign_id, prospect_id=p.prospect_id, tenant_id=tenant_id,
                                status=enrolment_status, current_step=1))
        m = EmailMessage(campaign_id=camp.campaign_id, prospect_id=p.prospect_id, sequence_id=sent_step.sequence_id,
                         direction="OUTBOUND", status="SENT", to_email=p.email, sent_at=utcnow())
        db.add(m)
        db.commit()
        return camp.campaign_id, p.prospect_id, m.message_id


def _enrolment(cid, pid):
    from app.models.campaign import Campaign, CampaignProspect
    with SessionLocal() as db:
        cp = db.query(CampaignProspect).filter_by(campaign_id=cid, prospect_id=pid).one()
        return cp.status, cp.current_step, db.get(Campaign, cid).status


def test_metrics_opened_enrolment_completes_after_the_last_step_and_the_campaign_completes(client):
    from app.models.email_message import EmailMessage
    from app.services.email_scheduler_service import email_scheduler

    from app.services.email_scheduler_service import _context

    cid, pid, mid = _one_contact_campaign(client, "mtd", "OPENED", steps=1)
    with SessionLocal() as db:
        asyncio.run(email_scheduler._update_prospect_step(db.get(EmailMessage, mid), db))
        db.commit()
    assert _enrolment(cid, pid)[0] == "COMPLETED"
    # The scheduler's auto-complete sweep (_check_completed_campaigns) completes an ACTIVE campaign when no
    # enrolment is still open; checked on this campaign alone so the sweep does not touch other tests' data
    with SessionLocal() as db:
        assert _context(db).enrollment_counts(cid) == (1, 0)


def test_metrics_opened_enrolment_advances_to_the_next_step(client):
    from app.models.email_message import EmailMessage
    from app.services.email_scheduler_service import email_scheduler

    cid, pid, mid = _one_contact_campaign(client, "mte", "OPENED", steps=2)
    with SessionLocal() as db:
        asyncio.run(email_scheduler._update_prospect_step(db.get(EmailMessage, mid), db))
        db.commit()
    assert _enrolment(cid, pid)[:2] == ("OPENED", 2)


def test_metrics_send_now_advances_and_completes_an_opened_enrolment(client):
    from app.models.email_message import EmailMessage
    from app.services.campaign_send_now_service import CampaignSendNowService

    cid, pid, mid = _one_contact_campaign(client, "mtf", "OPENED", steps=2)
    with SessionLocal() as db:
        CampaignSendNowService(db)._advance_prospect(db.get(EmailMessage, mid))
        db.commit()
    assert _enrolment(cid, pid)[:2] == ("OPENED", 2)

    cid, pid, mid = _one_contact_campaign(client, "mtg", "OPENED", steps=1)
    with SessionLocal() as db:
        CampaignSendNowService(db)._advance_prospect(db.get(EmailMessage, mid))
        db.commit()
    assert _enrolment(cid, pid)[0] == "COMPLETED"


def test_metrics_replied_enrolment_is_not_completed_over(client):
    from app.models.email_message import EmailMessage
    from app.services.email_scheduler_service import email_scheduler

    cid, pid, mid = _one_contact_campaign(client, "mth", "REPLIED", steps=1)
    with SessionLocal() as db:
        asyncio.run(email_scheduler._update_prospect_step(db.get(EmailMessage, mid), db))
        db.commit()
    assert _enrolment(cid, pid)[0] == "REPLIED"
