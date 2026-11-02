"""UAT seed (app/db/seed_uat_users): placeholder users per role, random passwords shown once,
sign-in works with them, a rerun changes nothing, and it never runs in production."""
import io
import uuid

import pytest

from app.core.config import settings
from app.db import seed_uat_users as seed_mod
from app.models import User
from tests.conftest import SessionLocal, auth, login


def users_at(domain):
    db = SessionLocal()
    try:
        return {u.email: (u.role, u.status, u.tenant_id, u.password_hash, u.user_id, u.manager_id)
                for u in db.query(User).filter(User.email.like(f"%@{domain}"))}
    finally:
        db.close()


@pytest.fixture(scope="module")
def seeded():
    domain = f"uat-{uuid.uuid4().hex[:8]}.example"
    db = SessionLocal()
    try:
        first = seed_mod.seed(db, domain, 2, f"UAT seed {domain}")
        again = seed_mod.seed(db, domain, 2, f"UAT seed {domain}")
    finally:
        db.close()
    return domain, first, again


def test_seed_creates_two_users_per_role_with_random_passwords(seeded):
    domain, first, _ = seeded
    assert [(o.email, o.role) for o in first] == [
        (f"uat.{slug}{n}@{domain}", role)
        for role, slug in (("SUPER_ADMIN", "superadmin"), ("ADMIN", "admin"), ("MANAGER", "manager"), ("AGENT", "agent"))
        for n in (1, 2)]
    passwords = [o.password for o in first]
    assert all(p and len(p) >= 16 for p in passwords) and len(set(passwords)) == len(passwords)
    rows = list(users_at(domain).values())
    assert len(rows) == 8 and len({r[2] for r in rows}) == 1          # one workspace
    assert all(r[1] == "ACTIVE" for r in rows)
    assert all(r[3] and r[3] not in passwords for r in rows)          # stored hashed, never in clear


def test_seeded_users_sign_in_with_the_right_role(client, seeded):
    domain, first, _ = seeded
    for o in first:
        me = client.get("/api/auth/me", headers=auth(login(client, o.email, o.password)))
        assert me.status_code == 200, o.email
        assert me.json()["role"] == o.role


def test_agents_report_to_their_manager(seeded):
    domain, _, _ = seeded
    users = users_at(domain)
    for n in (1, 2):
        assert users[f"uat.agent{n}@{domain}"][5] == users[f"uat.manager{n}@{domain}"][4]


def test_rerun_leaves_existing_accounts_and_passwords_alone(client, seeded):
    domain, first, again = seeded
    assert all(o.password is None and "unchanged" in o.note for o in again)
    assert len(users_at(domain)) == 8
    login(client, first[0].email, first[0].password)                  # the first password still works


def test_output_shows_each_password_once(seeded):
    _, first, _ = seeded
    buf = io.StringIO()
    seed_mod.print_outcomes(first, out=buf)
    text = buf.getvalue()
    assert all(text.count(o.password) == 1 for o in first)
    assert "8 created, 0 left unchanged." in text


def test_refuses_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(SystemExit, match="production"):
        seed_mod.main(["--domain", "never.example"])
    assert users_at("never.example") == {}


def test_per_role_is_bounded():
    with pytest.raises(SystemExit):
        seed_mod.main(["--per-role", "0"])
