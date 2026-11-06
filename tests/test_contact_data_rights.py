"""
Data rights on one contact (UAT gap G2, G3).

  * G2: the contact record says until when an unsubscribe blocks new campaigns (DEC-14) and that an
    expired block does not restart email while consent stays UNSUBSCRIBED; re-opting in lifts it
  * G3: GET /contacts/{id}/data-export, the data-access request export: everything held about the
    person in one JSON file, admins with export_contacts only (403 for agents, managers and an admin
    without the permission), 404 outside the workspace, one export-log row per export
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest

from app.core.clock import utcnow
from app.core.config import settings
from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    t = register_tenant(client, "dsar")
    t["user_id"] = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    return t


def _uid():
    return uuid.uuid4().hex[:6]


def _contact(client, ws, **kw):
    email = f"dora.{_uid()}@dsar{RUN}.example.com"
    r = client.post("/contacts", headers=ws["headers"], json={"email": email, "first_name": "Dora", "last_name": "Sar",
                                                               **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"], email


def _user(client, ws, role, permissions=None):
    from app.core.security import hash_password
    from app.models.user import User
    email = f"{role.lower()}-{_uid()}-{RUN}@dsar{RUN}.example.com"
    password = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(User(tenant_id=ws["tenant_id"], first_name=role.title(), last_name="User", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True, custom_permissions=permissions))
        db.commit()
    return auth(login(client, email, password))


def _utc(value):
    assert value.endswith("Z"), value
    return datetime.fromisoformat(value[:-1])


# ── G2 ──────────────────────────────────────────────────────────────

def test_contact_detail_shows_until_when_an_unsubscribe_blocks(client, ws, monkeypatch):
    monkeypatch.setattr(settings, "UNSUBSCRIBE_SUPPRESSION_HOURS", 0)
    monkeypatch.setattr(settings, "UNSUBSCRIBE_SUPPRESSION_DAYS", 30)
    h = ws["headers"]
    pid, email = _contact(client, ws)
    detail = client.get(f"/contacts/{pid}", headers=h).json()
    assert detail["suppression"] is None and detail["suppression_expires_at"] is None

    r = client.patch(f"/contacts/{pid}", headers=h, json={"consent_status": "UNSUBSCRIBED"})
    assert r.status_code == 200, r.text
    detail = client.get(f"/contacts/{pid}", headers=h).json()
    s = detail["suppression"]
    assert s["active"] is True
    until = _utc(detail["suppression_expires_at"])
    assert until == _utc(s["expires_at"])
    assert timedelta(days=29) < until - utcnow() <= timedelta(days=30, minutes=1)

    # after the date the block no longer applies, but the contact stays unsubscribed (no email restarts)
    sql("UPDATE global_unsubscribes SET suppression_expires_at = :t WHERE tenant_id = :tn AND email = :e",
        t=utcnow() - timedelta(days=1), tn=ws["tenant_id"], e=email.lower())
    detail = client.get(f"/contacts/{pid}", headers=h).json()
    assert detail["suppression"]["active"] is False and detail["consent_status"] == "UNSUBSCRIBED"

    r = client.patch(f"/contacts/{pid}", headers=h, json={"consent_status": "OPT_IN"})
    assert r.status_code == 200, r.text
    assert client.get(f"/contacts/{pid}", headers=h).json()["suppression"] is None


# ── G3 ──────────────────────────────────────────────────────────────

def _seed_history(client, ws, pid, email):
    h = ws["headers"]
    r = client.post("/contacts/fields", headers=h, json={"label": f"Shoe size {_uid()}", "field_type": "NUMBER"})
    assert r.status_code == 201, r.text
    key = r.json()["field_key"]
    assert client.patch(f"/contacts/{pid}", headers=h, json={"custom_fields": {key: 42}}).status_code == 200
    assert client.patch(f"/contacts/{pid}", headers=h, json={"lifecycle_stage": "MQL"}).status_code == 200
    assert client.post(f"/contacts/{pid}/activities", headers=h,
                       json={"activity_type": "NOTE", "body": "Asked for their data"}).status_code == 201
    assert client.post(f"/contacts/{pid}/activities", headers=h,
                       json={"activity_type": "CALL", "subject": "Intro call"}).status_code == 201
    r = client.post("/tasks", headers=h, json={"title": "Send the export", "prospect_id": pid})
    assert r.status_code == 201, r.text
    r = client.post("/lists", headers=h, json={"list_name": f"DSAR {_uid()} {RUN}", "list_type": "STATIC"})
    assert r.status_code == 201, r.text
    list_id = r.json()["list_id"]
    assert client.post(f"/lists/{list_id}/members", headers=h, json={"prospect_ids": [pid]}).status_code == 200
    from app.models.email_message import EmailMessage
    sent = datetime(2026, 10, 1, 9, 30)
    with SessionLocal() as db:
        db.add_all([
            EmailMessage(tenant_id=ws["tenant_id"], prospect_id=pid, to_email=email, from_email="rep@example.com",
                         direction="OUTBOUND", status="DELIVERED", subject="Hello Dora", body_text="Our offer",
                         sent_at=sent),
            EmailMessage(tenant_id=ws["tenant_id"], prospect_id=pid, to_email="rep@example.com", from_email=email,
                         direction="INBOUND", status="RECEIVED", subject="Re: Hello Dora",
                         body_text="Please send me my data", sent_at=sent + timedelta(hours=2)),
        ])
        db.commit()
    return key, list_id


def _check_crm_records(data, email, key, account_id, list_id):
    assert data["contact"]["email"] == email.lower() and data["contact"]["first_name"] == "Dora"
    assert data["contact"]["created_at"].endswith("Z")
    assert {"key": key, "value": 42} == {k: v for k, v in data["custom_fields"][0].items() if k != "label"}
    assert [(c["account_id"], c["is_primary"]) for c in data["companies"]] == [(account_id, True)]
    assert [lst["list_id"] for lst in data["lists"]] == [list_id]
    assert [n["body"] for n in data["notes"]] == ["Asked for their data"]
    assert [a["subject"] for a in data["activities"]] == ["Intro call"]
    assert [t["title"] for t in data["tasks"]] == ["Send the export"]


def _check_emails_and_consent(data, key):
    emails = {e["direction"]: e for e in data["emails"]}
    assert emails["OUTBOUND"]["subject"] == "Hello Dora" and emails["OUTBOUND"]["body"] == "Our offer"
    assert emails["OUTBOUND"]["sent_at"] == "2026-10-01T09:30:00Z" and emails["OUTBOUND"]["status"] == "DELIVERED"
    assert emails["INBOUND"]["body"] == "Please send me my data"
    assert data["consent"]["consent_status"] == "OPT_IN" and data["consent"]["suppression"] is None
    assert [h["new_value"] for h in data["lifecycle_history"]] == ["Marketing qualified lead"]
    assert any(key in str(h["field"]) for h in data["property_history"])


def _check_export_log(ws, pid, email, before):
    """One export-log row, which names the request but holds none of the exported data."""
    assert sql("SELECT COUNT(*) FROM export_logs WHERE tenant_id = :t AND format = 'json'",
               t=ws["tenant_id"]) == before + 1
    summary = sql("SELECT filter_summary FROM export_logs WHERE tenant_id = :t AND format = 'json' "
                  "ORDER BY created_at DESC LIMIT 1", t=ws["tenant_id"])
    assert pid in summary and email.lower() not in summary.lower()
    row = sql("SELECT CONCAT_WS('|', object_type, row_count, user_id) FROM export_logs WHERE tenant_id = :t "
              "AND format = 'json' ORDER BY created_at DESC LIMIT 1", t=ws["tenant_id"])
    assert row == f"CONTACT|1|{ws['user_id']}"


def test_data_export_holds_everything_about_the_person(client, ws):
    h = ws["headers"]
    r = client.post("/accounts", headers=h, json={"name": f"Sar Holdings {_uid()} {RUN}"})
    assert r.status_code == 201, r.text
    account_id = r.json()["account_id"]
    pid, email = _contact(client, ws, account_id=account_id)
    key, list_id = _seed_history(client, ws, pid, email)
    before = sql("SELECT COUNT(*) FROM export_logs WHERE tenant_id = :t AND format = 'json'", t=ws["tenant_id"])

    r = client.get(f"/contacts/{pid}/data-export", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"] and ".json" in r.headers["content-disposition"]
    data = json.loads(r.content)
    _check_crm_records(data, email, key, account_id, list_id)
    _check_emails_and_consent(data, key)
    _check_export_log(ws, pid, email, before)


def test_data_export_is_for_admins_who_may_export(client, ws):
    pid, _ = _contact(client, ws)
    before = sql("SELECT COUNT(*) FROM export_logs WHERE tenant_id = :t AND format = 'json'", t=ws["tenant_id"])
    agent = _user(client, ws, "AGENT", ["manage_prospects", "export_contacts"])
    manager = _user(client, ws, "MANAGER")
    admin_without = _user(client, ws, "ADMIN", ["manage_prospects"])
    for h in (agent, manager, admin_without):
        assert client.get(f"/contacts/{pid}/data-export", headers=h).status_code == 403
    assert client.get(f"/contacts/{pid}/data-export", headers=_user(client, ws, "ADMIN")).status_code == 200
    assert sql("SELECT COUNT(*) FROM export_logs WHERE tenant_id = :t AND format = 'json'",
               t=ws["tenant_id"]) == before + 1


def test_data_export_of_another_workspace_contact_is_404(client, ws):
    other = register_tenant(client, "dsarx")
    pid, _ = _contact(client, other)
    assert client.get(f"/contacts/{pid}/data-export", headers=ws["headers"]).status_code == 404
    assert client.get(f"/contacts/{uuid.uuid4()}/data-export", headers=ws["headers"]).status_code == 404
