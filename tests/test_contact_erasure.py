"""GDPR erasure (DELETE /prospects/{id} -> contact_service.delete_contacts) scrubs copies of the
contact's personal data from rows that do not reference it by foreign key (D-12).

Own workspace. Rows the app would write in other flows (import errors, webhook outbox rows,
notifications) are inserted directly so each path is checked on its own.
"""
import json
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    t = register_tenant(client, "erase")
    t["user_id"] = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    return t


@pytest.fixture
def contact(client, ws):
    email = f"erase.me.{uuid.uuid4().hex[:6]}@client{RUN}.example.com"
    r = client.post("/contacts", headers=ws["headers"], json={"email": email, "first_name": "Erin",
                                                               "last_name": "Rasure"})
    assert r.status_code == 201, r.text
    return {"prospect_id": r.json()["prospect_id"], "email": email}


def _erase(client, ws, contact):
    r = client.delete(f"/prospects/{contact['prospect_id']}", headers=ws["headers"])
    assert r.status_code == 200, r.text
    assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=contact["prospect_id"]) == 0


def test_import_job_errors_lose_the_erased_rows(client, ws, contact):
    from app.models.crm import ImportJob
    other = f"someone.else@client{RUN}.example.com"
    with SessionLocal() as db:
        job = ImportJob(tenant_id=ws["tenant_id"], created_by=ws["user_id"], file_name="contacts.csv",
                        status="COMPLETED", error_count=3, errors=[
                            {"row": 2, "reason": "Invalid phone", "values": {"Email": contact["email"].upper(),
                                                                            "Name": "Erin Rasure"}},
                            {"row": 3, "reason": "Missing name", "values": {"Email": other}},
                            {"row": 4, "reason": f"Duplicate of {contact['email']}", "values": {"Email": ""}},
                        ])
        db.add(job)
        db.commit()
        import_id = job.import_id
    _erase(client, ws, contact)
    with SessionLocal() as db:
        job = db.get(ImportJob, import_id)
        assert [e["row"] for e in job.errors] == [3]
        assert job.error_count == 1
        assert contact["email"] not in json.dumps(job.errors).lower()


def test_webhook_payloads_are_redacted(client, ws, contact):
    from app.models.webhook import WebhookDelivery, WebhookEndpoint
    other_body = json.dumps({"id": "e2", "type": "contact.created", "data": {"contact_id": "someone-else",
                                                                             "email": "x@example.com"}})
    with SessionLocal() as db:
        ep = WebhookEndpoint(tenant_id=ws["tenant_id"], url="https://hooks.example.com/in", secret="s3cret",
                             event_types=["contact.created", "contact.deleted"])
        db.add(ep)
        db.flush()
        mine_by_id = WebhookDelivery(tenant_id=ws["tenant_id"], endpoint_id=ep.endpoint_id, event_id="e1",
                                     event_type="contact.created", status="SUCCEEDED", attempts=1,
                                     payload=json.dumps({"id": "e1", "type": "contact.created", "data": {
                                         "contact_id": contact["prospect_id"], "email": contact["email"],
                                         "first_name": "Erin", "last_name": "Rasure"}}))
        mine_by_email = WebhookDelivery(tenant_id=ws["tenant_id"], endpoint_id=ep.endpoint_id, event_id="e3",
                                        event_type="email.bounced", status="PENDING", attempts=0,
                                        payload=json.dumps({"id": "e3", "type": "email.bounced", "data": {
                                            "email": contact["email"], "reason": "mailbox full"}}))
        other = WebhookDelivery(tenant_id=ws["tenant_id"], endpoint_id=ep.endpoint_id, event_id="e2",
                                event_type="contact.created", status="SUCCEEDED", attempts=1, payload=other_body)
        db.add_all([mine_by_id, mine_by_email, other])
        db.commit()
        ids = mine_by_id.delivery_id, mine_by_email.delivery_id, other.delivery_id
    _erase(client, ws, contact)
    with SessionLocal() as db:
        by_id, by_email, untouched = (db.get(WebhookDelivery, i) for i in ids)
        assert json.loads(by_id.payload) == {"id": "e1", "type": "contact.created",
                                             "data": {"contact_id": contact["prospect_id"], "redacted": True}}
        assert contact["email"] not in by_email.payload and "Erin" not in by_id.payload
        assert json.loads(by_email.payload)["data"] == {"redacted": True}
        assert by_email.status == "PENDING"  # still delivered, without the personal data
        assert untouched.payload == other_body


