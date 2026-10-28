"""
Campaign P2 features against MySQL: duplicate (S-12), clearing fields (S-23), enrolment dry run and
extra rules (S-20), removed contacts hidden and re-enrolable (S-21), lead-list timing columns (F-12),
weighted mailbox rotation (S-18).
"""
import uuid
from collections import Counter
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant, sql
from app.core.clock import utcnow
from app.core.config import settings
from app.models.automation_rule import AutomationRule
from app.models.email_attachment import EmailAttachment
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.subject_variant import EmailSubjectVariant
from app.services.campaign_email_service import CampaignEmailService


@pytest.fixture(scope="module")
def camp(client):
    return register_tenant(client, "camp")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "campo")


@pytest.fixture(autouse=True)
def _no_daily_limit(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)


def _inbox(client, h, limit=None):
    domain = f"cp-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass",
        "warmup_enabled": False, "delay_between_emails": 0})
    assert r.status_code == 201, r.text
    iid = r.json()["inbox_id"]
    if limit is not None:
        sql("UPDATE sending_inboxes SET max_emails_per_day = :n WHERE inbox_id = :i", n=limit, i=iid)
    return iid


def _campaign(client, h, label, inbox_ids=None, status="DRAFT", **extra):
    r = client.post("/campaigns", headers=h, json={
        "campaign_name": f"{label} {uuid.uuid4().hex[:6]} {RUN}", "inbox_ids": inbox_ids or [], **extra})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    if status != "DRAFT":
        sql("UPDATE campaigns SET status = :s WHERE campaign_id = :c", s=status, c=cid)
    return cid


def _contact(client, h, label, designation=None):
    r = client.post("/contacts", headers=h, json={
        "email": f"{label}.{uuid.uuid4().hex[:6]}.{RUN}@campclient{RUN}.example.com",
        "first_name": label.title(), "designation": designation})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _steps(cid, n=2, designations=(None,)):
    """n steps, one template per designation per step; returns [(sequence_id, [template_id...])]."""
    out = []
    with SessionLocal() as db:
        for number in range(1, n + 1):
            step = EmailSequence(campaign_id=cid, step_number=number, wait_days=0 if number == 1 else 2,
                                 stop_on_reply=number != n)
            db.add(step)
            db.flush()
            tids = []
            for d in designations:
                t = EmailTemplate(campaign_id=cid, sequence_id=step.sequence_id, designation=d,
                                  subject=f"S{number} {d}", body="Hi {{first_name}}")
                db.add(t)
                db.flush()
                tids.append(t.template_id)
            out.append((step.sequence_id, tids))
        db.commit()
    return out


def _enrol(client, h, cid, pids):
    r = client.post(f"/campaigns/{cid}/prospects", headers=h, json={"prospect_ids": pids})
    assert r.status_code == 200, r.text
    return r.json()


# ── S-12 duplicate ───────────────────────────────────────────────────────────────────────────────

def test_camp_duplicate_copies_content_and_overrides(client, camp):
    h = camp["headers"]
    iid = _inbox(client, h)
    src = _campaign(client, h, "Source", [iid], sender_title="VP Sales", cta_link="https://cal.example.com/x",
                    daily_batch_size=40)
    steps = _steps(src, 2, designations=(None, "CFO"))
    with SessionLocal() as db:
        db.add(EmailSubjectVariant(tenant_id=camp["tenant_id"], campaign_id=src, sequence_id=steps[0][0],
                                   label="A", subject="Variant A", weight=1, is_winner=True))
        db.add(EmailAttachment(template_id=steps[0][1][0], filename="deck.pdf", content_type="application/pdf",
                               size_bytes=10, storage_path="shared-deck.pdf"))
        db.add(AutomationRule(campaign_id=src, name="Follow", trigger_type="EMAIL_OPENED",
                              action_type="SEND_EMAIL", action_config={"template_id": steps[1][1][0]}))
        db.commit()
    in_source = _contact(client, h, "insource")
    _enrol(client, h, src, [in_source])
    fresh = _contact(client, h, "fresh", designation="Chief Technology Officer")

    r = client.post(f"/campaigns/{src}/duplicates", headers=h, json={
        "campaign_name": "My copy", "prospect_ids": [in_source, fresh]})
    assert r.status_code == 201, r.text
    body = r.json()
    copy = body["campaign"]
    assert copy["status"] == "DRAFT" and copy["campaign_name"] == "My copy"
    assert copy["source_campaign_id"] == src and body["source_campaign_id"] == src
    assert copy["sender_title"] == "VP Sales" and copy["daily_batch_size"] == 40
    assert [i["inbox_id"] for i in copy["sending_inboxes"]] == [iid]
    assert body["copied"] == {"sequences": 2, "templates": 4, "subject_variants": 1, "attachments": 1,
                              "automation_rules": 1}
    assert body["enrollment"]["enrolled_count"] == 1
    assert body["enrollment"]["rejected_summary"] == {"in_source_campaign": 1}
    new = copy["campaign_id"]
    assert sql("SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id = :c", c=new) == 1
    # The new rule points at the copied template, not the source's
    tid = sql("SELECT JSON_UNQUOTE(JSON_EXTRACT(action_config, '$.template_id')) FROM automation_rules "
              "WHERE campaign_id = :c", c=new)
    assert sql("SELECT campaign_id FROM email_templates WHERE template_id = :t", t=tid) == new
    assert sql("SELECT is_winner FROM email_subject_variants WHERE campaign_id = :c", c=new) == 0
    assert sql("SELECT storage_path FROM email_attachments a JOIN email_templates t ON t.template_id = a.template_id "
               "WHERE t.campaign_id = :c", c=new) == "shared-deck.pdf"
    # Nothing copied into or changed on the source
    assert sql("SELECT COUNT(*) FROM email_templates WHERE campaign_id = :c", c=src) == 4
    assert isinstance(body["persona_gaps"], list)


