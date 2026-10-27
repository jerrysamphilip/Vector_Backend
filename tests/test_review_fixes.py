"""Fixes from the external code-review round, in-process API tests.

- Reactivate: only INACTIVE members; only a Super Admin can reactivate a Super Admin; an invited
  member cannot be activated around the invitation.
- Invited sign-in (I-13): an invited account never signs in with a password (it has none; a legacy
  one is refused too); it joins only through a live, unused sign-in link.
- Platform tenant creation: the duplicate-name check is an exact (case-insensitive) match, so
  LIKE wildcards in a name do not match other tenants.
"""
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql
from app.core.clock import utcnow


def _user(client, tenant_id, label: str, role: str, status: str = "ACTIVE") -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@rvf{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status=status, auth_provider="local",
                    email_verified=True)
        db.add(user)
        db.commit()
        user_id = user.user_id
    out = {"user_id": user_id, "email": email, "password": password}
    if status == "ACTIVE":
        out["headers"] = auth(login(client, email, password))
    return out


def _magic_link(user_id: str, expires_in: timedelta, used: bool = False) -> str:
    from app.core.security import hash_token
    from app.models.magic_login_token import MagicLoginToken

    raw = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(MagicLoginToken(user_id=user_id, token_hash=hash_token(raw),
                               expires_at=utcnow() + expires_in,
                               used_at=utcnow() if used else None))
        db.commit()
    return raw


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "rvf")


# ── Reactivate ───────────────────────────────────────────────

def test_rvf_f01_admin_cannot_reactivate_a_super_admin(client, ws):
    admin = _user(client, ws["tenant_id"], "admin", "ADMIN")
    boss = _user(client, ws["tenant_id"], "boss", "SUPER_ADMIN", status="INACTIVE")
    r = client.put(f"/api/users/{boss['user_id']}/reactivate", headers=admin["headers"])
    assert r.status_code == 403, r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=boss["user_id"]) == "INACTIVE"
    # The workspace Super Admin can
    r = client.put(f"/api/users/{boss['user_id']}/reactivate", headers=ws["headers"])
    assert r.status_code == 200, r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=boss["user_id"]) == "ACTIVE"


def test_rvf_f02_invited_member_cannot_be_reactivated(client, ws):
    invited = _user(client, ws["tenant_id"], "invitee", "SUPER_ADMIN", status="INVITED")
    for headers in (ws["headers"], _user(client, ws["tenant_id"], "admin2", "ADMIN")["headers"]):
        r = client.put(f"/api/users/{invited['user_id']}/reactivate", headers=headers)
        assert r.status_code == 400, r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=invited["user_id"]) == "INVITED"


def test_rvf_f03_inactive_agent_reactivates_as_before(client, ws):
    admin = _user(client, ws["tenant_id"], "admin3", "ADMIN")
    agent = _user(client, ws["tenant_id"], "agent", "AGENT", status="INACTIVE")
    r = client.put(f"/api/users/{agent['user_id']}/reactivate", headers=admin["headers"])
    assert r.status_code == 200, r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=agent["user_id"]) == "ACTIVE"


# ── Invited sign-in ──────────────────────────────────────────

def _password_login(client, u):
    return client.post("/api/auth/login", json={"email": u["email"], "password": u["password"]})


def test_rvf_f04_invited_sign_in_needs_a_live_link(client, ws):
    # A legacy invited account that still carries a password: refused, even with a live link
    u = _user(client, ws["tenant_id"], "legacy", "AGENT", status="INVITED")
    _magic_link(u["user_id"], timedelta(hours=5))
    r = _password_login(client, u)
    assert r.status_code == 403 and "sign-in link" in r.json()["detail"], r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=u["user_id"]) == "INVITED"

    # A new-style invited account has no password at all
    sql("UPDATE users SET password_hash = NULL WHERE user_id = :u", u=u["user_id"])
    assert _password_login(client, u).status_code == 401

    expired = _magic_link(u["user_id"], timedelta(hours=-1))
    spent = _magic_link(u["user_id"], timedelta(hours=5), used=True)  # revoked or resent
    for token in (expired, spent):
        assert client.post("/api/auth/magic-login", json={"token": token}).status_code == 401
    assert sql("SELECT status FROM users WHERE user_id = :u", u=u["user_id"]) == "INVITED"

    live = _magic_link(u["user_id"], timedelta(hours=5))
    r = client.post("/api/auth/magic-login", json={"token": live})
    assert r.status_code == 200 and r.json()["first_login"] is True, r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=u["user_id"]) == "ACTIVE"


def test_rvf_f05_active_users_are_unaffected(client, ws):
    u = _user(client, ws["tenant_id"], "active", "AGENT")
    assert _password_login(client, u).status_code == 200


# ── Tenant name duplicate check ──────────────────────────────

def test_rvf_f06_tenant_duplicate_check_is_exact(client):
    padmin = _user(client, None, "padmin", "PLATFORM_ADMIN")
    base = f"Acme{RUN}"

    def create(name):
        return client.post("/api/admin/tenants", headers=padmin["headers"], json={
            "tenant_name": name, "admin_email": f"sa-{uuid.uuid4().hex[:8]}@rvf{RUN}.example.com",
            "admin_first_name": "Sam", "admin_last_name": "Admin"})

    r = create(f"{base} Ltd")
    assert r.status_code == 201, r.text
    # A wildcard name used to match the tenant above
    r = create(f"{base}%")
    assert r.status_code == 201, r.text
    r = create(f"{base}_Ltd")
    assert r.status_code == 201, r.text
    # A real duplicate (any case) is still refused
    assert create(f"{base.upper()} LTD").status_code == 409
