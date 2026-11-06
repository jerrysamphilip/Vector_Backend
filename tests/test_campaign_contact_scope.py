"""
A campaign's contact-level views follow the contacts visibility rule (BR-SH-02): a user scoped to a team
(sales level 2+) sees only the enrolled contacts they may see on the Contacts page, in the campaign's
prospects list, its schedule timeline and its per-step replies; admins see everyone. Campaign-level
counts are unchanged.
"""
import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from tests.conftest import RUN, SessionLocal, auth, login, register_tenant


def _uid():
    return uuid.uuid4().hex[:6]


def _user(client, tenant_id, label, level, manager_id=None):
    from app.core.security import hash_password
    from app.models.user import User
    email = f"cs-{label}-{RUN}-{_uid()}@cs{RUN}.example.com"
    password = f"cs-{label}-pass-{RUN}-{_uid()}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.title(), last_name="Rep", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                    email_verified=True, sales_level=level, manager_id=manager_id)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


@pytest.fixture(scope="module")
def world(client):
    from app.models.campaign import CampaignProspect
    from app.models.conversation import Conversation
    from app.models.email_message import EmailMessage
    from app.models.email_sequence import EmailSequence
    from app.models.prospect import Prospect

    t = register_tenant(client, "cscope")
    tid, h = t["tenant_id"], t["headers"]
    x3 = _user(client, tid, "xlead", 3)
    x4 = _user(client, tid, "xrep", 4, x3["user_id"])
    y3 = _user(client, tid, "ylead", 3)
    y4 = _user(client, tid, "yrep", 4, y3["user_id"])
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@cs-{_uid()}{RUN}.example.com", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass", "imap_host": "127.0.0.1",
        "imap_port": 1, "imap_username": "sender", "imap_password": "fixture-imap-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Scope {_uid()} {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    owners = {"x3": x3["user_id"], "x4": x4["user_id"], "y4": y4["user_id"]}
    contacts = {}
    with SessionLocal() as db:
        step = EmailSequence(campaign_id=cid, step_number=1, wait_days=0)
        db.add(step)
        db.flush()
        for key, owner in owners.items():
            p = Prospect(tenant_id=tid, email=f"{key}.{_uid()}.{RUN}@csclient{RUN}.example.com",
                         first_name=key.upper(), owner_id=owner, consent_status="OPT_IN")
            db.add(p)
            db.flush()
            contacts[key] = p.prospect_id
            db.add(CampaignProspect(campaign_id=cid, prospect_id=p.prospect_id, tenant_id=tid, status="ACTIVE"))
            conversation = Conversation(tenant_id=tid, prospect_id=p.prospect_id, inbox_id=inbox_id, subject="Hi",
                                        last_message_at=utcnow())
            db.add(conversation)
            db.flush()
            conv = conversation.id
            sent = utcnow() - timedelta(hours=2)
            db.add(EmailMessage(campaign_id=cid, prospect_id=p.prospect_id, sequence_id=step.sequence_id,
                                direction="OUTBOUND", status="SENT", subject="Hi", body_text="Hello",
                                to_email=p.email, sent_at=sent, scheduled_at=sent, conversation_id=conv))
            db.add(EmailMessage(campaign_id=cid, prospect_id=p.prospect_id, direction="INBOUND", status="RECEIVED",
                                subject="Re: Hi", body_text=f"reply from {key}", from_email=p.email,
                                to_email="sender@cs.example.com", sent_at=sent + timedelta(minutes=5),
                                conversation_id=conv))
        db.commit()
    return {"cid": cid, "admin": h, "x3": x3, "x4": x4, "y3": y3, "y4": y4, "contacts": contacts}


def _prospect_ids(client, world, headers):
    r = client.get(f"/campaigns/{world['cid']}/prospects", headers=headers)
    assert r.status_code == 200, r.text
    return {p["prospect_id"] for p in r.json()}


def _schedule(client, world, headers):
    r = client.get(f"/campaigns/{world['cid']}/schedule", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {p["name"] for day in body["schedule"] for p in day["prospects"]}
    return names, sum(day["prospect_count"] for day in body["schedule"])


def _reply_bodies(client, world, headers):
    r = client.get(f"/campaigns/{world['cid']}/sequences/1/emails", headers=headers)
    assert r.status_code == 200, r.text
    return {m["body_text"] for m in r.json()["recent_replies"]}


def test_scoped_users_see_only_their_teams_enrolled_contacts(client, world):
    c = world["contacts"]
    assert _prospect_ids(client, world, world["x4"]["headers"]) == {c["x4"]}
    assert _prospect_ids(client, world, world["x3"]["headers"]) == {c["x3"], c["x4"]}
    assert _prospect_ids(client, world, world["y3"]["headers"]) == {c["y4"]}


def test_admin_sees_every_enrolled_contact(client, world):
    assert _prospect_ids(client, world, world["admin"]) == set(world["contacts"].values())
    names, count = _schedule(client, world, world["admin"])
    assert names == {"X3", "X4", "Y4"} and count == 3
    assert _reply_bodies(client, world, world["admin"]) == {"reply from x3", "reply from x4", "reply from y4"}


def test_schedule_and_step_replies_list_only_visible_contacts(client, world):
    names, count = _schedule(client, world, world["x4"]["headers"])
    assert names == {"X4"} and count == 3  # the campaign's counts, the caller's contacts
    assert _reply_bodies(client, world, world["x4"]["headers"]) == {"reply from x4"}
    assert _reply_bodies(client, world, world["y3"]["headers"]) == {"reply from y4"}


def test_prospects_export_lists_only_visible_contacts(client, world):
    from tests.conftest import sql
    x3 = world["x3"]
    sql("UPDATE users SET custom_permissions = :p WHERE user_id = :u",
        p='["manage_campaigns", "export_data"]', u=x3["user_id"])
    sql("UPDATE campaigns SET created_by = :u WHERE campaign_id = :c", u=x3["user_id"], c=world["cid"])
    r = client.get(f"/reports/export/prospects/{world['cid']}", headers=x3["headers"])
    assert r.status_code == 200, r.text
    lines = r.text.strip().splitlines()[1:]
    assert sorted(line.split(",")[1] for line in lines) == ["X3", "X4"]
    r = client.get(f"/reports/export/prospects/{world['cid']}", headers=world["admin"])
    assert r.status_code == 200 and len(r.text.strip().splitlines()) == 4, r.text
