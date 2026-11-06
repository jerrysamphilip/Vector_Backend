"""
Sales UAT gap fixes (fix agent E), functional (MySQL):

  * E1  reassigning a lead or a deal notifies the new owner (in-app ASSIGNED notification)
  * E2  a task created on a deal's contact (not on another deal) shows on the deal timeline
  * E5  GET /sales-reports/pipeline?date_from=&date_to= counts won / lost closed in the period; open stays as of now
  * E6  the round-robin index restarts when the pool changes; the settings row is locked for the pick
  * E8  GET /sales-reports/leaderboard?sort=attainment ranks by % of target
"""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.repositories.sales_admin import SqlAlchemySalesSettingsRepository
from app.services.sales import fiscal_quarter_of, fiscal_year_of
from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


def _uid():
    return uuid.uuid4().hex[:6]


def _user(client, tenant_id, label):
    from app.core.security import hash_password
    from app.models.user import User
    email = f"fe-{label}-{RUN}-{_uid()}@fixe{RUN}.example.com"
    password = f"fe-{label}-pass-{RUN}-{_uid()}"
    with SessionLocal() as db:
        u = User(tenant_id=tenant_id, first_name=label.title(), last_name="Rep", email=email,
                 password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                 email_verified=True)
        db.add(u)
        db.commit()
        user_id = u.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


@pytest.fixture(scope="module")
def ws(client):
    t = register_tenant(client, "fixe")
    t["user_id"] = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    t["rep1"] = _user(client, t["tenant_id"], "ria")
    t["rep2"] = _user(client, t["tenant_id"], "sam")
    stages = client.get("/sales/stages", headers=t["headers"]).json()
    t["open"] = next(s for s in stages if s["status"] == "OPEN")["stage_id"]
    t["won"] = next(s for s in stages if s["status"] == "WON")["stage_id"]
    t["lost"] = next(s for s in stages if s["status"] == "LOST")["stage_id"]
    return t


def _contact(client, h, label):
    r = client.post("/contacts", headers=h, json={"email": f"{label}.{_uid()}.{RUN}@fixe{RUN}.example.com",
                                                  "first_name": label.capitalize(), "last_name": "Buyer"})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _notifications(client, headers):
    return client.get("/notifications", headers=headers).json()["items"]


def _deal(client, ws, name, **kw):
    r = client.post("/opportunities", headers=ws["headers"], json={"name": f"{name} {RUN}", "amount": 1000,
                                                                    "stage_id": ws["open"], **kw})
    assert r.status_code == 201, r.text
    return r.json()["opportunity_id"]


# ── E1 ──

def test_reassigning_a_lead_notifies_the_new_owner(client, ws):
    h = ws["headers"]
    r = client.post("/leads", headers=h, json={"prospect_id": _contact(client, h, "reas"), "owner_id": ws["user_id"]})
    assert r.status_code == 201, r.text
    lid = r.json()["lead_id"]
    r = client.patch(f"/leads/{lid}", headers=h, json={"owner_id": ws["rep1"]["user_id"]})
    assert r.status_code == 200, r.text
    got = [n for n in _notifications(client, ws["rep1"]["headers"]) if n["link"] == f"/app/leads/{lid}"]
    assert len(got) == 1 and got[0]["kind"] == "ASSIGNED" and got[0]["title"].startswith("Lead reassigned: Reas")
    # Saving again without an owner change sends nothing more
    client.patch(f"/leads/{lid}", headers=h, json={"owner_id": ws["rep1"]["user_id"], "next_step": "Call"})
    assert len([n for n in _notifications(client, ws["rep1"]["headers"]) if n["link"] == f"/app/leads/{lid}"]) == 1


def test_reassigning_a_deal_notifies_the_new_owner(client, ws):
    oid = _deal(client, ws, "E1 deal")
    r = client.patch(f"/opportunities/{oid}", headers=ws["headers"], json={"owner_id": ws["rep2"]["user_id"]})
    assert r.status_code == 200, r.text
    got = [n for n in _notifications(client, ws["rep2"]["headers"]) if n["link"] == f"/app/deals/{oid}"]
    assert len(got) == 1 and got[0]["title"] == f"Deal reassigned: E1 deal {RUN}"


# ── E2 ──

def test_contact_tasks_show_on_the_deal_timeline(client, ws):
    h = ws["headers"]
    pid = _contact(client, h, "tl")
    deal_a, deal_b = _deal(client, ws, "E2 a", prospect_id=pid), _deal(client, ws, "E2 b", prospect_id=pid)
    titles = {}
    for key, link in (("contact", {}), ("deal_a", {"opportunity_id": deal_a}), ("deal_b", {"opportunity_id": deal_b})):
        r = client.post("/tasks", headers=h, json={"title": f"E2 {key} {RUN}", "prospect_id": pid, **link})
        assert r.status_code == 201, r.text
        titles[key] = f"E2 {key} {RUN}"
    items = client.get(f"/opportunities/{deal_a}/timeline", headers=h).json()["items"]
    shown = {i["task"]["title"] for i in items if i["kind"] == "TASK"}
    assert shown == {titles["contact"], titles["deal_a"]}  # not the task linked to the other deal


