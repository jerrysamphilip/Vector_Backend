"""
A2 / F-04 against MySQL: editing a campaign after launch (POST /campaigns/{id}/edits and its dry run),
the step preview, template assignments and launch's pacing warning. Unsent emails follow the edit;
SENT / SENDING ones never change.
"""
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant, sql
from app.core.config import settings
from app.models.company_profile import CompanyProfile
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate


@pytest.fixture(scope="module")
def ed(client):
    tenant = register_tenant(client, "aedit")
    with SessionLocal() as db:
        db.add(CompanyProfile(tenant_id=tenant["tenant_id"], profile_name="Main", is_default=True,
                              company_name="Edit Co", postal_address="1 Main St, Springfield"))
        db.commit()
    return tenant


@pytest.fixture(scope="module")
def stranger(client):
    return register_tenant(client, "aedito")


@pytest.fixture(autouse=True)
def _no_daily_limit(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)


def _inbox(client, h, limit=None):
    domain = f"ae-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass",
        "warmup_enabled": False, "delay_between_emails": 60})
    assert r.status_code == 201, r.text
    iid = r.json()["inbox_id"]
    if limit is not None:
        sql("UPDATE sending_inboxes SET max_emails_per_day = :n WHERE inbox_id = :i", n=limit, i=iid)
    return iid


def _launched(client, h, n_contacts=2, steps=2, inbox_limit=None):
    """An ACTIVE campaign with ``steps`` steps (generic + CFO version of step 1) and enrolled contacts."""
    iid = _inbox(client, h, inbox_limit)
    r = client.post("/campaigns", headers=h, json={
        "campaign_name": f"Edit {uuid.uuid4().hex[:6]} {RUN}", "inbox_ids": [iid], "sender_name": "Ann"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    with SessionLocal() as db:
        for number in range(1, steps + 1):
            step = EmailSequence(campaign_id=cid, step_number=number, wait_days=0 if number == 1 else 2)
            db.add(step)
            db.flush()
            db.add(EmailTemplate(campaign_id=cid, sequence_id=step.sequence_id, subject=f"Step {number} {{{{first_name}}}}",
                                 body=f"Hello {{{{first_name}}}} from step {number}"))
            if number == 1:
                db.add(EmailTemplate(campaign_id=cid, sequence_id=step.sequence_id, designation="CFO",
                                     subject="CFO hello", body="Numbers for you {{first_name}}"))
        db.commit()
    pids = []
    for i in range(n_contacts):
        r = client.post("/contacts", headers=h, json={
            "email": f"c{i}.{uuid.uuid4().hex[:6]}.{RUN}@aeclient{RUN}.example.com", "first_name": f"Cara{i}"})
        assert r.status_code == 201, r.text
        pids.append(r.json()["prospect_id"])
    assert client.post(f"/campaigns/{cid}/prospects", headers=h, json={"prospect_ids": pids}).status_code == 200
    r = client.post(f"/campaigns/{cid}/launch", headers=h)
    assert r.status_code == 200, r.text
    return cid, pids, r.json()


def _version(client, h, cid):
    return client.get(f"/campaigns/{cid}", headers=h).json()["row_version"]


def _steps(client, h, cid):
    return client.get(f"/campaigns/{cid}/sequences", headers=h).json()["steps"]


def _edit(client, h, cid, body, check=False):
    body = {"expected_version": _version(client, h, cid), **body}
    return client.post(f"/campaigns/{cid}/edits{'/checks' if check else ''}", headers=h, json=body)


def test_a2_content_edit_rerenders_unsent_but_not_sent(client, ed):
    h = ed["headers"]
    cid, pids, _ = _launched(client, h)
    sent_id = sql("SELECT message_id FROM email_messages WHERE campaign_id = :c AND prospect_id = :p "
                  "ORDER BY scheduled_at LIMIT 1", c=cid, p=pids[0])
    sql("UPDATE email_messages SET status = 'SENT', sent_at = UTC_TIMESTAMP(), subject = 'old sent' "
        "WHERE message_id = :m", m=sent_id)
    steps = _steps(client, h, cid)
    tid = sql("SELECT template_id FROM email_templates WHERE sequence_id = :s AND designation IS NULL",
              s=steps[1]["sequence_id"])
    payload = {"steps": [{"sequence_id": steps[0]["sequence_id"]},
                         {"sequence_id": steps[1]["sequence_id"],
                          "templates": [{"template_id": tid, "subject": "New follow-up {{first_name}}",
                                         "body": "Updated {{first_name}} {{signature_block}}"}]}]}
    check = _edit(client, h, cid, payload, check=True)
    assert check.status_code == 200, check.text
    assert check.json()["dry_run"] is True and check.json()["unsent_emails_affected"] == 2
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c AND subject LIKE 'New follow-up%'",
               c=cid) == 0  # dry run wrote nothing
    r = _edit(client, h, cid, payload)
    assert r.status_code == 200, r.text
    assert r.json()["emails_rerendered"] == 2 and r.json()["campaign"]["row_version"] >= 2
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id = :c AND subject LIKE 'New follow-up Cara%'",
               c=cid) == 2
    assert sql("SELECT subject FROM email_messages WHERE message_id = :m", m=sent_id) == "old sent"
    # sender name is a content field: every unsent email follows
    r = _edit(client, h, cid, {"settings": {"sender_name": "Bob"}})
    assert r.status_code == 200 and r.json()["emails_rerendered"] == 3


