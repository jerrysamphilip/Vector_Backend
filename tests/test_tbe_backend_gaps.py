"""tbe functional tests (MySQL) for the remaining backend items of the team-review gap list.

  * C-17 custom-property safety: fixed data type, {value, label} options (old string rows read as such),
    DELETE deactivates (hidden from the property list, values kept and exported), reactivation, system
    properties, BOOLEAN / LONG_TEXT, max_length; tenant isolation
  * S-26 IMAP derived from SMTP: ``imap_configured`` / ``imap_derived_from_smtp`` on the inbox API
  * S-25 DNS records vs live DNS (lookups faked: no network)
  * S-17 a new contact moved off a capped mailbox takes its later steps along (rebind_unsent)
"""
import csv
import io
import uuid

import pytest

from app.core.clock import utcnow
from app.models.email_message import EmailMessage
from app.repositories.sending import SqlAlchemySendingQueueRepository
from app.services import deliverability_service as dsvc
from tests.conftest import RUN, SessionLocal, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "tbe")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "tbeb")


def _u():
    return uuid.uuid4().hex[:6]


# ── C-17 ──

def test_tbe_property_type_is_fixed_and_option_labels_can_be_renamed(client, ws):
    h = ws["headers"]
    r = client.post("/contacts/fields", headers=h, json={"label": f"Tier {_u()}", "field_type": "SELECT",
                                                          "options": ["Gold", "Silver"]})
    assert r.status_code == 201, r.text
    field = r.json()
    assert field["options"] == ["Gold", "Silver"] and field["choices"][0] == {"value": "Gold", "label": "Gold"}
    key, fid = field["field_key"], field["field_id"]
    stored = sql("SELECT options FROM contact_field_definitions WHERE field_id = :f", f=fid)
    assert "label" in stored  # stored as {value, label}
    pid = client.post("/contacts", headers=h, json={"email": f"tier.{_u()}.{RUN}@tbeclient.example.com",
                                                    "custom_fields": {key: "Gold"}}).json()["prospect_id"]

    r = client.patch(f"/contacts/fields/{fid}", headers=h, json={"label": field["label"], "field_type": "TEXT"})
    assert r.status_code == 400 and "cannot be changed" in r.text
    r = client.patch(f"/contacts/fields/{fid}", headers=h, json={
        "label": field["label"], "options": [{"value": "Gold", "label": "Gold tier"}, "Silver"]})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0] == {"value": "Gold", "label": "Gold tier"}
    # the stored value is still valid; a label is accepted and stored as its value
    assert client.get(f"/contacts/{pid}", headers=h).json()["custom_fields"][key] == "Gold"
    r = client.patch(f"/contacts/{pid}", headers=h, json={"custom_fields": {key: "silver"}})
    assert r.status_code == 200, r.text
    assert sql("SELECT JSON_UNQUOTE(JSON_EXTRACT(custom_fields, :p)) FROM prospects WHERE prospect_id = :i",
               p=f"$.{key}", i=pid) == "Silver"


def test_tbe_old_string_options_are_read_without_rewriting(client, ws):
    h = ws["headers"]
    field = client.post("/contacts/fields", headers=h, json={"label": f"Legacy {_u()}", "field_type": "RADIO",
                                                              "options": ["Yes", "No"]}).json()
    sql("UPDATE contact_field_definitions SET options = JSON_ARRAY('Yes', 'No') WHERE field_id = :f",
        f=field["field_id"])  # a row written before C-17
    listed = next(f for f in client.get("/contacts/fields", headers=h).json() if f["field_id"] == field["field_id"])
    assert listed["options"] == ["Yes", "No"] and listed["choices"][1] == {"value": "No", "label": "No"}
    assert sql("SELECT JSON_TYPE(JSON_EXTRACT(options, '$[0]')) FROM contact_field_definitions "
               "WHERE field_id = :f", f=field["field_id"]) == "STRING"


