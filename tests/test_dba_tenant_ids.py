"""
D3 / D5 / C-22 functional (MySQL): child rows carry their parent's tenant_id, the database refuses links across
tenants and status values outside their vocabulary.

* Rows written through the API (contacts, campaigns, steps, templates, enrolments, lists, rules, proposals) and
  through plain ORM inserts that never set tenant_id (messages, events, versions, attachments, compliance checks,
  state events, group members, personas) get the tenant of their parent from app/models/tenant_fill.py.
* Per table, a row of tenant A is never returned for tenant B through the main read paths.
* Composite foreign keys, the one-primary-company key and the status CHECK constraints refuse bad rows.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.clock import utcnow
from app.core.database import SessionLocal
from tests.conftest import RUN, register_tenant, sql

CHILD_TABLES = (
    "email_messages", "email_events", "campaign_prospects", "campaign_state_events", "email_sequences",
    "email_templates", "automation_rules", "email_template_versions", "email_attachments", "compliance_checks",
    "prospect_list_members", "proposal_lines", "campaign_group_members", "prospect_personas",
)


def _ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def _orm_children(t: dict, d: dict) -> None:
    """Rows the scheduler, the IMAP sync or the AI services write: plain ORM inserts without tenant_id."""
    from app.models.campaign import CampaignStateEvent
    from app.models.campaign_group import CampaignGroup, CampaignGroupMember
    from app.models.compliance_check import ComplianceCheck
    from app.models.email_attachment import EmailAttachment
    from app.models.email_message import EmailEvent, EmailMessage
    from app.models.email_template import EmailTemplateVersion
    from app.models.prospect_persona import ProspectPersona

    with SessionLocal() as db:
        msg = EmailMessage(campaign_id=d["campaign"], prospect_id=d["contact"], template_id=d["template"],
                           direction="OUTBOUND", status="SENT", subject="Hi", body_text="Hello",
                           to_email=d["email"], sent_at=utcnow())
        db.add(msg)
        db.add(EmailEvent(message=msg, event_type=EmailEvent.EVENT_OPEN, event_time=utcnow()))  # same flush
        db.add(EmailTemplateVersion(template_id=d["template"], subject="v1", body="b", version_number=1,
                                    created_by=d["owner"]))
        db.add(EmailAttachment(template_id=d["template"], filename="a.pdf", size_bytes=1, storage_path="x/a.pdf"))
        db.add(ComplianceCheck(template_id=d["template"], status="passed"))
        db.add(CampaignStateEvent(campaign_id=d["campaign"], from_state="DRAFT", to_state="ACTIVE"))
        group = CampaignGroup(tenant_id=t["tenant_id"], group_name=f"G {RUN}", owner_id=d["owner"])
        db.add(group)
        db.flush()
        db.add(CampaignGroupMember(group_id=group.group_id, campaign_id=d["campaign"]))
        db.add(ProspectPersona(prospect_id=d["contact"], persona_type="OPERATIONS"))
        db.commit()
        d["message"] = msg.message_id


def _workspace(client, label: str) -> dict:
    t = register_tenant(client, label)
    h = t["headers"]
    d: dict = {"t": t, "owner": sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])}
    d["email"] = f"c.{label}{RUN}@dba{RUN}.example.com"
    d["contact"] = _ok(client.post("/contacts", headers=h, json={"email": d["email"], "first_name": "Cy"}), 201)[
        "prospect_id"]
    d["campaign"] = _ok(client.post("/campaigns", headers=h, json={"campaign_name": f"D3 {label} {RUN}"}),
                        201)["campaign_id"]
    _ok(client.post(f"/campaigns/{d['campaign']}/sequences", headers=h, json={"step_number": 1, "wait_days": 0}),
        201)
    d["template"] = _ok(client.post("/templates", headers=h, json={
        "campaign_id": d["campaign"], "subject": "Hi {{first_name}}", "body": "Body"}), 201)["template_id"]
    _ok(client.post(f"/campaigns/{d['campaign']}/prospects", headers=h, json={"prospect_ids": [d["contact"]]}))
    d["list"] = _ok(client.post("/lists", headers=h, json={"list_name": f"D3 {label} {RUN}"}), 201)["list_id"]
    _ok(client.post(f"/lists/{d['list']}/members", headers=h, json={"prospect_ids": [d["contact"]]}))
    d["rule"] = _ok(client.post("/automation-rules/", headers=h, json={
        "campaign_id": d["campaign"], "name": f"rule {label}", "trigger_type": "EMAIL_OPENED",
        "action_type": "CHANGE_CAMPAIGN_STEP", "action_config": {"step_id": 1}}), 201)["rule_id"]
    stage = client.get("/sales/stages", headers=h).json()[0]
    opp = _ok(client.post("/opportunities", headers=h, json={"name": f"Deal {label} {RUN}",
                                                            "stage_id": stage["stage_id"]}), 201)
    d["proposal"] = _ok(client.post("/proposals", headers=h, json={
        "opportunity_id": opp["opportunity_id"], "title": f"Quote {label} {RUN}"}), 201)["proposal_id"]
    _ok(client.put(f"/proposals/{d['proposal']}/lines", headers=h, json={"lines": [
        {"description": "Seats", "quantity": 2, "unit_price": 10, "discount_pct": 0}]}))
    _orm_children(t, d)
    return d


@pytest.fixture(scope="module")
def ws(client):
    return {"a": _workspace(client, "dbaa"), "b": _workspace(client, "dbab")}


def _tenants_of(table: str, d: dict) -> set:
    """tenant_id of the table's rows that hang off this workspace's parents."""
    where = {
        "email_messages": "prospect_id = :contact", "email_events": "message_id = :message",
        "campaign_prospects": "campaign_id = :campaign", "campaign_state_events": "campaign_id = :campaign",
        "email_sequences": "campaign_id = :campaign", "email_templates": "campaign_id = :campaign",
        "automation_rules": "campaign_id = :campaign", "email_template_versions": "template_id = :template",
        "email_attachments": "template_id = :template", "compliance_checks": "template_id = :template",
        "prospect_list_members": "list_id = :list", "proposal_lines": "proposal_id = :proposal",
        "campaign_group_members": "campaign_id = :campaign", "prospect_personas": "prospect_id = :contact",
    }[table]
    params = {k: d[k] for k in ("contact", "message", "campaign", "template", "list", "proposal")}
    from sqlalchemy import text

    from app.core.database import engine
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(f"SELECT tenant_id FROM {table} WHERE {where}"), params)}


@pytest.mark.parametrize("table", CHILD_TABLES)
def test_dba_child_rows_get_the_parents_tenant(ws, table):
    for d in ws.values():
        assert _tenants_of(table, d) == {d["t"]["tenant_id"]}, table


def test_dba_child_tenant_columns_are_not_null():
    for table in CHILD_TABLES:
        assert sql("SELECT is_nullable FROM information_schema.columns WHERE table_schema = DATABASE() "
                   "AND table_name = :t AND column_name = 'tenant_id'", t=table) == "NO", table


def test_dba_reads_never_cross_tenants(client, ws):
    a, hb = ws["a"], ws["b"]["t"]["headers"]
    for path in (f"/campaigns/{a['campaign']}/prospects", f"/campaigns/{a['campaign']}/sequences",
                 f"/campaigns/{a['campaign']}/templates", f"/templates/{a['template']}",
                 f"/templates/{a['template']}/versions", f"/templates/{a['template']}/attachments",
                 f"/contacts/{a['contact']}/emails", f"/contacts/{a['contact']}/timeline", f"/lists/{a['list']}",
                 f"/proposals/{a['proposal']}/lines"):
        assert client.get(path, headers=hb).status_code == 404, path
        assert client.get(path, headers=a["t"]["headers"]).status_code == 200, path
    rules = client.get("/automation-rules/", headers=hb).json()
    assert a["rule"] not in str(rules) and ws["b"]["rule"] in str(rules)
    assert a["message"] not in client.get(f"/contacts/{ws['b']['contact']}/emails", headers=hb).text


def test_dba_composite_keys_refuse_cross_tenant_links(ws):
    a, b = ws["a"], ws["b"]
    with pytest.raises(IntegrityError):  # B's contact in A's list
        sql("INSERT INTO prospect_list_members (id, list_id, prospect_id, tenant_id) VALUES (:i, :l, :p, :t)",
            i=str(uuid.uuid4()), l=a["list"], p=b["contact"], t=a["t"]["tenant_id"])
    with pytest.raises(IntegrityError):  # A's list under B's tenant
        sql("INSERT INTO prospect_list_members (id, list_id, prospect_id, tenant_id) VALUES (:i, :l, :p, :t)",
            i=str(uuid.uuid4()), l=a["list"], p=b["contact"], t=b["t"]["tenant_id"])
    account = str(uuid.uuid4())
    sql("INSERT INTO accounts (account_id, tenant_id, name) VALUES (:a, :t, :n)", a=account,
        t=a["t"]["tenant_id"], n=f"Acme D3 {RUN}")
    with pytest.raises(IntegrityError):  # B's contact linked to A's company
        sql("INSERT INTO contact_companies (link_id, tenant_id, prospect_id, account_id, is_primary) "
            "VALUES (:l, :t, :p, :a, 0)", l=str(uuid.uuid4()), t=b["t"]["tenant_id"], p=b["contact"], a=account)


def test_dba_one_primary_company_per_contact(client, ws):
    a = ws["a"]
    h, tenant = a["t"]["headers"], a["t"]["tenant_id"]
    first, second = (_ok(client.post("/accounts", headers=h, json={"name": f"{n} D3 {RUN}"}), 201)["account_id"]
                     for n in ("First", "Second"))
    _ok(client.post(f"/contacts/{a['contact']}/companies", headers=h, json={"account_id": first}), 201)
    with pytest.raises(IntegrityError):
        sql("INSERT INTO contact_companies (link_id, tenant_id, prospect_id, account_id, is_primary) "
            "VALUES (:l, :t, :p, :a, 1)", l=str(uuid.uuid4()), t=tenant, p=a["contact"], a=second)
    # Switching the primary through the API still works (the listener writes the demotion first)
    _ok(client.post(f"/contacts/{a['contact']}/companies", headers=h, json={"account_id": second}), 201)
    _ok(client.patch(f"/contacts/{a['contact']}/companies/{second}", headers=h, json={"is_primary": True}))
    assert sql("SELECT account_id FROM contact_companies WHERE prospect_id = :p AND is_primary",
               p=a["contact"]) == second
    _ok(client.delete(f"/contacts/{a['contact']}/companies/{second}", headers=h, params={"new_primary_id": first}))
    assert sql("SELECT account_id FROM contact_companies WHERE prospect_id = :p AND is_primary",
               p=a["contact"]) == first


@pytest.mark.parametrize("table,column,key", [
    ("campaigns", "status", "campaign_id = :i"), ("campaign_prospects", "status", "campaign_id = :i"),
    ("email_messages", "status", "message_id = :m"), ("email_messages", "final_status", "message_id = :m"),
    ("proposals", "status", "proposal_id = :p"), ("proposals", "approval_status", "proposal_id = :p"),
])
def test_dba_status_checks_refuse_unknown_values(ws, table, column, key):
    a = ws["a"]
    with pytest.raises(OperationalError, match="3819"):
        sql(f"UPDATE {table} SET {column} = 'NOT_A_STATUS' WHERE {key}", i=a["campaign"], m=a["message"],
            p=a["proposal"])


def test_dba_status_checks_accept_every_vocabulary_value_and_null():
    from app.domain.status import STATUS_CHECKS
    for (table, column), values in STATUS_CHECKS.items():
        clause = sql("SELECT check_clause FROM information_schema.check_constraints "
                     "WHERE constraint_schema = DATABASE() AND constraint_name = :n", n=f"ck_{table}_{column}")
        assert clause, (table, column)
        clause = clause.replace("\\'", "'")
        for v in values:
            assert f"'{v}'" in clause, (table, column, v)
    assert sql("SELECT COUNT(*) FROM email_messages WHERE final_status IS NULL") >= 1  # NULL passes