def test_a2_stale_version_is_409_and_version_required(client, ed):
    h = ed["headers"]
    cid, _, _ = _launched(client, h, n_contacts=1)
    v = _version(client, h, cid)
    assert client.post(f"/campaigns/{cid}/edits", headers=h, json={"expected_version": v,
                                                                 "settings": {"sender_title": "VP"}}).status_code == 200
    r = client.post(f"/campaigns/{cid}/edits", headers=h, json={"expected_version": v, "settings": {"sender_title": "X"}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "STALE_VERSION"
    assert client.post(f"/campaigns/{cid}/edits", headers=h, json={"settings": {}}).status_code == 428


def test_a2_start_date_locked_and_completed_refused(client, ed):
    h = ed["headers"]
    cid, _, _ = _launched(client, h, n_contacts=1)
    r = _edit(client, h, cid, {"settings": {"start_date": "2030-01-01"}})
    assert r.status_code == 400 and "start date" in r.json()["detail"]
    sql("UPDATE campaigns SET status = 'COMPLETED' WHERE campaign_id = :c", c=cid)
    assert _edit(client, h, cid, {"settings": {"sender_title": "x"}}).status_code == 409


def test_a2_schedule_edit_moves_unsent_in_step_order(client, ed):
    h = ed["headers"]
    cid, pids, _ = _launched(client, h, n_contacts=2)
    steps = _steps(client, h, cid)
    r = _edit(client, h, cid, {"steps": [{"sequence_id": steps[0]["sequence_id"]},
                                         {"sequence_id": steps[1]["sequence_id"], "wait_days": 9}],
                               "settings": {"wait_business_days": True}})
    assert r.status_code == 200, r.text
    assert r.json()["emails_rescheduled"] >= 2
    for pid in pids:
        gap = sql("SELECT TIMESTAMPDIFF(DAY, MIN(scheduled_at), MAX(scheduled_at)) FROM email_messages "
                  "WHERE campaign_id = :c AND prospect_id = :p", c=cid, p=pid)
        assert gap >= 9  # 9 business days >= 9 calendar days
    assert client.get(f"/campaigns/{cid}", headers=h).json()["wait_business_days"] is True


def test_a2_add_step_backfills_and_delete_rules(client, ed):
    h = ed["headers"]
    cid, pids, _ = _launched(client, h, n_contacts=2)
    steps = _steps(client, h, cid)
    s1, s2 = steps[0]["sequence_id"], steps[1]["sequence_id"]
    # contact 0 already got step 2: a step 2b inserted before it is not sent to them
    sql("UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE campaign_id=:c AND prospect_id=:p",
        c=cid, p=pids[0])
    new_step = {"wait_days": 1, "templates": [{"subject": "Inserted {{first_name}}", "body": "Inserted body"}]}
    r = _edit(client, h, cid, {"steps": [{"sequence_id": s1}, new_step, {"sequence_id": s2}]})
    assert r.status_code == 200, r.text
    assert r.json()["steps_added"] == [2] and r.json()["emails_created"] == 1
    numbers = [s["step_number"] for s in _steps(client, h, cid)]
    assert numbers == [1, 2, 3]
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id=:c AND subject LIKE 'Inserted%'", c=cid) == 1
    # the inserted email goes before the contact's (old) step 2
    inserted = sql("SELECT scheduled_at FROM email_messages WHERE campaign_id=:c AND prospect_id=:p AND "
                   "subject LIKE 'Inserted%'", c=cid, p=pids[1])
    later = sql("SELECT scheduled_at FROM email_messages WHERE campaign_id=:c AND prospect_id=:p AND sequence_id=:s",
                c=cid, p=pids[1], s=s2)
    assert inserted < later
    # a sent step can't be deleted; an unsent one can, the rest is renumbered
    assert _edit(client, h, cid, {"steps": [{"sequence_id": s1}]}).status_code == 409
    new_id = _steps(client, h, cid)[1]["sequence_id"]
    r = _edit(client, h, cid, {"steps": [{"sequence_id": s1}, {"sequence_id": s2}]})
    assert r.status_code == 200, r.text
    assert r.json()["steps_deleted"] == [2] and r.json()["emails_removed"] == 1
    assert [s["step_number"] for s in _steps(client, h, cid)] == [1, 2]
    assert sql("SELECT COUNT(*) FROM email_sequences WHERE sequence_id = :s", s=new_id) == 0
    # reordering existing steps is refused
    assert _edit(client, h, cid, {"steps": [{"sequence_id": s2}, {"sequence_id": s1}]}).status_code == 400


def test_a2_inbox_change_rebinds_unsent(client, ed):
    h = ed["headers"]
    cid, _, _ = _launched(client, h, n_contacts=3)
    new_inbox = _inbox(client, h)
    r = _edit(client, h, cid, {"inbox_ids": [new_inbox]})
    assert r.status_code == 200, r.text
    assert r.json()["emails_rebound"] > 0
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id=:c AND inbox_id <> :i", c=cid, i=new_inbox) == 0
    assert _edit(client, h, cid, {"inbox_ids": []}).status_code == 400


def test_a2_preview_and_template_assignments(client, ed, stranger):
    h = ed["headers"]
    cid, pids, _ = _launched(client, h, n_contacts=1)
    r = client.post(f"/campaigns/{cid}/email-previews", headers=h, json={"prospect_id": pids[0], "step_number": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "scheduled_email" and body["subject"] == "Step 1 Cara0"
    assert "1 Main St, Springfield" in body["body"] and "unsubscribe" in body["body"]
    r = client.post(f"/campaigns/{cid}/email-previews", headers=h, json={
        "prospect_id": pids[0], "step_number": 2, "subject": "Draft {{first_name}}", "body": "Hi"})
    assert r.json()["source"] == "draft" and r.json()["subject"] == "Draft Cara0"
    assert client.post(f"/campaigns/{cid}/email-previews", headers=stranger["headers"],
                       json={"prospect_id": pids[0], "step_number": 1}).status_code == 404
    r = client.get(f"/campaigns/{cid}/template-assignments", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 1 and len(data["steps"]) == 2
    assert {t["designation"] for t in data["steps"][0]["templates"]} == {None, "CFO"}
    assert sum(t["recipients"] for t in data["steps"][0]["templates"]) == 1
    assert data["prospects"][0]["assignments"][0]["source"] == "email"
    assert client.get(f"/campaigns/{cid}/template-assignments", headers=stranger["headers"]).status_code == 404
    # a new persona version (generic) for step 2 is re-picked for the contact
    s2 = data["steps"][1]["sequence_id"]
    r = _edit(client, h, cid, {"steps": [{"sequence_id": data["steps"][0]["sequence_id"]}, {
        "sequence_id": s2, "templates": [{"designation": "OTHER", "subject": "Other {{first_name}}", "body": "x"}]}]})
    assert r.status_code == 200, r.text
    assert sql("SELECT subject FROM email_messages WHERE campaign_id=:c AND sequence_id=:s", c=cid, s=s2) \
        == "Other Cara0"


def test_a2_launch_pacing_warning(client, ed):
    h = ed["headers"]
    _, _, launch = _launched(client, h, n_contacts=3, inbox_limit=1)
    warning = launch["pacing_warning"]
    assert warning and warning["capacity_per_day"] == 1 and warning["planned_peak_per_day"] >= 2
    _, _, launch = _launched(client, h, n_contacts=1)
    assert launch["pacing_warning"] is None


def test_a2_delete_step_route_refuses_sent(client, ed):
    h = ed["headers"]
    cid, pids, _ = _launched(client, h, n_contacts=1, steps=3)
    steps = _steps(client, h, cid)
    sql("UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE campaign_id=:c AND sequence_id=:s",
        c=cid, s=steps[0]["sequence_id"])
    assert client.delete(f"/campaigns/sequences/{steps[0]['sequence_id']}", headers=h).status_code == 409
    assert client.delete(f"/campaigns/sequences/{steps[1]['sequence_id']}", headers=h).status_code == 204
    assert [s["step_number"] for s in _steps(client, h, cid)] == [1, 2]