def test_tbe_delete_deactivates_keeps_values_and_exports_them(client, ws, other):
    h = ws["headers"]
    field = client.post("/contacts/fields", headers=h, json={"label": f"Notes {_u()}", "field_type": "LONG_TEXT",
                                                              "max_length": 2000}).json()
    key, fid = field["field_key"], field["field_id"]
    email = f"notes.{_u()}.{RUN}@tbeclient.example.com"
    pid = client.post("/contacts", headers=h, json={"email": email, "custom_fields": {key: "=line one\nline two"}}
                      ).json()["prospect_id"]
    assert client.delete(f"/contacts/fields/{fid}", headers=other["headers"]).status_code == 404
    r = client.delete(f"/contacts/fields/{fid}", headers=h)
    assert r.status_code == 200 and r.json() == {"status": "deactivated", "field_id": fid}
    assert sql("SELECT is_active FROM contact_field_definitions WHERE field_id = :f", f=fid) == 0
    assert fid not in {f["field_id"] for f in client.get("/contacts/fields", headers=h).json()}
    inactive = client.get("/contacts/fields", headers=h, params={"include_inactive": "true"}).json()
    assert next(f for f in inactive if f["field_id"] == fid)["is_active"] is False
    # writes leave the inactive value alone; it is still exported (formula-guarded)
    assert client.patch(f"/contacts/{pid}", headers=h, json={"custom_fields": {key: "changed"}}).status_code == 200
    assert client.get(f"/contacts/{pid}", headers=h).json()["custom_fields"][key] == "=line one\nline two"
    r = client.get("/contacts/export", headers=h, params={"format": "csv", "q": email})
    assert r.status_code == 200, r.text
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    column = rows[0].index(field["label"])
    assert rows[1][column] == "'=line one\nline two"
    # reactivate
    r = client.patch(f"/contacts/fields/{fid}", headers=h, json={"label": field["label"], "is_active": True})
    assert r.status_code == 200 and r.json()["is_active"] is True


def test_tbe_system_properties_and_new_types(client, ws):
    h = ws["headers"]
    flag = client.post("/accounts/fields", headers=h, json={"label": f"Partner {_u()}", "field_type": "BOOLEAN"})
    assert flag.status_code == 201, flag.text
    fid = flag.json()["field_id"]
    company = client.post("/accounts", headers=h, json={"name": f"Bool Co {_u()} {RUN}",
                                                        "custom_fields": {flag.json()["field_key"]: "yes"}})
    assert company.status_code == 201, company.text
    assert company.json()["custom_fields"][flag.json()["field_key"]] is True
    sql("UPDATE contact_field_definitions SET is_system = 1 WHERE field_id = :f", f=fid)
    r = client.delete(f"/accounts/fields/{fid}", headers=h)
    assert r.status_code == 400 and "System properties cannot be deactivated" in r.text
    short = client.post("/contacts/fields", headers=h, json={"label": f"Code {_u()}", "max_length": 3}).json()
    r = client.post("/contacts", headers=h, json={"email": f"code.{_u()}.{RUN}@tbeclient.example.com",
                                                  "custom_fields": {short["field_key"]: "ABCD"}})
    assert r.status_code == 400 and "at most 3 characters" in r.text


# ── S-26 ──

def test_tbe_inbox_without_imap_settings_derives_them_from_smtp(client, ws):
    h = ws["headers"]
    domain = f"tbe{_u()}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": f"smtp.{domain}", "smtp_port": 587,
        "smtp_username": f"sender@{domain}", "smtp_password": "fixture-smtp-pass", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imap_host"] is None and body["imap_configured"] is True and body["imap_derived_from_smtp"] is True
    got = client.get(f"/inboxes/{body['inbox_id']}", headers=h).json()
    assert got["imap_configured"] is True
    r = client.put(f"/inboxes/{body['inbox_id']}", headers=h, json={"smtp_host": "email-smtp.us-east-1.amazonaws.com"})
    assert r.status_code == 200 and r.json()["imap_configured"] is False
    assert sql("SELECT imap_host FROM sending_inboxes WHERE inbox_id = :i", i=body["inbox_id"]) is None  # not stored


