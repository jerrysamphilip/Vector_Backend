"""Tenant B must never read or change tenant A's data (SEC-03 .. SEC-09)."""

from tests.conftest import RUN, sql


def test_conversation_isolated(client, tenant_a, tenant_b, tenant_a_data):
    conv = tenant_a_data["conversation_id"]
    assert client.get(f"/conversations/{conv}", headers=tenant_b["headers"]).status_code == 404
    r = client.post(f"/conversations/{conv}/reply", headers=tenant_b["headers"],
                    json={"body_text": "hijack"})
    assert r.status_code == 404
    assert client.get(f"/conversations/{conv}", headers=tenant_a["headers"]).status_code == 200
    listing = client.get("/conversations", headers=tenant_b["headers"])
    assert listing.status_code == 200
    assert conv not in listing.text


def test_template_isolated(client, tenant_a, tenant_b, tenant_a_data):
    tpl = tenant_a_data["template_id"]
    assert client.get(f"/templates/{tpl}", headers=tenant_b["headers"]).status_code == 404
    assert client.delete(f"/templates/{tpl}", headers=tenant_b["headers"]).status_code == 404
    assert client.get(f"/templates/{tpl}", headers=tenant_a["headers"]).status_code == 200


def test_campaign_and_step_emails_isolated(client, tenant_a, tenant_b, tenant_a_data):
    camp = tenant_a_data["campaign_id"]
    assert client.get(f"/campaigns/{camp}", headers=tenant_b["headers"]).status_code == 404
    assert client.get(f"/campaigns/{camp}/sequences/1/emails", headers=tenant_b["headers"]).status_code == 404
    assert client.get(f"/campaigns/{camp}", headers=tenant_a["headers"]).status_code == 200


def test_company_profiles_isolated(client, tenant_a, tenant_b):
    name = f"A profile {RUN}"
    r = client.post("/api/company-profiles", headers=tenant_a["headers"],
                    json={"profile_name": name, "company_name": f"A Co {RUN}", "description": "x"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    pid = body.get("profile_id") or body.get("id")
    assert pid
    assert sql("SELECT tenant_id FROM company_profiles WHERE profile_name = :n", n=name) == tenant_a["tenant_id"]

    r = client.get("/api/company-profiles", headers=tenant_b["headers"])
    assert r.status_code == 200
    assert all(p.get("profile_name") != name for p in r.json())
    assert client.get(f"/api/company-profiles/{pid}", headers=tenant_b["headers"]).status_code == 404
    assert client.get(f"/api/company-profiles/{pid}", headers=tenant_a["headers"]).status_code == 200


def test_inbox_isolated(client, tenant_a, tenant_b, tenant_a_data):
    inbox = tenant_a_data["inbox_id"]
    hb = tenant_b["headers"]
    assert client.get(f"/inboxes/{inbox}", headers=hb).status_code == 404
    assert client.put(f"/inboxes/{inbox}", headers=hb, json={"smtp_username": "stolen"}).status_code == 404
    assert client.post(f"/inboxes/{inbox}/test-smtp", headers=hb).status_code == 404
    assert client.get(f"/inboxes/{inbox}/test-imap", headers=hb).status_code == 404
    assert all(i["inbox_id"] != inbox for i in client.get("/inboxes", headers=hb).json())
    assert sql("SELECT smtp_username FROM sending_inboxes WHERE inbox_id = :i", i=inbox) == "sender"


def test_deliverability_sent_log_isolated(client, tenant_a, tenant_b, tenant_a_data):
    path = f"/deliverability/domains/{tenant_a_data['domain']}/sent-log"
    own = client.get(path, headers=tenant_a["headers"])
    assert own.status_code == 200
    assert any(e["message_id"] == tenant_a_data["message_id"] for e in own.json())

    other = client.get(path, headers=tenant_b["headers"])
    assert other.status_code == 404 or (other.status_code == 200 and other.json() == []), other.text


def test_tenant_admin_cannot_stop_global_scheduler(client, tenant_b):
    assert client.post("/scheduler/stop", headers=tenant_b["headers"]).status_code == 403


def test_tenant_cannot_create_global_blueprint(client, tenant_b):
    r = client.post("/persona-blueprints", headers=tenant_b["headers"], json={"persona_type": f"X{RUN}"})
    assert r.status_code == 403, r.text
    assert client.get("/persona-blueprints", headers=tenant_b["headers"]).status_code == 200
