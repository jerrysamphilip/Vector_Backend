"""
Contact time zone (CM-01, BR-CM-01) and bulk owner change for companies (CM-36, BR-CM-36), against MySQL.

  * the zone follows country + state (India -> Asia/Kolkata), is None for a US contact without a state
    (scheduling then uses the campaign's zone), and a zone a user sets is validated and kept;
  * imports take a 'timezone' column; scheduling with respect_timezone uses the contact's zone;
  * POST /accounts/bulk assign_owner: an admin reassigns two companies with history rows; a scoped user
    cannot assign outside their team (403); unknown action 400; another workspace's companies are not found.
"""
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def tenant(client):
    return register_tenant(client, "tzcb")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "tzcc")


def _u():
    return uuid.uuid4().hex[:6]


def _contact(client, h, **kw):
    r = client.post("/contacts", headers=h, json={"email": f"tz-{_u()}@tzcb{RUN}.example.com", "first_name": "Tz", **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def _zone(pid):
    return sql("SELECT timezone FROM prospects WHERE prospect_id=:p", p=pid)


# ── CM-01 ──────────────────────────────────────────────────────────

def test_zone_follows_country_and_state(client, tenant):
    h = tenant["headers"]
    assert _zone(_contact(client, h, poc_country="India", poc_state="Maharashtra")) == "Asia/Kolkata"
    assert _zone(_contact(client, h, poc_country="Germany")) == "Europe/Berlin"
    assert _zone(_contact(client, h, poc_country="USA", poc_state="CA")) == "America/Los_Angeles"
    assert _zone(_contact(client, h, poc_country="USA")) is None  # no state: no guess (not New York)


def test_user_set_zone_is_validated_and_kept(client, tenant):
    h = tenant["headers"]
    pid = _contact(client, h, poc_country="India", timezone="Europe/London")
    assert _zone(pid) == "Europe/London"
    r = client.patch(f"/contacts/{pid}", headers=h, json={"poc_country": "Japan", "poc_state": "Tokyo"})
    assert r.status_code == 200, r.text
    assert _zone(pid) == "Europe/London"  # a location change does not overwrite a user's choice
    r = client.patch(f"/contacts/{pid}", headers=h, json={"timezone": "Mars/Olympus"})
    assert r.status_code == 400 and "IANA" in r.text
    r = client.patch(f"/contacts/{pid}", headers=h, json={"timezone": ""})  # back to the derived zone
    assert r.status_code == 200, r.text
    assert _zone(pid) == "Asia/Tokyo"
    r = client.patch(f"/contacts/{pid}", headers=h, json={"poc_country": "India", "poc_state": ""})
    assert r.status_code == 200 and _zone(pid) == "Asia/Kolkata"  # a derived zone follows the move
    history = sql("SELECT COUNT(*) FROM property_changes WHERE object_id=:p AND field='timezone'", p=pid)
    assert history >= 2


def test_sending_back_the_derived_zone_with_a_move_is_no_choice(client, tenant):
    """A form that re-sends the zone it loaded along with a new country still re-derives the zone."""
    h = tenant["headers"]
    pid = _contact(client, h, poc_country="France")
    assert _zone(pid) == "Europe/Paris"
    r = client.patch(f"/contacts/{pid}", headers=h, json={"poc_country": "Spain", "timezone": "Europe/Paris"})
    assert r.status_code == 200 and _zone(pid) == "Europe/Madrid"


def test_import_maps_a_timezone_column():
    from app.domain.imports import ALIASES, CONTACT_TARGETS
    assert CONTACT_TARGETS["timezone"] == "Time zone" and "time zone" in ALIASES["contact.timezone"]


def test_scheduling_uses_the_contacts_zone(client, tenant):
    from app.domain.campaigns_edit import ScheduleSettings, contact_timezone
    from app.models.prospect import Prospect
    from app.services.campaign_calendar import CALENDAR

    h = tenant["headers"]
    india, us_blank, own = (_contact(client, h, poc_country="India"), _contact(client, h, poc_country="USA"),
                            _contact(client, h, poc_state="NY", timezone="Asia/Singapore"))
    on = ScheduleSettings(timezone="America/Chicago", respect_timezone=True, calendar=CALENDAR)
    off = ScheduleSettings(timezone="America/Chicago", respect_timezone=False, calendar=CALENDAR)
    with SessionLocal() as db:
        contacts = {p.prospect_id: p for p in db.query(Prospect).filter(Prospect.prospect_id.in_([india, us_blank, own]))}
        assert contact_timezone(on, contacts[india]) == "Asia/Kolkata"
        assert contact_timezone(on, contacts[us_blank]) == "America/Chicago"  # falls back to the campaign
        assert contact_timezone(on, contacts[own]) == "Asia/Singapore"
        assert contact_timezone(off, contacts[india]) == "America/Chicago"


# ── CM-36: companies bulk owner change ─────────────────────────────

def _company(client, h, name):
    r = client.post("/accounts", headers=h, json={"name": f"{name} {RUN} {_u()}"})
    assert r.status_code == 201, r.text
    return r.json()["account_id"]


def _agent(client, tenant_id):
    """A scoped user: manages prospects but sees only their own records (sales level 3, no reports)."""
    from app.core.security import hash_password
    from app.models.user import User

    email = f"agent-{RUN}-{_u()}@tzcb{RUN}.example.com"
    password = f"agent-pass-{RUN}-{_u()}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name="Ag", last_name="Ent", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                    email_verified=True, sales_level=3, custom_permissions=["manage_prospects"])
        db.add(user)
        db.commit()
        user_id = user.user_id
    return user_id, auth(login(client, email, password))


def _owner(account_id):
    return sql("SELECT owner_id FROM accounts WHERE account_id=:a", a=account_id)


def test_admin_reassigns_two_companies(client, tenant):
    h = tenant["headers"]
    agent_id, _ = _agent(client, tenant["tenant_id"])
    a, b, untouched = (_company(client, h, n) for n in ("Bulk A", "Bulk B", "Bulk C"))
    version = sql("SELECT row_version FROM accounts WHERE account_id=:a", a=a)
    r = client.post("/accounts/bulk", headers=h, json={"account_ids": [a, b], "action": "assign_owner",
                                                       "owner_id": agent_id})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 2 and r.json()["skipped"] == []
    assert _owner(a) == agent_id and _owner(b) == agent_id and _owner(untouched) != agent_id
    assert sql("SELECT row_version FROM accounts WHERE account_id=:a", a=a) == version + 1
    assert sql("SELECT COUNT(*) FROM property_changes WHERE object_type='COMPANY' AND object_id=:a "
               "AND field='owner_id' AND source='BULK' AND new_value=:o", a=a, o=agent_id) == 1


def test_scoped_user_cannot_assign_outside_their_team(client, tenant):
    h = tenant["headers"]
    agent_id, agent_h = _agent(client, tenant["tenant_id"])
    mine = _company(client, agent_h, "Agent own")
    admin_id = sql("SELECT user_id FROM users WHERE email=:e", e=tenant["email"])
    r = client.post("/accounts/bulk", headers=agent_h, json={"account_ids": [mine], "action": "assign_owner",
                                                             "owner_id": admin_id})
    assert r.status_code == 403, r.text
    assert _owner(mine) == agent_id


def test_bulk_refusals(client, tenant, other):
    h = tenant["headers"]
    a = _company(client, h, "Bulk refusal")
    admin_id = sql("SELECT user_id FROM users WHERE email=:e", e=tenant["email"])
    r = client.post("/accounts/bulk", headers=h, json={"account_ids": [a], "action": "delete_all"})
    assert r.status_code == 400
    r = client.post("/accounts/bulk", headers=h, json={"account_ids": [a], "action": "assign_owner"})
    assert r.status_code == 400
    # another workspace cannot touch these companies, nor assign them to its own users
    other_id = sql("SELECT user_id FROM users WHERE email=:e", e=other["email"])
    r = client.post("/accounts/bulk", headers=other["headers"], json={"account_ids": [a], "action": "assign_owner",
                                                                      "owner_id": other_id})
    assert r.status_code == 404
    r = client.post("/accounts/bulk", headers=h, json={"account_ids": [a], "action": "assign_owner",
                                                       "owner_id": other_id})
    assert r.status_code == 400
    assert _owner(a) == admin_id