# ── S-25 ──

def test_tbe_dns_records_compare_with_live_dns(client, ws, monkeypatch):
    h = ws["headers"]
    name = f"live{_u()}{RUN}.example.com"
    assert client.post("/deliverability/domains", headers=h, json={"domain_name": name}).status_code == 201
    live = {("TXT", f"mail.{name}"): ["v=spf1 include:_spf.google.com ~all"],
            ("TXT", f"_dmarc.{name}"): ["v=DMARC1; p=reject"]}
    monkeypatch.setattr(dsvc, "live_dns_values", lambda rtype, host: live.get((rtype, host), []))
    r = client.get(f"/deliverability/domains/{name}/dns-records", headers=h)
    assert r.status_code == 200, r.text
    records = {(x["kind"], x["host"]): x for x in r.json()["dns_records"]}
    spf = records[("SPF", f"mail.{name}")]
    assert spf["status"] == "UPDATE" and spf["current_value"] == "v=spf1 include:_spf.google.com ~all"
    assert spf["suggested_value"] == "v=spf1 include:_spf.google.com include:amazonses.com ~all"
    assert records[("DMARC", f"_dmarc.{name}")]["status"] == "OK"
    assert records[("MAIL_FROM", f"mail.{name}")]["status"] in ("ADD", "UNKNOWN")
    assert sum(r.json()["live_summary"].values()) == len(records)
    plain = client.get(f"/deliverability/domains/{name}/dns-records", headers=h, params={"live": "false"}).json()
    assert "status" not in plain["dns_records"][0]


# ── S-17 ──

def _inbox(client, h):
    domain = f"rb{_u()}{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={"email_address": f"sender@{domain}", "smtp_host": "127.0.0.1",
                                                 "smtp_port": 1, "smtp_password": "fixture", "warmup_enabled": False})
    assert r.status_code == 201, r.text
    return r.json()["inbox_id"], r.json()["email_address"]


def test_tbe_rebind_unsent_moves_only_this_contacts_pending_mail(client, ws):
    h = ws["headers"]
    (old, _), (new, new_email) = _inbox(client, h), _inbox(client, h)
    cid = client.post("/campaigns", headers=h, json={"campaign_name": f"Rebind {_u()} {RUN}"}).json()["campaign_id"]
    pids = [client.post("/contacts", headers=h, json={"email": f"rb{i}.{_u()}.{RUN}@tbeclient.example.com"}
                        ).json()["prospect_id"] for i in range(2)]
    with SessionLocal() as db:
        rows = [EmailMessage(campaign_id=cid, prospect_id=pids[0], inbox_id=old, status=s, subject="x",
                             to_email="a@tbeclient.example.com", direction="OUTBOUND",
                             sent_at=utcnow() if s == "SENT" else None)
                for s in ("QUEUED", "SCHEDULED", "SENT", "CANCELLED")]
        rows.append(EmailMessage(campaign_id=cid, prospect_id=pids[1], inbox_id=old, status="QUEUED",
                                 subject="x", to_email="b@tbeclient.example.com", direction="OUTBOUND"))
        db.add_all(rows)
        db.commit()
        ids = [m.message_id for m in rows]
        repo = SqlAlchemySendingQueueRepository(db)
        assert repo.rebind_unsent(cid, pids[0], ids[0], new, new_email) == 1
        db.commit()
    got = {mid: sql("SELECT inbox_id FROM email_messages WHERE message_id = :m", m=mid) for mid in ids}
    assert got == {ids[0]: old, ids[1]: new, ids[2]: old, ids[3]: old, ids[4]: old}
    assert sql("SELECT from_email FROM email_messages WHERE message_id = :m", m=ids[1]) == new_email
