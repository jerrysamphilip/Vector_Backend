"""
A1a functional tests (MySQL): companies on the layered pattern plus this round's company features.

  * company custom properties (C-16): definitions under /accounts/fields, kept apart from contact properties,
    values validated on create / update, filtered, shown as columns, exported, in the history
  * field propagation (C-20): name / industry / size changes reach the contacts with history rows
  * backfill preview / runs / undo (C-14), and the old POST /accounts/backfill
  * company export (C-07): permission, formula guard, refused over the row limit
  * Active / Archived list, tenant isolation and the owner scope
"""
import csv
import io
import json
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def tenant(client):
    return register_tenant(client, "aoa")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "aob")


def _u():
    return uuid.uuid4().hex[:6]


def _agent(client, tenant_id, manage=True):
    from app.core.security import hash_password
    from app.models.user import User

    email = f"agent-{RUN}-{_u()}@aoa{RUN}.example.com"
    password = f"agent-pass-{RUN}"
    with SessionLocal() as db:
        db.add(User(tenant_id=tenant_id, first_name="Ag", last_name="Ent", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                    email_verified=True, sales_level=3,
                    custom_permissions=["manage_prospects"] if manage else []))
        db.commit()
    return auth(login(client, email, password))


def _company(client, h, name, **kw):
    r = client.post("/accounts", headers=h, json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _contact(client, h, email, **kw):
    r = client.post("/contacts", headers=h, json={"email": email, "first_name": "Pat", **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


# ── C-16 company custom properties ────────────────────────────────

def test_company_properties_are_validated_filtered_exported_and_kept_apart(client, tenant, other):
    h = tenant["headers"]
    r = client.post("/accounts/fields", headers=h, json={"label": f"Tier {RUN}", "field_type": "SELECT",
                                                         "options": ["Gold", "Silver"]})
    assert r.status_code == 201, r.text
    field = r.json()
    assert field["object_type"] == "COMPANY"
    key = field["field_key"]
    # contact properties do not list it, and cannot edit it
    assert key not in {f["field_key"] for f in client.get("/contacts/fields", headers=h).json()}
    assert client.patch(f"/contacts/fields/{field['field_id']}", headers=h, json={"label": "x"}).status_code == 404
    # another tenant cannot see it
    assert key not in {f["field_key"] for f in client.get("/accounts/fields", headers=other["headers"]).json()}

    bad = client.post("/accounts", headers=h, json={"name": f"Bad {RUN}", "custom_fields": {key: "Bronze"}})
    assert bad.status_code == 400
    unknown = client.post("/accounts", headers=h, json={"name": f"Bad {RUN}", "custom_fields": {"nope": 1}})
    assert unknown.status_code == 400
    gold = _company(client, h, f"=Gold Co {RUN}", custom_fields={key: "Gold"})
    silver = _company(client, h, f"Silver Co {RUN}", custom_fields={key: "Silver"})
    assert gold["custom_fields"] == {key: "Gold"}

    flt = json.dumps({"field": f"custom.{key}", "operator": "is", "value": "Gold"})
    listed = client.get("/accounts", headers=h, params={"filters": flt, "page_size": 200}).json()
    ids = {a["account_id"] for a in listed["items"]}
    assert gold["account_id"] in ids and silver["account_id"] not in ids
    assert client.get("/accounts", headers=h, params={"filters": "{bad"}).status_code == 400

    meta = client.get("/accounts/meta", headers=h).json()
    assert f"custom.{key}" in {c["key"] for c in meta["columns"]} and meta["can_export"] is True

    r = client.patch(f"/accounts/{silver['account_id']}", headers=h,
                     json={"custom_fields": {key: "Gold"}, "expected_version": silver["row_version"]})
    assert r.status_code == 200, r.text
    history = r.json()["history"]
    assert any(item["field"] == f"custom.{key}" and item["label"] == f"Tier {RUN}" for item in history)

    export = client.get("/accounts/export", headers=h, params={"filters": flt, "columns": "name,domain"})
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert rows[0] == ["Company", "Domain", f"Tier {RUN}"]
    assert [f"'=Gold Co {RUN}", "", "Gold"] in rows  # formula guard
    assert sql("SELECT COUNT(*) FROM export_logs WHERE tenant_id = :t AND object_type = 'COMPANY' AND row_count = 2",
               t=tenant["tenant_id"]) == 1  # C-07 export log


# ── C-20 propagation ──────────────────────────────────────────────

def test_company_changes_reach_its_contacts_with_history(client, tenant):
    h = tenant["headers"]
    domain = f"prop{_u()}.example.org"
    company = _company(client, h, f"Prop {RUN} {_u()}", domain=domain, industry="SaaS", emp_band="11-50")
    inherits = _contact(client, h, f"a@{domain}", company_name=company["name"], industry="SaaS")
    own = _contact(client, h, f"b@{domain}", industry="Banking")
    assert sql("SELECT account_id FROM prospects WHERE prospect_id = :p", p=inherits) == company["account_id"]

    r = client.patch(f"/accounts/{company['account_id']}", headers=h, json={
        "name": f"Renamed {RUN} {_u()}", "industry": "Fintech", "emp_band": "51-200",
        "expected_version": company["row_version"]})
    assert r.status_code == 200, r.text
    new_name = r.json()["name"]
    assert sql("SELECT company_name FROM prospects WHERE prospect_id = :p", p=inherits) == new_name
    assert sql("SELECT industry FROM prospects WHERE prospect_id = :p", p=inherits) == "Fintech"
    assert sql("SELECT industry FROM prospects WHERE prospect_id = :p", p=own) == "Banking"  # kept
    assert sql("SELECT company_name FROM prospects WHERE prospect_id = :p", p=own) == new_name
    assert sql("SELECT COUNT(*) FROM property_changes WHERE object_id = :p AND source = 'COMPANY' "
               "AND field IN ('company_name', 'industry', 'emp_band')", p=inherits) == 3
    history = client.get(f"/contacts/{inherits}/history", headers=h)
    assert history.status_code == 200


# ── C-14 backfill ─────────────────────────────────────────────────

def _unlinked(client, h, email, company_name=None, keep=None):
    """A contact without a company, as an old import left them (the company creation made a new one: dropped)."""
    pid = _contact(client, h, email, company_name=company_name)
    made = sql("SELECT account_id FROM prospects WHERE prospect_id = :p", p=pid)
    sql("UPDATE prospects SET account_id = NULL, company_name = :c WHERE prospect_id = :p", p=pid, c=company_name)
    sql("DELETE FROM contact_companies WHERE prospect_id = :p", p=pid)
    if made and made != keep:
        sql("DELETE FROM property_changes WHERE object_id = :a", a=made)
        sql("DELETE FROM accounts WHERE account_id = :a", a=made)
    return pid


def _account_of(pid):
    return sql("SELECT account_id FROM prospects WHERE prospect_id = :p", p=pid)


def _check_run(run, existing, p_existing, p_new, p_name, tag):
    assert run["status"] == "COMPLETED" and run["stats"]["contacts_linked"] >= 3
    assert _account_of(p_existing) == existing["account_id"]
    assert sql("SELECT company_name FROM prospects WHERE prospect_id = :p", p=p_existing) == existing["name"]
    assert sql("SELECT name FROM accounts WHERE account_id = :a", a=_account_of(p_new)) == f"NewCo {tag}"
    assert sql("SELECT domain FROM accounts WHERE account_id = :a", a=_account_of(p_name)) is None


def _check_undone(existing, p_existing, new_account):
    assert _account_of(p_existing) is None
    assert sql("SELECT company_name FROM prospects WHERE prospect_id = :p", p=p_existing) is None
    assert sql("SELECT COUNT(*) FROM accounts WHERE account_id = :a", a=new_account) == 0
    assert sql("SELECT COUNT(*) FROM accounts WHERE account_id = :a", a=existing["account_id"]) == 1


def test_backfill_preview_run_history_and_undo(client, tenant, other):
    h = tenant["headers"]
    tag = _u()
    existing = _company(client, h, f"Existing {tag}", domain=f"existing{tag}.example.org")
    p_existing = _unlinked(client, h, f"x@existing{tag}.example.org", keep=existing["account_id"])
    p_new = _unlinked(client, h, f"y@newco{tag}.example.org", company_name=f"NewCo {tag}")
    p_name = _unlinked(client, h, f"z{tag}@gmail.com", company_name=f"Byname {tag}")

    preview = client.post("/accounts/backfill/previews", headers=h)
    assert preview.status_code == 200, preview.text
    stats = preview.json()["stats"]
    assert stats["contacts_to_link"] >= 3
    assert stats["companies_to_create"] >= 2
    assert _account_of(p_new) is None  # dry run

    run = client.post("/accounts/backfill/runs", headers=h)
    assert run.status_code == 201, run.text
    run = run.json()
    _check_run(run, existing, p_existing, p_new, p_name, tag)
    new_account = _account_of(p_new)

    runs = client.get("/accounts/backfill/runs", headers=h).json()["items"]
    assert runs[0]["run_id"] == run["run_id"]
    assert runs[0]["can_undo"] is True
    assert client.get("/accounts/backfill/runs", headers=other["headers"]).json()["items"] == []
    undo_path = f"/accounts/backfill/runs/{run['run_id']}/undo"
    assert client.post(undo_path, headers=other["headers"]).status_code == 404

    undo = client.post(undo_path, headers=h)
    assert undo.status_code == 200, undo.text
    assert undo.json()["status"] == "UNDONE"
    _check_undone(existing, p_existing, new_account)
    assert client.post(undo_path, headers=h).status_code == 409

    legacy = client.post("/accounts/backfill", headers=h)
    assert legacy.status_code == 200
    assert legacy.json()["status"] == "ok"
    assert legacy.json()["linked_contacts"] >= 3


def test_backfill_needs_manage_permission(client, tenant):
    agent = _agent(client, tenant["tenant_id"], manage=False)
    assert client.post("/accounts/backfill/previews", headers=agent).status_code == 403
    assert client.post("/accounts/backfill/runs", headers=agent).status_code == 403
    assert client.post("/accounts/backfill", headers=agent).status_code == 403


# ── C-07 export, Active / Archived, isolation ─────────────────────

def test_export_rights_and_row_limit(client, tenant, monkeypatch):
    agent = _agent(client, tenant["tenant_id"])
    assert client.get("/accounts/export", headers=agent).status_code == 403
    from app.services import account_service
    monkeypatch.setattr(account_service, "EXPORT_ROW_LIMIT", 1)
    _company(client, tenant["headers"], f"Limit A {RUN} {_u()}")
    _company(client, tenant["headers"], f"Limit B {RUN} {_u()}")
    r = client.get("/accounts/export", headers=tenant["headers"], params={"format": "xlsx"})
    assert r.status_code == 400 and "at most 1" in r.json()["detail"]


def test_archived_filter_and_isolation(client, tenant, other):
    h = tenant["headers"]
    company = _company(client, h, f"Archive {RUN} {_u()}")
    assert client.delete(f"/accounts/{company['account_id']}", headers=h).status_code == 200
    active = client.get("/accounts", headers=h, params={"q": company["name"]}).json()
    assert active["total"] == 0
    archived = client.get("/accounts", headers=h, params={"q": company["name"], "status": "archived"}).json()
    assert [a["account_id"] for a in archived["items"]] == [company["account_id"]]
    assert archived["items"][0]["purge_at"]
    agent = _agent(client, tenant["tenant_id"], manage=False)
    assert client.get("/accounts", headers=agent, params={"status": "archived"}).status_code == 403
    # another tenant sees nothing of it
    assert client.get(f"/accounts/{company['account_id']}", headers=other["headers"]).status_code == 404
    assert client.post(f"/accounts/{company['account_id']}/restore", headers=other["headers"]).status_code == 404
    assert client.post(f"/accounts/{company['account_id']}/restore", headers=h).status_code == 200


def test_agent_sees_only_companies_of_their_contacts(client, tenant):
    h = tenant["headers"]
    hidden = _company(client, h, f"Hidden {RUN} {_u()}")
    agent = _agent(client, tenant["tenant_id"])
    assert client.get(f"/accounts/{hidden['account_id']}", headers=agent).status_code == 404
    names = {a["name"] for a in client.get("/accounts", headers=agent, params={"page_size": 200}).json()["items"]}
    assert hidden["name"] not in names
