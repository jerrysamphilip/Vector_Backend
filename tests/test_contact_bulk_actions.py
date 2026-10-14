"""
Contacts bulk action 'enroll' (POST /contacts/bulk, action=enroll): selected contacts go into a
campaign through CampaignService.enroll_prospects_with_report with prospect_ids only
(list_ids=None). Covers a DRAFT campaign (enrollments, no emails yet), an ACTIVE campaign
(enrollments plus pre-scheduled step-1 emails), per-row rejections and the error paths.
"""

import pytest

from tests.conftest import RUN, register_tenant, sql


@pytest.fixture(scope="module")
def tenant_be(client):
    return register_tenant(client, "bulkenr")


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{RUN}@bulkenr{RUN}.example.com", "first_name": label.title()})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _campaign(client, h, name, with_step=False):
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"{name} {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    if with_step:
        r = client.post(f"/campaigns/{cid}/sequences", headers=h, json={"step_number": 1, "wait_days": 0})
        assert r.status_code == 201, r.text
        r = client.post("/templates", headers=h, json={
            "campaign_id": cid, "sequence_id": r.json()["sequence_id"],
            "subject": "Hello {{first_name}}", "body": "Step one body"})
        assert r.status_code == 201, r.text
    return cid


def _bulk_enroll(client, h, cid, ids):
    return client.post("/contacts/bulk", headers=h, json={
        "action": "enroll", "campaign_id": cid, "prospect_ids": ids})


def _enrolled(cid, pid):
    return sql("SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id=:c AND prospect_id=:p", c=cid, p=pid)


def _messages(cid, pid):
    return sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id=:c AND prospect_id=:p", c=cid, p=pid)


def test_bulk_enroll_into_draft_campaign(client, tenant_be):
    h = tenant_be["headers"]
    cid = _campaign(client, h, "Bulk draft")
    a, b, out = (_contact(client, h, n) for n in ("draft-a", "draft-b", "draft-out"))
    sql("UPDATE prospects SET consent_status='OPT_OUT' WHERE prospect_id=:p", p=out)

    r = _bulk_enroll(client, h, cid, [a, b, out])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolled_count"] == 2 and body["updated"] == 2
    assert body["rejected_count"] == 1 and body["rejected_summary"] == {"opted_out": 1}
    assert [x["prospect_id"] for x in body["rejected"]] == [out]
    assert body["rejected"][0]["reason_code"] == "opted_out"
    assert "daily_limit_notice" in body
    assert (_enrolled(cid, a), _enrolled(cid, b), _enrolled(cid, out)) == (1, 1, 0)
    # A draft is not launched yet: nothing is scheduled
    assert _messages(cid, a) == 0 and _messages(cid, b) == 0

    # Enrolling the same contacts again rejects each row, enrolls nobody, adds no duplicates
    r = _bulk_enroll(client, h, cid, [a, b, out])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolled_count"] == 0
    assert body["rejected_summary"] == {"already_enrolled": 2, "opted_out": 1}
    assert {x["prospect_id"]: x["reason_code"] for x in body["rejected"]} == {
        a: "already_enrolled", b: "already_enrolled", out: "opted_out"}
    assert _enrolled(cid, a) == 1 and _enrolled(cid, b) == 1


def test_bulk_enroll_into_active_campaign_schedules_emails(client, tenant_be):
    h = tenant_be["headers"]
    cid = _campaign(client, h, "Bulk active", with_step=True)
    sql("UPDATE campaigns SET status='ACTIVE' WHERE campaign_id=:c", c=cid)
    a, b, bad = (_contact(client, h, n) for n in ("act-a", "act-b", "act-bad"))
    sql("UPDATE prospects SET is_valid_email=0 WHERE prospect_id=:p", p=bad)

    r = _bulk_enroll(client, h, cid, [a, b, bad])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enrolled_count"] == 2 and body["rejected_count"] == 1
    assert body["rejected_summary"] == {"invalid_email": 1}
    assert body["rejected"][0]["prospect_id"] == bad
    assert (_enrolled(cid, a), _enrolled(cid, b), _enrolled(cid, bad)) == (1, 1, 0)
    # Launched campaign: the newly enrolled contacts get their step-1 email pre-scheduled
    assert _messages(cid, a) == 1 and _messages(cid, b) == 1 and _messages(cid, bad) == 0
    assert sql("SELECT COUNT(*) FROM email_messages WHERE campaign_id=:c AND sent_at IS NOT NULL", c=cid) == 0


def test_bulk_enroll_error_paths(client, tenant_be):
    h = tenant_be["headers"]
    pid = _contact(client, h, "err-a")
    r = _bulk_enroll(client, h, "no-such-campaign", [pid])
    assert r.status_code == 404, r.text

    cid = _campaign(client, h, "Bulk completed")
    sql("UPDATE campaigns SET status='COMPLETED' WHERE campaign_id=:c", c=cid)
    r = _bulk_enroll(client, h, cid, [pid])
    assert r.status_code == 400, r.text
    assert _enrolled(cid, pid) == 0

    # Another workspace cannot enroll into this tenant's campaign
    other = register_tenant(client, "bulkenrx")
    opid = _contact(client, other["headers"], "err-other")
    draft = _campaign(client, h, "Bulk foreign")
    r = _bulk_enroll(client, other["headers"], draft, [opid])
    assert r.status_code == 404, r.text
    assert _enrolled(draft, opid) == 0