def test_notifications_about_the_contact_are_removed(client, ws, contact):
    from app.models.sales_extra import Notification
    with SessionLocal() as db:
        rows = [
            Notification(tenant_id=ws["tenant_id"], user_id=ws["user_id"], kind="MENTION",
                         title="You were mentioned on Erin Rasure", body="call her back",
                         link=f"/app/contacts/{contact['prospect_id']}"),
            Notification(tenant_id=ws["tenant_id"], user_id=ws["user_id"], kind="ASSIGNED",
                         title=f"New lead: {contact['email']}", body="Assigned to you.", link="/app/leads/x"),
            Notification(tenant_id=ws["tenant_id"], user_id=ws["user_id"], kind="TASK",
                         title="Unrelated", body="nothing personal", link="/app/deals/abc"),
        ]
        db.add_all(rows)
        db.commit()
        ids = [n.notification_id for n in rows]
    _erase(client, ws, contact)
    left = [i for i in ids if sql("SELECT COUNT(*) FROM notifications WHERE notification_id = :i", i=i)]
    assert left == [ids[2]]


def test_lead_notification_removed_and_suppression_kept(client, ws, contact):
    """A notification linking to the contact's lead goes; the suppression entry stays (documented exception)."""
    from app.models.sales import Lead
    from app.models.sales_extra import Notification
    from app.services.suppression import suppress
    with SessionLocal() as db:
        lead = Lead(tenant_id=ws["tenant_id"], prospect_id=contact["prospect_id"], owner_id=ws["user_id"])
        db.add(lead)
        db.flush()
        db.add(Notification(tenant_id=ws["tenant_id"], user_id=ws["user_id"], kind="ASSIGNED",
                            title="New lead: Erin Rasure", link=f"/app/leads/{lead.lead_id}"))
        suppress(db, ws["tenant_id"], contact["email"], "asked to stop", kind="UNSUBSCRIBE", source="test")
        db.commit()
        lead_link = f"/app/leads/{lead.lead_id}"
    _erase(client, ws, contact)
    assert sql("SELECT COUNT(*) FROM notifications WHERE link = :l", l=lead_link) == 0
    assert sql("SELECT COUNT(*) FROM leads WHERE prospect_id = :p", p=contact["prospect_id"]) == 0
    assert sql("SELECT COUNT(*) FROM global_unsubscribes WHERE email = :e", e=contact["email"]) == 1


def test_fk_linked_copies_are_deleted(client, ws, contact):
    """Messages, conversations, activities, list memberships and history of the contact go too."""
    r = client.post(f"/contacts/{contact['prospect_id']}/activities", headers=ws["headers"],
                    json={"activity_type": "NOTE", "body": "Erin prefers mornings"})
    _erase(client, ws, contact)
    for table in ("email_messages", "conversations", "contact_activities", "prospect_list_members",
                  "campaign_prospects", "crm_tasks"):
        assert sql(f"SELECT COUNT(*) FROM {table} WHERE prospect_id = :p", p=contact["prospect_id"]) == 0, table
    assert sql("SELECT COUNT(*) FROM property_changes WHERE object_id = :p", p=contact["prospect_id"]) == 0
    if r.status_code in (200, 201):
        assert sql("SELECT COUNT(*) FROM contact_activities WHERE body LIKE '%prefers mornings%'"
                   " AND tenant_id = :t", t=ws["tenant_id"]) == 0