# ── E5 ──

def test_pipeline_report_period_limits_won_and_lost_only(client, ws):
    h = ws["headers"]
    _deal(client, ws, "E5 open")
    won = _deal(client, ws, "E5 won")
    r = client.patch(f"/opportunities/{won}", headers=h, json={"stage_id": ws["won"], "closed_reason": "Price"})
    assert r.status_code == 200, r.text
    me = ws["user_id"]

    def row(params):
        r = client.get("/sales-reports/pipeline", headers=h, params={"member": me, **params})
        assert r.status_code == 200, r.text
        return r.json(), next(o for o in r.json()["by_owner"] if o["owner_id"] == me)

    body, all_time = row({})
    assert body["period"] is None and all_time["won_count"] >= 1
    today = date.today()
    body, this_week = row({"date_from": str(today - timedelta(days=7)), "date_to": str(today)})
    assert body["period"] == {"from": str(today - timedelta(days=7)), "to": str(today)}
    assert this_week["won_count"] == all_time["won_count"] and this_week["open_count"] == all_time["open_count"]
    _, last_year = row({"date_from": str(today - timedelta(days=400)), "date_to": str(today - timedelta(days=370))})
    assert last_year["won_count"] == 0 and last_year["lost_count"] == 0
    assert last_year["open_count"] == all_time["open_count"]  # open pipeline is as of now
    assert client.get("/sales-reports/pipeline", headers=h,
                      params={"date_from": str(today), "date_to": str(today - timedelta(days=1))}).status_code == 400


# ── E6 ──

def test_round_robin_index_restarts_when_the_pool_changes(client, ws):
    h = ws["headers"]
    pool = [ws["rep1"]["user_id"], ws["rep2"]["user_id"]]
    r = client.put("/sales/settings", headers=h, json={"lead_assignment": {"mode": "round_robin", "users": pool}})
    assert r.status_code == 200, r.text
    owners = []
    for i in range(3):
        pid = _contact(client, h, f"rr{i}")
        sql("UPDATE prospects SET owner_id = NULL WHERE prospect_id = :p", p=pid)
        r = client.post("/leads", headers=h, json={"prospect_id": pid})
        assert r.status_code == 201, r.text
        owners.append(r.json()["owner_id"])
    assert owners == [pool[0], pool[1], pool[0]]
    assert client.get("/sales/settings", headers=h).json()["lead_assignment"]["next_index"] == 1
    # A client-sent index is ignored; the same pool keeps the index
    r = client.put("/sales/settings", headers=h, json={"lead_assignment": {"mode": "round_robin", "users": pool,
                                                                           "next_index": 0}})
    assert r.json()["lead_assignment"]["next_index"] == 1
    r = client.put("/sales/settings", headers=h, json={"lead_assignment": {"users": pool[::-1]}})
    assert r.json()["lead_assignment"]["next_index"] == 0
    client.put("/sales/settings", headers=h, json={"lead_assignment": {"mode": "off", "users": []}})


def test_round_robin_settings_read_locks_the_row(ws, client):
    client.put("/sales/settings", headers=ws["headers"], json={"stale_deal_days": 14})  # the row exists
    first, second = SessionLocal(), SessionLocal()
    try:
        assert SqlAlchemySalesSettingsRepository(first).stored_for_update(ws["tenant_id"]) is not None
        second.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        with pytest.raises(OperationalError):
            SqlAlchemySalesSettingsRepository(second).stored_for_update(ws["tenant_id"])
    finally:
        second.rollback()
        first.rollback()
        second.close()
        first.close()


# ── E8 ──

def test_leaderboard_sorts_by_attainment(client, ws):
    h = ws["headers"]
    rep1, rep2 = ws["rep1"]["user_id"], ws["rep2"]["user_id"]
    today = date.today()
    fy, quarter = fiscal_year_of(today), fiscal_quarter_of(today)
    for uid, amount in ((rep1, 100000), (rep2, 1000)):
        r = client.put("/sales/targets", headers=h, json={"user_id": uid, "fy": fy, "quarter": quarter,
                                                           "amount": amount})
        assert r.status_code == 200, r.text
    for uid, amount in ((rep1, 5000), (rep2, 2000)):
        oid = _deal(client, ws, f"E8 {uid[:4]}", owner_id=uid)
        r = client.patch(f"/opportunities/{oid}", headers=h, json={"stage_id": ws["won"], "closed_reason": "Price",
                                                                    "amount": amount})
        assert r.status_code == 200, r.text
    params = {"date_from": str(today), "date_to": str(today)}

    def order(sort):
        r = client.get("/sales-reports/leaderboard", headers=h, params={**params, "sort": sort})
        assert r.status_code == 200, r.text
        assert r.json()["sort"] == sort
        return [row for row in r.json()["rows"] if row["user_id"] in (rep1, rep2)]

    by_revenue = order("revenue")
    assert [r["user_id"] for r in by_revenue] == [rep1, rep2]
    by_attainment = order("attainment")
    assert [r["user_id"] for r in by_attainment] == [rep2, rep1]
    assert by_attainment[0]["target"] and by_attainment[0]["attainment"] > by_attainment[1]["attainment"]
