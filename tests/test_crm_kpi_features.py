"""BRD v2.0 additions: contacts at several companies with labels (CM-014), @mentions in
notes (BR-CM-16 / CM-033), and the SQL-to-win and forecast-accuracy KPIs (section 8)."""

import uuid
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


def _member(client, tenant_id: str, label: str, role: str = "AGENT", manager_id=None, sales_level=None) -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@kpi{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True, manager_id=manager_id, sales_level=sales_level)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "email": email, "headers": auth(login(client, email, password))}


def _contact(client, h, email, **kw):
    r = client.post("/contacts", headers=h, json={"email": email, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _company(client, h, name, domain=None):
    r = client.post("/accounts", headers=h, json={"name": name, "domain": domain})
    assert r.status_code == 201, r.text
    return r.json()["account_id"]


# ── Feature 1: several companies per contact ──────────────────

@pytest.fixture(scope="module")
def tenant_x(client):
    return register_tenant(client, "kpix")


def test_contact_companies_add_label_primary_remove(client, tenant_x):
    h = tenant_x["headers"]
    acme = _company(client, h, f"Acme {RUN}", f"acme{RUN}.example.com")
    globex = _company(client, h, f"Globex {RUN}", f"globex{RUN}.example.com")
    initech = _company(client, h, f"Initech {RUN}")
    contact = _contact(client, h, f"ann.{RUN}@acme{RUN}.example.com", first_name="Ann", account_id=acme)
    pid = contact["prospect_id"]
    # The primary company (prospects.account_id) is listed even before any link was edited
    assert [(c["account_id"], c["is_primary"]) for c in contact["companies"]] == [(acme, True)]

    r = client.post(f"/contacts/{pid}/companies", headers=h, json={"account_id": globex, "label": "Billing"})
    assert r.status_code == 201, r.text
    items = {c["account_id"]: c for c in r.json()["items"]}
    assert items[globex]["label"] == "Billing" and not items[globex]["is_primary"] and items[acme]["is_primary"]
    assert client.post(f"/contacts/{pid}/companies", headers=h, json={"account_id": globex}).status_code == 409
    # Unbounded input is refused
    assert client.post(f"/contacts/{pid}/companies", headers=h,
                       json={"account_id": initech, "label": "x" * 101}).status_code == 422

    # Both companies list the contact, with the association label; the list filter matches either
    acc = client.get(f"/accounts/{globex}", headers=h).json()
    row = next(c for c in acc["contacts"] if c["prospect_id"] == pid)
    assert row["association_label"] == "Billing" and row["is_primary_company"] is False
    assert any(c["prospect_id"] == pid for c in client.get(f"/accounts/{acme}", headers=h).json()["contacts"])
    listed = client.get("/contacts", headers=h, params={"account_id": globex}).json()
    assert pid in [c["prospect_id"] for c in listed["items"]]
    counts = {a["account_id"]: a["contact_count"] for a in client.get("/accounts", headers=h,
                                                                       params={"q": RUN, "page_size": 50}).json()["items"]}
    assert counts[globex] == 1 and counts[acme] == 1

    # Make Globex primary: prospects.account_id and company_name follow, Acme stays associated
    r = client.patch(f"/contacts/{pid}/companies/{globex}", headers=h, json={"is_primary": True, "label": "Decision maker"})
    assert r.status_code == 200, r.text
    items = {c["account_id"]: c for c in r.json()["items"]}
    assert items[globex]["is_primary"] and items[globex]["label"] == "Decision maker" and not items[acme]["is_primary"]
    assert sql("SELECT account_id FROM prospects WHERE prospect_id = :p", p=pid) == globex
    assert client.get(f"/contacts/{pid}", headers=h).json()["company_name"] == f"Globex {RUN}"
    # Can't demote the primary directly
    assert client.patch(f"/contacts/{pid}/companies/{globex}", headers=h, json={"is_primary": False}).status_code == 400

    # Removing the primary needs another company chosen as primary
    assert client.delete(f"/contacts/{pid}/companies/{globex}", headers=h).status_code == 400
    r = client.delete(f"/contacts/{pid}/companies/{globex}", headers=h, params={"new_primary_id": acme})
    assert r.status_code == 200, r.text
    assert [(c["account_id"], c["is_primary"]) for c in r.json()["items"]] == [(acme, True)]
    # ...and the only company can't be removed this way
    assert client.delete(f"/contacts/{pid}/companies/{acme}", headers=h).status_code == 400
    assert client.delete(f"/contacts/{pid}/companies/{initech}", headers=h).status_code == 404

    # History: association added / removed, primary changes
    history = client.get(f"/contacts/{pid}/history", headers=h).json()["items"]
    assoc = [c for c in history if c["field"] == "company_association"]
    assert any(c["new_value"] == f"Globex {RUN}" for c in assoc) and any(c["old_value"] == f"Globex {RUN}" for c in assoc)
    assert any(c["field"] == "account_id" and c["new_value"] == f"Globex {RUN}" for c in history)


def test_contact_companies_first_company_becomes_primary_and_field_edit_replaces(client, tenant_x):
    h = tenant_x["headers"]
    one = _company(client, h, f"One {RUN}")
    two = _company(client, h, f"Two {RUN}")
    pid = _contact(client, h, f"nocompany.{RUN}@gmail.com", first_name="Nico")["prospect_id"]
    r = client.post(f"/contacts/{pid}/companies", headers=h, json={"account_id": one})
    assert r.status_code == 201 and r.json()["items"][0]["is_primary"] is True
    assert sql("SELECT account_id FROM prospects WHERE prospect_id = :p", p=pid) == one
    # Changing the Company field replaces the (unlabelled) primary association
    assert client.patch(f"/contacts/{pid}", headers=h, json={"account_id": two}).status_code == 200
    items = client.get(f"/contacts/{pid}/companies", headers=h).json()["items"]
    assert [(c["account_id"], c["is_primary"]) for c in items] == [(two, True)]
    assert pid not in [c["prospect_id"] for c in client.get(f"/accounts/{one}", headers=h).json()["contacts"]]


def test_contact_companies_tenant_and_hierarchy(client, tenant_x, tenant_b):
    h = tenant_x["headers"]
    mine = _company(client, h, f"Mine {RUN}")
    other_tenant_company = _company(client, tenant_b["headers"], f"Theirs {RUN}")
    pid = _contact(client, h, f"iso.{RUN}@gmail.com", first_name="Iso", account_id=mine)["prospect_id"]
    # Another workspace's company can't be linked; another workspace can't see the contact's companies
    assert client.post(f"/contacts/{pid}/companies", headers=h,
                       json={"account_id": other_tenant_company}).status_code == 400
    assert client.get(f"/contacts/{pid}/companies", headers=tenant_b["headers"]).status_code == 404
    assert client.post(f"/contacts/{pid}/companies", headers=tenant_b["headers"],
                       json={"account_id": other_tenant_company}).status_code == 404
    # An agent only sees their own contacts
    agent = _member(client, tenant_x["tenant_id"], "agentcc")
    assert client.get(f"/contacts/{pid}/companies", headers=agent["headers"]).status_code == 404
    acc = client.get(f"/accounts/{mine}", headers=agent["headers"])
    assert acc.status_code == 404 or pid not in [c["prospect_id"] for c in acc.json()["contacts"]]


# ── Feature 2: @mentions in notes ─────────────────────────────

def test_mention_in_note_notifies_colleague(client, tenant_x, tenant_b):
    h = tenant_x["headers"]
    ada = _member(client, tenant_x["tenant_id"], "ada", role="ADMIN")
    author_id = sql("SELECT user_id FROM users WHERE email = :e", e=tenant_x["email"])
    pid = _contact(client, h, f"mention.{RUN}@gmail.com", first_name="Men")["prospect_id"]

    picker = client.get("/contacts/mentionable-users", headers=h, params={"q": "ada"}).json()
    assert ada["user_id"] in [u["user_id"] for u in picker]
    assert all(u["user_id"] != sql("SELECT user_id FROM users WHERE email = :e", e=tenant_b["email"]) for u in picker)

    r = client.post(f"/contacts/{pid}/activities", headers=h, json={
        "activity_type": "NOTE", "body": "@Ada Tester can you call them?",
        "mentioned_user_ids": [ada["user_id"], author_id]})
    assert r.status_code == 201, r.text
    assert {m["user_id"] for m in r.json()["mentions"]} == {ada["user_id"], author_id}
    activity_id = r.json()["activity_id"]

    notes = client.get("/notifications", headers=ada["headers"]).json()["items"]
    mention = next(n for n in notes if n["kind"] == "MENTION")
    assert mention["link"] == f"/app/contacts/{pid}" and "mentioned you" in mention["title"]
    assert "call them" in mention["body"]
    # The author is never notified about their own note
    assert not any(n["kind"] == "MENTION" for n in client.get("/notifications", headers=h).json()["items"])

    timeline = client.get(f"/contacts/{pid}/timeline", headers=h, params={"types": "NOTE"}).json()["items"]
    assert {m["user_id"] for m in timeline[0]["activity"]["mentions"]} == {ada["user_id"], author_id}

    # Editing the note doesn't notify again; a newly added mention does
    bob = _member(client, tenant_x["tenant_id"], "bob", role="ADMIN")
    r = client.patch(f"/contacts/activities/{activity_id}", headers=h,
                     json={"mentioned_user_ids": [ada["user_id"], bob["user_id"]]})
    assert r.status_code == 200, r.text
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'MENTION'", u=ada["user_id"]) == 1
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'MENTION'", u=bob["user_id"]) == 1
    assert sql("SELECT COUNT(*) FROM note_mentions WHERE activity_id = :a", a=activity_id) == 2

    # Deleting the note removes its mentions
    assert client.delete(f"/contacts/activities/{activity_id}", headers=h).status_code == 200
    assert sql("SELECT COUNT(*) FROM note_mentions WHERE activity_id = :a", a=activity_id) == 0


def test_mention_validation(client, tenant_x, tenant_b):
    h = tenant_x["headers"]
    pid = _contact(client, h, f"mention2.{RUN}@gmail.com", first_name="Val")["prospect_id"]
    outsider = sql("SELECT user_id FROM users WHERE email = :e", e=tenant_b["email"])
    r = client.post(f"/contacts/{pid}/activities", headers=h, json={
        "activity_type": "NOTE", "body": "hi", "mentioned_user_ids": [outsider]})
    assert r.status_code == 400
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'MENTION'", u=outsider) == 0
    inactive = _member(client, tenant_x["tenant_id"], "gone")
    sql("UPDATE users SET status = 'INACTIVE' WHERE user_id = :u", u=inactive["user_id"])
    assert client.post(f"/contacts/{pid}/activities", headers=h, json={
        "activity_type": "NOTE", "body": "hi", "mentioned_user_ids": [inactive["user_id"]]}).status_code == 400
    assert client.post(f"/contacts/{pid}/activities", headers=h, json={
        "activity_type": "CALL", "subject": "c", "mentioned_user_ids": [outsider]}).status_code == 400
    assert client.post(f"/contacts/{pid}/activities", headers=h, json={
        "activity_type": "NOTE", "body": "hi", "mentioned_user_ids": ["x" * 37]}).status_code == 422


def test_mention_on_deal_note(client, tenant_x):
    h = tenant_x["headers"]
    cara = _member(client, tenant_x["tenant_id"], "cara", role="ADMIN")
    stage = next(s for s in client.get("/sales/stages", headers=h).json() if s["status"] == "OPEN")
    r = client.post("/opportunities", headers=h, json={"name": f"Deal mention {RUN}", "stage_id": stage["stage_id"]})
    assert r.status_code == 201, r.text
    opp = r.json()["opportunity_id"]
    r = client.post(f"/opportunities/{opp}/activities", headers=h, json={
        "activity_type": "NOTE", "body": "@Cara Tester pricing?", "mentioned_user_ids": [cara["user_id"]]})
    assert r.status_code == 201, r.text
    n = next(n for n in client.get("/notifications", headers=cara["headers"]).json()["items"] if n["kind"] == "MENTION")
    assert n["link"] == f"/app/deals/{opp}"
    timeline = client.get(f"/opportunities/{opp}/timeline", headers=h).json()["items"]
    note = next(i for i in timeline if i["kind"] == "NOTE")
    assert note["activity"]["mentions"][0]["user_id"] == cara["user_id"]


# ── Feature 3: SQL-to-win and forecast accuracy ───────────────

@pytest.fixture(scope="module")
def kpi(client):
    """A workspace with a manager, two reps (one under the manager), SQL leads and deals."""
    t = register_tenant(client, "kpis")
    h = t["headers"]
    manager = _member(client, t["tenant_id"], "mgr", role="MANAGER", sales_level=2)
    rep1 = _member(client, t["tenant_id"], "rep1", manager_id=manager["user_id"], sales_level=3)
    rep2 = _member(client, t["tenant_id"], "rep2", sales_level=3)
    stages = client.get("/sales/stages", headers=h).json()
    won = next(s for s in stages if s["is_won"])
    lost = next(s for s in stages if s.get("is_lost"))
    open_ = next(s for s in stages if s["status"] == "OPEN" and s["probability"] == 50) if any(
        s["status"] == "OPEN" and s["probability"] == 50 for s in stages) else next(s for s in stages if s["status"] == "OPEN")

    def sql_lead(owner, outcome, amount):
        p = _contact(client, h, f"lead.{uuid.uuid4().hex[:6]}.{RUN}@kpi{RUN}.example.com", first_name="L",
                     owner_id=owner)["prospect_id"]
        r = client.post("/leads", headers=h, json={"prospect_id": p, "owner_id": owner, "source": "Test"})
        assert r.status_code in (200, 201), r.text
        lead_id = r.json()["lead_id"]
        sql("UPDATE leads SET stage = 'SQL', qualified_at = NOW() - INTERVAL 5 DAY WHERE lead_id = :l", l=lead_id)
        if outcome:
            stage = {"won": won, "lost": lost, "open": open_}[outcome]
            body = {"name": f"D {uuid.uuid4().hex[:6]}", "prospect_id": p, "owner_id": owner,
                    "stage_id": stage["stage_id"], "amount": str(amount), "close_date": date.today().isoformat()}
            if outcome == "lost":
                body["closed_reason"] = "Price"
            if outcome == "won":
                body["closed_reason"] = "Price"
            r = client.post("/opportunities", headers=h, json=body)
            assert r.status_code == 201, r.text
            sql("UPDATE opportunities SET lead_id = :l WHERE opportunity_id = :o", l=lead_id, o=r.json()["opportunity_id"])
            sql("UPDATE leads SET stage = 'CONVERTED', opportunity_id = :o WHERE lead_id = :l",
                l=lead_id, o=r.json()["opportunity_id"])
        return lead_id

    sql_lead(rep1["user_id"], "won", 1000)
    sql_lead(rep1["user_id"], "lost", 500)
    sql_lead(rep1["user_id"], None, 0)
    sql_lead(rep2["user_id"], "won", 3000)
    sql_lead(rep2["user_id"], "open", 4000)
    # A lead qualified long ago is outside the default period
    old = sql_lead(rep2["user_id"], "won", 50)
    sql("UPDATE leads SET qualified_at = NOW() - INTERVAL 400 DAY WHERE lead_id = :l", l=old)
    return {"tenant": t, "manager": manager, "rep1": rep1, "rep2": rep2, "open_stage": open_}


def test_sql_to_win_rate_and_scoping(client, kpi):
    h = kpi["tenant"]["headers"]
    r = client.get("/sales-reports/sql-to-win", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["sqls"], d["won"], d["lost"], d["open"], d["no_deal"]) == (5, 2, 1, 1, 1)
    assert d["sql_to_win_rate"] == 40.0 and d["closed_win_rate"] == pytest.approx(66.7)
    assert d["won_amount"] == 4000.0
    by_owner = {row["owner_id"]: row for row in d["by_owner"]}
    assert by_owner[kpi["rep1"]["user_id"]]["sql_to_win_rate"] == pytest.approx(33.3)

    # member= narrows to a person's team; a manager sees only their team; a rep only themselves
    r = client.get("/sales-reports/sql-to-win", headers=h, params={"member": kpi["rep2"]["user_id"]})
    assert r.json()["sqls"] == 2 and r.json()["won"] == 1
    assert client.get("/sales-reports/sql-to-win", headers=kpi["manager"]["headers"]).json()["sqls"] == 3
    assert client.get("/sales-reports/sql-to-win", headers=kpi["rep2"]["headers"]).json()["sqls"] == 2
    assert client.get("/sales-reports/sql-to-win", headers=kpi["rep1"]["headers"],
                      params={"member": kpi["rep2"]["user_id"]}).status_code == 404

    # A longer range includes the old SQL; a fiscal year filter works too
    wide = client.get("/sales-reports/sql-to-win", headers=h,
                      params={"date_from": (date.today() - timedelta(days=500)).isoformat()}).json()
    assert wide["sqls"] == 6 and wide["won"] == 3
    assert client.get("/sales-reports/sql-to-win", headers=h, params={"quarter": 2}).status_code == 400
    assert client.get("/sales-reports/sql-to-win", headers=h, params={"fy": 1900}).status_code == 422


def test_sql_to_win_hides_amounts(client, kpi):
    t = kpi["tenant"]
    r = client.put("/sales/settings", headers=t["headers"], json={"amount_hidden_roles": ["AGENT"]})
    assert r.status_code == 200, r.text
    try:
        d = client.get("/sales-reports/sql-to-win", headers=kpi["rep2"]["headers"]).json()
        assert d["won_amount"] is None and d["sqls"] == 2 and d["sql_to_win_rate"] == 50.0
        acc = client.get("/sales-reports/forecast-accuracy", headers=kpi["rep2"]["headers"]).json()
        assert acc["amounts_hidden"] is True
        assert all(p["forecast"] is None and p["actual"] is None for p in acc["periods"])
    finally:
        client.put("/sales/settings", headers=t["headers"], json={"amount_hidden_roles": []})


def test_forecast_snapshots_and_accuracy(client, kpi):
    from app.services import forecast_snapshots as fs

    t = kpi["tenant"]
    h = t["headers"]
    # Only admins can take one on demand
    assert client.post("/sales-reports/forecast-snapshots", headers=kpi["manager"]["headers"], json={}).status_code == 403
    assert client.post("/sales-reports/forecast-snapshots", headers=h, json={"period_type": "YEAR"}).status_code == 422
    r = client.post("/sales-reports/forecast-snapshots", headers=h, json={"period_type": "MONTH"})
    assert r.status_code == 201, r.text
    snap = r.json()["snapshots"][0]
    assert snap["status"] == "taken"
    # won this month 4000 (both won deals close today) + open 4000 at the stage probability
    prob = kpi["open_stage"]["probability"]
    expected = 4000 + 4000 * prob / 100
    total = sql("SELECT SUM(forecast) FROM forecast_snapshots WHERE tenant_id = :t AND period_type = 'MONTH'",
                t=t["tenant_id"])
    assert float(total) == pytest.approx(expected + 50)  # the old won deal also closes today
    # Taking it again keeps the first one unless replace
    again = client.post("/sales-reports/forecast-snapshots", headers=h, json={"period_type": "MONTH"}).json()
    assert again["snapshots"][0]["status"] == "exists"

    acc = client.get("/sales-reports/forecast-accuracy", headers=h, params={"period_type": "MONTH"}).json()
    current = acc["periods"][0]
    assert current["closed"] is False and current["accuracy"] is None and acc["current_period"]["has_snapshot"]
    assert current["actual"] == pytest.approx(4050.0)

    # A finished period: forecast 2000 for rep2, actual won 1500 -> 75 %
    past_start = fs.add_months(fs.period_bounds("MONTH", date.today())[0], -2)
    past_end = fs.add_months(past_start, 1)
    with SessionLocal() as db:
        from app.models.sales_extra import ForecastSnapshot
        db.add(ForecastSnapshot(tenant_id=t["tenant_id"], period_type="MONTH", period_start=past_start,
                                period_end=past_end, owner_id=kpi["rep2"]["user_id"], won=500, weighted=1500,
                                forecast=2000, source="SCHEDULED", taken_at=datetime.combine(past_start, datetime.min.time())))
        db.commit()
    stage = next(s for s in client.get("/sales/stages", headers=h).json() if s["is_won"])
    r = client.post("/opportunities", headers=h, json={
        "name": f"Past won {RUN}", "owner_id": kpi["rep2"]["user_id"], "stage_id": stage["stage_id"],
        "amount": "1500", "close_date": (past_start + timedelta(days=3)).isoformat(), "closed_reason": "Price"})
    assert r.status_code == 201, r.text

    acc = client.get("/sales-reports/forecast-accuracy", headers=h, params={"period_type": "MONTH"}).json()
    past = next(p for p in acc["periods"] if p["period_start"] == past_start.isoformat())
    assert past["closed"] and past["forecast"] == 2000.0 and past["actual"] == 1500.0 and past["accuracy"] == 75.0
    assert acc["average_accuracy"] == 75.0
    # Scoped: rep1 has no share of that forecast; the manager's team (rep1) neither
    rep1_acc = client.get("/sales-reports/forecast-accuracy", headers=kpi["rep1"]["headers"],
                          params={"period_type": "MONTH"}).json()
    past1 = next(p for p in rep1_acc["periods"] if p["period_start"] == past_start.isoformat())
    assert past1["forecast"] == 0.0 and past1["accuracy"] is None
    assert rep1_acc["can_take_snapshot"] is False
    # Other workspaces see nothing of it
    other = register_tenant(client, "kpio")
    assert client.get("/sales-reports/forecast-accuracy", headers=other["headers"],
                      params={"period_type": "MONTH"}).json()["periods"] == []


def test_forecast_snapshot_worker_job(client, kpi):
    from app.services import forecast_snapshots as fs

    t = kpi["tenant"]
    start, _ = fs.period_bounds("QUARTER", date.today())
    with SessionLocal() as db:
        # Late in the period the worker leaves it to the admin
        fs.run_due(db, today=start + timedelta(days=fs.SCHEDULED_GRACE_DAYS + 1))
        assert sql("SELECT COUNT(*) FROM forecast_snapshots WHERE tenant_id = :t AND period_type = 'QUARTER'",
                   t=t["tenant_id"]) == 0
        fs.run_due(db, today=start)
        n = sql("SELECT COUNT(*) FROM forecast_snapshots WHERE tenant_id = :t AND period_type = 'QUARTER' "
                "AND source = 'SCHEDULED'", t=t["tenant_id"])
        assert n >= 4  # one row per sales user
        fs.run_due(db, today=start + timedelta(days=1))  # idempotent
        assert sql("SELECT COUNT(*) FROM forecast_snapshots WHERE tenant_id = :t AND period_type = 'QUARTER'",
                   t=t["tenant_id"]) == n