def test_camp_duplicate_default_name_and_persona_gaps(client, camp):
    h = camp["headers"]
    src = _campaign(client, h, "Gaps")
    _steps(src, 2, designations=("CFO",))  # no generic template
    pid = _contact(client, h, "gap", designation="Software Engineer")
    r = client.post(f"/campaigns/{src}/duplicates", headers=h, json={"prospect_ids": [pid]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["campaign"]["campaign_name"].startswith("Copy of Gaps")
    gaps = body["persona_gaps"]
    assert len(gaps) == 1 and gaps[0]["missing_steps"] == [1, 2] and gaps[0]["prospect_count"] == 1


def test_camp_duplicate_tenant_scoped_and_validated(client, camp, other):
    src = _campaign(client, camp["headers"], "Private")
    r = client.post(f"/campaigns/{src}/duplicates", headers=other["headers"], json={})
    assert r.status_code == 404, r.text
    r = client.post(f"/campaigns/{src}/duplicates", headers=camp["headers"],
                    json={"start_date": "2001-01-01"})
    assert r.status_code == 400, r.text
    other_inbox = _inbox(client, other["headers"])
    r = client.post(f"/campaigns/{src}/duplicates", headers=camp["headers"], json={"inbox_ids": [other_inbox]})
    assert r.status_code == 400, r.text
    r = client.post(f"/campaigns/{src}/duplicates", headers=camp["headers"], json={"campaign_timezone": "Mars/X"})
    assert r.status_code == 400, r.text
    # Nothing was left behind by the refused copies
    assert sql("SELECT COUNT(*) FROM campaigns WHERE source_campaign_id = :c", c=src) == 0


def test_camp_delete_source_keeps_copy(client, camp):
    h = camp["headers"]
    src = _campaign(client, h, "Doomed")
    _steps(src, 1)
    copy = client.post(f"/campaigns/{src}/duplicates", headers=h, json={}).json()["campaign"]["campaign_id"]
    assert client.delete(f"/campaigns/{src}", headers=h).status_code == 204
    assert sql("SELECT source_campaign_id FROM campaigns WHERE campaign_id = :c", c=copy) is None


# ── S-23 clearing fields ─────────────────────────────────────────────────────────────────────────

def test_camp_update_clears_with_null_and_keeps_absent(client, camp):
    h = camp["headers"]
    cid = _campaign(client, h, "Clear", sender_title="CTO", cta_link="https://cal.example.com/a",
                    batch_size=5, daily_batch_size=50, start_date="2030-01-01", end_date="2030-02-01")
    r = client.put(f"/campaigns/{cid}", headers=h, json={"campaign_name": "Renamed " + RUN})
    assert r.status_code == 200, r.text
    assert r.json()["sender_title"] == "CTO" and r.json()["batch_size"] == 5
    r = client.put(f"/campaigns/{cid}", headers=h, json={
        "sender_title": None, "cta_link": None, "batch_size": None, "daily_batch_size": None,
        "start_date": None, "end_date": None, "campaign_timezone": None, "expected_version": r.json()["row_version"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sender_title"] is None and body["cta_link"] is None
    assert body["batch_size"] is None and body["daily_batch_size"] is None
    assert body["start_date"] is None and body["end_date"] is None
    assert body["campaign_timezone"] == "UTC"  # null ignored for a field that cannot be empty
    # Validation kept: end before start (against the stored start) and out-of-range values
    client.put(f"/campaigns/{cid}", headers=h, json={"start_date": "2030-03-01"})
    r = client.put(f"/campaigns/{cid}", headers=h, json={"end_date": "2030-02-01"})
    assert r.status_code == 400, r.text
    assert client.put(f"/campaigns/{cid}", headers=h, json={"batch_size": 0}).status_code == 422
    # Stale row_version still 409
    assert client.put(f"/campaigns/{cid}", headers=h, json={"sender_name": "x", "expected_version": 1}
                      ).status_code == 409


# ── S-20 enrolment dry run + extra rules ─────────────────────────────────────────────────────────

def _positive_reply(cid, pid):
    with SessionLocal() as db:
        msg = EmailMessage(campaign_id=cid, prospect_id=pid, direction="INBOUND", status="RECEIVED",
                           to_email="me@x.example.com", subject="Re", body_text="Yes please")
        db.add(msg)
        db.flush()
        db.add(EmailEvent(message_id=msg.message_id, event_type=EmailEvent.EVENT_POSITIVE_REPLY))
        db.commit()


def test_camp_enrollment_check_matches_real_enrolment(client, camp, other):
    h = camp["headers"]
    busy, keen, ok = (_contact(client, h, n) for n in ("busy", "keen", "okc"))
    running = _campaign(client, h, "Running", status="ACTIVE")
    _enrol(client, h, running, [busy])
    _positive_reply(running, keen)
    sql("UPDATE campaign_prospects SET status = 'REPLIED' WHERE prospect_id = :p", p=keen)

    target = _campaign(client, h, "Target")
    r = client.post(f"/campaigns/{target}/enrollments/checks", headers=h,
                    json={"prospect_ids": [busy, keen, ok, "nope"]})
    assert r.status_code == 200, r.text
    check = r.json()
    reasons = {x["prospect_id"]: x["reason_code"] for x in check["rejected"]}
    assert reasons == {busy: "active_elsewhere", keen: "replied_positive", "nope": "not_found"}
    assert check["would_enroll_count"] == 1 and check["would_enroll"][0]["prospect_id"] == ok
    assert sql("SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id = :c", c=target) == 0  # dry run

    real = _enrol(client, h, target, [busy, keen, ok, "nope"])
    assert real["enrolled_count"] == 1
    assert {x["prospect_id"]: x["reason_code"] for x in real["rejected"]} == reasons

    # A paused campaign does not hold its contacts
    sql("UPDATE campaigns SET status = 'PAUSED' WHERE campaign_id = :c", c=running)
    r = client.post(f"/campaigns/{target}/enrollments/checks", headers=h, json={"prospect_ids": [busy]})
    assert r.json()["would_enroll_count"] == 1
    # Tenant scoped
    assert client.post(f"/campaigns/{target}/enrollments/checks", headers=other["headers"],
                       json={"prospect_ids": [ok]}).status_code == 404


def test_camp_enrollment_check_wizard_path(client, camp):
    h = camp["headers"]
    pid = _contact(client, h, "wiz")
    lid = str(uuid.uuid4())
    owner = sql("SELECT user_id FROM users WHERE email = :e", e=camp["email"])
    sql("INSERT INTO prospect_lists (list_id, tenant_id, list_name, source_type, list_type, uploaded_by) "
        "VALUES (:l, :t, :n, 'MANUAL', 'STATIC', :u)", l=lid, t=camp["tenant_id"], n=f"camp {lid[:6]}", u=owner)
    sql("INSERT INTO prospect_list_members (id, list_id, prospect_id, tenant_id) VALUES (:i, :l, :p, :t)",
        i=str(uuid.uuid4()), l=lid, p=pid, t=camp["tenant_id"])
    cid = _campaign(client, h, "Wizard check")
    r = client.post(f"/campaigns/{cid}/enrollments/checks", headers=h,
                    json={"path": "wizard", "list_id": lid, "exclude_personal_emails": False})
    assert r.status_code == 200, r.text
    assert r.json()["list_total"] == 1
    assert r.json()["would_enroll_count"] + r.json()["rejected_count"] == 1


# ── S-21 removed contacts, F-12 lead timing ──────────────────────────────────────────────────────

def test_camp_removed_hidden_and_reenrolable(client, camp):
    h = camp["headers"]
    a, b = _contact(client, h, "rema"), _contact(client, h, "remb")
    cid = _campaign(client, h, "Removal")
    _enrol(client, h, cid, [a, b])
    assert client.delete(f"/campaigns/{cid}/prospects/{a}", headers=h).status_code == 200
    listed = client.get(f"/campaigns/{cid}/prospects", headers=h).json()
    assert [p["prospect_id"] for p in listed] == [b]
    assert client.get(f"/campaigns/{cid}", headers=h).json()["prospect_count"] == 1
    full = client.get(f"/campaigns/{cid}/prospects", headers=h, params={"include_removed": True}).json()
    assert {p["prospect_id"]: p["removed"] for p in full} == {a: True, b: False}
    # Never emailed: enrolling again reactivates the kept row
    again = _enrol(client, h, cid, [a])
    assert again["enrolled_count"] == 1, again
    assert sql("SELECT status FROM campaign_prospects WHERE campaign_id = :c AND prospect_id = :p", c=cid, p=a) \
        == "ACTIVE"
    assert sql("SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id = :c", c=cid) == 2


def test_camp_removed_after_send_not_reenrolable(client, camp):
    h = camp["headers"]
    pid = _contact(client, h, "sent")
    cid = _campaign(client, h, "Removal sent")
    (seq, _), = _steps(cid, 1)
    _enrol(client, h, cid, [pid])
    with SessionLocal() as db:
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=seq, status="SENT",
                            sent_at=utcnow() - timedelta(days=3), to_email="x@y.example.com", subject="s"))
        db.commit()
    assert client.delete(f"/campaigns/{cid}/prospects/{pid}", headers=h).status_code == 200
    r = _enrol(client, h, cid, [pid])
    assert r["enrolled_count"] == 0 and r["rejected"][0]["reason_code"] == "previously_removed"


def test_camp_lead_timing_columns(client, camp):
    h = camp["headers"]
    iid = _inbox(client, h)
    pid = _contact(client, h, "timing")
    cid = _campaign(client, h, "Timing", [iid])
    steps = _steps(cid, 3)
    _enrol(client, h, cid, [pid])
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)
    sent_at = utcnow() - timedelta(days=1)
    nxt = utcnow() + timedelta(days=2)
    with SessionLocal() as db:
        first = EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=steps[0][0], status="SENT",
                             sent_at=sent_at, delivered_at=sent_at, to_email="t@y.example.com", subject="s")
        db.add(first)
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=steps[1][0], status="SCHEDULED",
                            scheduled_at=nxt, to_email="t@y.example.com", subject="s"))
        db.add(EmailMessage(campaign_id=cid, prospect_id=pid, sequence_id=steps[2][0], status="SCHEDULED",
                            scheduled_at=nxt + timedelta(days=3), to_email="t@y.example.com", subject="s"))
        db.flush()
        db.add(EmailEvent(message_id=first.message_id, event_type=EmailEvent.EVENT_REPLY_OOO))
        db.commit()
    row, = client.get(f"/campaigns/{cid}/prospects", headers=h).json()
    assert row["steps_sent"] == 1 and row["steps_total"] == 3 and row["ooo"] is True
    assert row["last_sent_at"] and row["delivered_at"] and row["next_email_at"].startswith(nxt.date().isoformat())
    # Replied: step 2 stops (stop_on_reply), step 3 does not (flag off) -> next is step 3
    sql("UPDATE campaign_prospects SET status = 'REPLIED', stopped_reason = 'Replied' WHERE campaign_id = :c",
        c=cid)
    row, = client.get(f"/campaigns/{cid}/prospects", headers=h).json()
    assert row["next_email_at"].startswith((nxt + timedelta(days=3)).date().isoformat())
    assert row["stopped_reason"] == "Replied"


