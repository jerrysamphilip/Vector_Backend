"""
UAT defect 1 (body), against MySQL: step bodies saved by the rich-text editor (one line of HTML) survive template
save, launch without a CTA link (the email_messages snapshot), the preview and the sent message's inbox snippet.
"""
import uuid

import pytest

from app.core.clock import utcnow
from app.core.config import settings
from app.models.company_profile import CompanyProfile
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.services.email_sender_service import EmailSenderService
from app.utils.email_utils import html_to_text
from tests.conftest import RUN, SessionLocal, register_tenant

TIPTAP_URL = ('<p>Hi {{first_name}},</p><p>AccessOS is our AI platform for Market Access, Patient Access, HUB and FRM '
              'teams at specialty pharma companies like {{company_name}}. You can see it at '
              '<a target="_blank" rel="noopener noreferrer nofollow" href="https://accessos.example.com">'
              'accessos.example.com</a>.</p><ul><li><p>Benefit verification automation</p></li>'
              '<li><p>Affordability analytics</p></li></ul>'
              '<p>Would a 20 minute walkthrough next week be useful?</p><p>Regards,</p><p>{{signature_block}}</p>')
TIPTAP_SHORT = '<p>Hi {{first_name}},</p><p>Just bumping this up. Open to a quick demo?</p>'
TIPTAP_FOOTER = ('<p>Hi {{first_name}},</p><p>Last note from me about AccessOS for {{company_name}} and your team.</p>'
                 '<p>If you do not wish to receive these emails, just reply.</p>')
EXPECTED = {
    1: ["Hi Amber,", "AccessOS is our AI platform", "like QGenda", "accessos.example.com",
        "Benefit verification automation", "Affordability analytics", "Regards,", "Amy Parks"],
    2: ["Hi Amber,"],
    3: ["Hi Amber,", "Last note from me about AccessOS for QGenda and your team."],
}


@pytest.fixture(autouse=True)
def _no_daily_limit(monkeypatch):
    monkeypatch.setattr(settings, "DAILY_NEW_CONTACT_LIMIT", 10_000)


def _text(body):
    return " ".join(html_to_text(body or "").split())


def _campaign(client, h, tenant_id):
    with SessionLocal() as db:
        db.add(CompanyProfile(tenant_id=tenant_id, profile_name="Main", is_default=True,
                              company_name="Neutrino", postal_address="1 Main St, Springfield"))
        db.commit()
    domain = f"body-{uuid.uuid4().hex[:6]}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"amy@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1, "smtp_username": "amy",
        "smtp_password": "x-pass", "imap_host": "127.0.0.1", "imap_port": 1, "imap_username": "amy",
        "imap_password": "x-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    iid = r.json()["inbox_id"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Body {RUN}", "inbox_ids": [iid],
                                                   "sender_name": "Amy Parks"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    tids = []
    with SessionLocal() as db:
        for n in (1, 2, 3):
            seq = EmailSequence(campaign_id=cid, step_number=n, wait_days=0 if n == 1 else 2)
            db.add(seq)
            db.flush()
            t = EmailTemplate(campaign_id=cid, sequence_id=seq.sequence_id, subject=f"Step {n} {{{{first_name}}}}",
                              body="placeholder")
            db.add(t)
            db.flush()
            tids.append(t.template_id)
        db.commit()
    return cid, iid, tids


@pytest.fixture
def launched(client):
    t = register_tenant(client, f"body{uuid.uuid4().hex[:6]}")
    h = t["headers"]
    cid, iid, tids = _campaign(client, h, t["tenant_id"])
    saved = {}
    for n, (tid, body) in enumerate(zip(tids, [TIPTAP_URL, TIPTAP_SHORT, TIPTAP_FOOTER]), start=1):
        r = client.put(f"/templates/{tid}", headers=h, json={"body": body})  # what the Sequence tab's Edit does
        assert r.status_code == 200, r.text
        saved[n] = r.json()["body"]
    r = client.post("/contacts", headers=h, json={"email": f"amber.{RUN}@qgenda{RUN}.example.com",
                                                  "first_name": "Amber", "company_name": "QGenda"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["prospect_id"]
    assert client.post(f"/campaigns/{cid}/prospects", headers=h, json={"prospect_ids": [pid]}).status_code == 200
    r = client.post(f"/campaigns/{cid}/launch", headers=h)
    assert r.status_code == 200, r.text
    return dict(h=h, tenant_id=t["tenant_id"], cid=cid, iid=iid, pid=pid, saved=saved)


def _messages(cid):
    with SessionLocal() as db:
        rows = (db.query(EmailMessage).filter(EmailMessage.campaign_id == cid)
                .order_by(EmailMessage.scheduled_at).all())
        db.expunge_all()
    return rows


def test_body_template_save_keeps_editor_html(launched):
    assert "Last note from me" in _text(launched["saved"][3])
    assert "AccessOS is our AI platform" in _text(launched["saved"][1])
    assert "If you do not wish to receive" not in launched["saved"][3]


def test_body_launch_snapshot_keeps_every_steps_paragraphs(launched):
    rows = _messages(launched["cid"])
    assert len(rows) == 3
    for step, row in enumerate(rows, start=1):
        text = _text(row.body_text)
        for part in EXPECTED[step]:
            assert part in text, (step, row.body_text)
    assert "href=" not in rows[0].body_text  # no CTA link configured: link markup removed, text kept
    assert "walkthrough" not in _text(rows[0].body_text) and "demo" not in _text(rows[1].body_text)


def test_body_preview_shows_every_steps_paragraphs(client, launched):
    for step in (1, 2, 3):
        r = client.post(f"/campaigns/{launched['cid']}/email-previews", headers=launched["h"],
                        json={"prospect_id": launched["pid"], "step_number": step})
        assert r.status_code == 200, r.text
        text = _text(r.json()["body"])
        for part in EXPECTED[step]:
            assert part in text, (step, r.json()["body"])


def test_body_sent_message_snippet_is_not_empty(client, launched):
    first = _messages(launched["cid"])[0]
    with SessionLocal() as db:
        msg = db.get(EmailMessage, first.message_id)
        template = db.get(EmailTemplate, msg.template_id)
        sender = EmailSenderService.__new__(EmailSenderService)
        subject, body = sender._message_content(msg, template, msg.prospect, "Amy Parks", None, None)
        assert "AccessOS is our AI platform" in _text(body)
        conv = Conversation(tenant_id=launched["tenant_id"], prospect_id=launched["pid"], inbox_id=launched["iid"],
                            subject=subject)
        db.add(conv)
        db.flush()
        msg.conversation_id, msg.status, msg.sent_at = conv.id, "SENT", utcnow()
        db.commit()
        conv_id = conv.id
    r = client.get("/conversations", headers=launched["h"])
    assert r.status_code == 200, r.text
    [item] = [c for c in r.json()["items"] if c["id"] == conv_id]
    assert item["last_message_snippet"] != "No messages"
    assert item["last_message_snippet"].startswith("Hi Amber,")
    r = client.get(f"/conversations/{conv_id}", headers=launched["h"])
    assert r.status_code == 200, r.text
    assert "AccessOS is our AI platform" in _text(r.json()["messages"][0]["body_text"])
