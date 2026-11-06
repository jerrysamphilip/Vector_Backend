"""
The Team page's "Market Research" preset (custom permissions [manage_campaigns, manage_prospects] on an AGENT):
the super admin can save it, and the agent may then reassign contacts, within the same visibility as before
(their own team only).
"""
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant

MARKET_RESEARCH = ["manage_campaigns", "manage_prospects"]


def _uid():
    return uuid.uuid4().hex[:6]


def _agent(client, tenant_id, label, level, manager_id=None):
    from app.core.security import hash_password
    from app.models.user import User
    email = f"mr-{label}-{RUN}-{_uid()}@mr{RUN}.example.com"
    password = f"mr-{label}-pass-{RUN}-{_uid()}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.title(), last_name="Agent", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE", auth_provider="local",
                    email_verified=True, sales_level=level, manager_id=manager_id)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


@pytest.fixture(scope="module")
def world(client):
    t = register_tenant(client, "mresearch")
    lead = _agent(client, t["tenant_id"], "lead", 3)
    rep = _agent(client, t["tenant_id"], "rep", 4, lead["user_id"])
    other = _agent(client, t["tenant_id"], "other", 3)
    return {"admin": t["headers"], "lead": lead, "rep": rep, "other": other}


def _own_contact(client, headers, owner_id):
    r = client.post("/contacts", headers=headers, json={
        "email": f"mr.{_uid()}.{RUN}@mrclient{RUN}.example.com", "first_name": "Mia", "owner_id": owner_id})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


def test_market_research_preset_lets_an_agent_reassign_within_their_team(client, world):
    lead = world["lead"]
    pid = _own_contact(client, lead["headers"], lead["user_id"])
    r = client.patch(f"/contacts/{pid}", headers=lead["headers"], json={"owner_id": world["rep"]["user_id"]})
    assert r.status_code == 403, r.text  # a plain agent cannot reassign

    r = client.put(f"/api/users/{lead['user_id']}/permissions", headers=world["admin"],
                   json={"permissions": MARKET_RESEARCH})
    assert r.status_code == 200 and sorted(r.json()["permissions"]) == MARKET_RESEARCH, r.text

    r = client.patch(f"/contacts/{pid}", headers=lead["headers"], json={"owner_id": world["rep"]["user_id"]})
    assert r.status_code == 200, r.text
    # Visibility is unchanged: still only their own team
    pid2 = _own_contact(client, lead["headers"], lead["user_id"])
    r = client.patch(f"/contacts/{pid2}", headers=lead["headers"], json={"owner_id": world["other"]["user_id"]})
    assert r.status_code == 403, r.text


def test_only_a_super_admin_sets_the_preset(client, world):
    r = client.put(f"/api/users/{world['rep']['user_id']}/permissions", headers=world["lead"]["headers"],
                   json={"permissions": MARKET_RESEARCH})
    assert r.status_code == 403, r.text