# ── S-18 weighted rotation ───────────────────────────────────────────────────────────────────────

def test_camp_preschedule_weighted_rotation(client, camp):
    h = camp["headers"]
    big, small = _inbox(client, h, limit=300), _inbox(client, h, limit=100)
    cid = _campaign(client, h, "Weighted", [big, small])
    _steps(cid, 2)
    pids = [_contact(client, h, f"w{i}") for i in range(8)]
    _enrol(client, h, cid, pids)
    with SessionLocal() as db:
        CampaignEmailService(db)._preschedule_all_emails(cid)
        db.commit()
    rows = sql("SELECT COUNT(DISTINCT prospect_id) FROM email_messages WHERE campaign_id = :c AND inbox_id = :i",
               c=cid, i=big)
    assert rows == 6
    # Each contact keeps one mailbox for all its steps
    assert sql("SELECT MAX(n) FROM (SELECT COUNT(DISTINCT inbox_id) n FROM email_messages "
               "WHERE campaign_id = :c GROUP BY prospect_id) x", c=cid) == 1
    with SessionLocal() as db:
        per_inbox = Counter(i for (i,) in db.query(EmailMessage.inbox_id).filter(EmailMessage.campaign_id == cid))
    assert per_inbox[big] == 12 and per_inbox[small] == 4
