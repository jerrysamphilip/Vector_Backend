"""
A7: bounce-triggered automations fire (follow-up "bounce-triggered automations never fire").

The SES webhook raised TriggerType.EMAIL_BOUNCED, which did not exist, so the AttributeError was logged
and no rule ever ran. Now a hard bounce runs the campaign's active EMAIL_BOUNCED rules once per
(rule, contact) (automation_rule_runs); soft bounces and other triggers do not.
"""
import uuid

from app.core.clock import utcnow
from app.models.campaign import CampaignProspect
from app.models.email_message import EmailMessage
from tests.conftest import RUN, SessionLocal, sql

WEBHOOK = "/webhooks/ses/notifications"


def _campaign(client, h):
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Bounce rules {uuid.uuid4().hex[:6]} {RUN}"})
    assert r.status_code == 201, r.text
    return r.json()["campaign_id"]


def _enrolled_contact(client, h, cid):
    r = client.post("/contacts", headers=h, json={"email": f"bnc.{uuid.uuid4().hex[:6]}@a7bounce{RUN}.example.com",
                                                  "first_name": "Bounce"})
    assert r.status_code == 201, r.text
    pid, addr = r.json()["prospect_id"], r.json()["email"]
    with SessionLocal() as db:
        db.add(CampaignProspect(campaign_id=cid, prospect_id=pid, current_step=1, status="ACTIVE"))
        db.commit()
    return pid, addr


def _sent(cid, pid, addr):
    ses_id = f"0100-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, direction="OUTBOUND", status="SENT", to_email=addr,
                            from_email=f"sender@a7bounce{RUN}.example.com", ses_message_id=ses_id, sent_at=utcnow()))
        db.commit()
    return ses_id


def _bounce(client, ses_id, addr, kind="Permanent"):
    r = client.post(WEBHOOK, json={
        "notificationType": "Bounce",
        "mail": {"messageId": ses_id, "source": f"sender@a7bounce{RUN}.example.com", "tags": {}},
        "bounce": {"bounceType": kind, "bounceSubType": "General", "timestamp": "2026-10-27T10:00:01.000Z",
                   "bouncedRecipients": [{"emailAddress": addr, "diagnosticCode": "550 5.1.1 user unknown"}]}})
    assert r.status_code == 200, r.text


def _rule(client, h, cid, trigger, step):
    r = client.post("/automation-rules/", headers=h, json={
        "campaign_id": cid, "name": f"{trigger} -> step {step}", "trigger_type": trigger,
        "action_type": "CHANGE_CAMPAIGN_STEP", "action_config": {"step_id": step}})
    assert r.status_code == 201, r.text
    return r.json()["rule_id"]


def _step(cid, pid):
    return sql("SELECT current_step FROM campaign_prospects WHERE campaign_id = :c AND prospect_id = :p", c=cid, p=pid)


def test_hard_bounce_runs_bounce_rules_once_per_contact(client, tenant_a):
    h = tenant_a["headers"]
    cid = _campaign(client, h)
    bounce_rule = _rule(client, h, cid, "EMAIL_BOUNCED", 4)
    click_rule = _rule(client, h, cid, "EMAIL_CLICKED", 6)
    pid, addr = _enrolled_contact(client, h, cid)

    _bounce(client, _sent(cid, pid, addr), addr, kind="Transient")
    assert _step(cid, pid) == 1  # a soft bounce is not a bounce trigger

    _bounce(client, _sent(cid, pid, addr), addr)
    assert _step(cid, pid) == 4
    assert sql("SELECT COUNT(*) FROM automation_rule_runs WHERE rule_id = :r AND prospect_id = :p",
               r=bounce_rule, p=pid) == 1
    assert sql("SELECT COUNT(*) FROM automation_rule_runs WHERE rule_id = :r", r=click_rule) == 0

    # The next hard bounce for the same contact does not run the rule again
    sql("UPDATE campaign_prospects SET current_step = 2 WHERE campaign_id = :c AND prospect_id = :p", c=cid, p=pid)
    _bounce(client, _sent(cid, pid, addr), addr)
    assert _step(cid, pid) == 2
    assert sql("SELECT COUNT(*) FROM automation_rule_runs WHERE rule_id = :r", r=bounce_rule) == 1


def test_inactive_bounce_rule_does_not_run(client, tenant_a):
    h = tenant_a["headers"]
    cid = _campaign(client, h)
    rule_id = _rule(client, h, cid, "EMAIL_BOUNCED", 5)
    assert client.put(f"/automation-rules/{rule_id}", headers=h, json={"is_active": False}).status_code == 200
    pid, addr = _enrolled_contact(client, h, cid)
    _bounce(client, _sent(cid, pid, addr), addr)
    assert _step(cid, pid) == 1
    assert sql("SELECT COUNT(*) FROM automation_rule_runs WHERE rule_id = :r", r=rule_id) == 0
