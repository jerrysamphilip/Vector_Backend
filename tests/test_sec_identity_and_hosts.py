"""SEC fixes, end to end against the database.

- F017: one ACTIVE / INVITED account per email across workspaces: invite, accept-invite and reactivate
  refuse a second live account (409); the unique users.active_email index backs it; sign-in and
  forgot-password act on the live account when old rows share the address.
- F024: a reset link requested with a malicious Origin falls back to the configured URL.
- F007: a mailbox on a loopback / metadata host is refused (400) when deployed; metadata always.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.security import decode_access_token, hash_password
from app.models.user import User
from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def ws_a(client):
    return register_tenant(client, "seca")


@pytest.fixture(scope="module")
def ws_b(client):
    return register_tenant(client, "secb")


@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    def fake_send(**kw):
        sent.append(kw)
        return {"sent": True, "ses_message_id": f"msg-{len(sent)}"}

    monkeypatch.setattr("app.services.invitation_email.send_invitation_email", fake_send)
    return sent


def _email(tag: str = "sec") -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}@sec{RUN}.example.com"


def _invite(client, ws, email, role="AGENT"):
    return client.post("/api/users/invite", headers=ws["headers"], json={"email": email, "role": role})


def _join(client, link: str) -> str:
    r = client.post("/api/auth/magic-login", json={"token": link.split("token=", 1)[1]})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_sec_invite_refused_when_email_is_live_in_another_workspace(client, ws_a, ws_b, mailbox):
    # Owner of A is ACTIVE there: B cannot invite the same address
    r = _invite(client, ws_b, ws_a["email"])
    assert r.status_code == 409, r.text
    # An INVITED account counts as live too
    email = _email("inv")
    assert _invite(client, ws_a, email).status_code == 201
    r = _invite(client, ws_b, email.upper())  # the collation is case-insensitive
    assert r.status_code == 409, r.text
    assert sql("SELECT COUNT(*) FROM users WHERE email = :e", e=email) == 1


def test_sec_reactivate_refused_when_email_went_live_elsewhere(client, ws_a, ws_b, mailbox):
    email = _email("react")
    r = _invite(client, ws_a, email)
    assert r.status_code == 201, r.text
    user_a = r.json()["user_id"]
    _join(client, mailbox[-1]["magic_login_link"])
    assert client.delete(f"/api/users/{user_a}", headers=ws_a["headers"]).status_code == 200
    assert sql("SELECT status FROM users WHERE user_id = :u", u=user_a) == "INACTIVE"
    # Deactivated in A: B may invite the address
    r = _invite(client, ws_b, email)
    assert r.status_code == 201, r.text
    # ... and then A may not bring its old account back to life
    r = client.put(f"/api/users/{user_a}/reactivate", headers=ws_a["headers"])
    assert r.status_code == 409, r.text
    assert sql("SELECT status FROM users WHERE user_id = :u", u=user_a) == "INACTIVE"


def test_sec_accept_invite_refused_when_email_went_live_elsewhere(client, ws_b, mailbox):
    email = _email("acc")
    r = _invite(client, ws_b, email)
    assert r.status_code == 201, r.text
    invited_id = r.json()["user_id"]
    token = mailbox[-1]["magic_login_link"].split("token=", 1)[1]
    # An invitation whose account was never created up front (older data): drop the INVITED row
    sql("DELETE FROM magic_login_tokens WHERE user_id = :u", u=invited_id)
    sql("DELETE FROM users WHERE user_id = :u", u=invited_id)
    # Meanwhile the address became the owner of its own workspace
    other = client.post("/api/auth/register", json={"first_name": "O", "last_name": "W", "email": email,
                                                    "password": "other-pass-123", "tenant_name": f"Own {RUN}"})
    assert other.status_code == 201, other.text
    r = client.post("/api/users/accept-invite", json={"token": token, "first_name": "A", "last_name": "B",
                                                      "password": "accept-pass-123"})
    assert r.status_code == 409, r.text
    assert sql("SELECT COUNT(*) FROM users WHERE email = :e", e=email) == 1


def test_sec_database_refuses_a_second_live_account(ws_a, ws_b):
    """The unique index is the backstop for any path that skips the checks."""
    with SessionLocal() as db:
        db.add(User(tenant_id=ws_b["tenant_id"], first_name="Dup", last_name="Licate", email=ws_a["email"],
                    role="AGENT", status="ACTIVE", password_hash=None))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # A deactivated duplicate is fine (active_email is NULL)
        db.add(User(tenant_id=ws_b["tenant_id"], first_name="Old", last_name="Account", email=ws_a["email"],
                    role="AGENT", status="INACTIVE", password_hash=None))
        db.commit()
    assert sql("SELECT COUNT(*) FROM users WHERE email = :e AND active_email IS NULL", e=ws_a["email"]) == 1


def test_sec_login_and_forgot_password_pick_the_live_account(client, ws_b, monkeypatch):
    live = register_tenant(client, "secl")
    live_id = sql("SELECT user_id FROM users WHERE email = :e", e=live["email"])
    # An older, deactivated account with the same address and a lower id (what .first() used to find)
    old_password = "old-account-pass-1"
    with SessionLocal() as db:
        db.add(User(user_id="00000000-0000-4000-8000-" + uuid.uuid4().hex[:12], tenant_id=ws_b["tenant_id"],
                    first_name="Old", last_name="Me", email=live["email"], role="ADMIN", status="INACTIVE",
                    password_hash=hash_password(old_password)))
        db.commit()

    token = login(client, live["email"], live["password"])
    assert decode_access_token(token)["sub"] == live_id
    r = client.post("/api/auth/login", json={"email": live["email"], "password": old_password})
    assert r.status_code == 401, r.text

    sent = []
    monkeypatch.setattr("app.routers.auth_router._send_password_reset_email",
                        lambda to, link: sent.append((to, link)))
    monkeypatch.setattr(settings, "RESET_PASSWORD_URL", "https://app.example.com/reset-password")
    r = client.post("/api/auth/forgot-password", json={"email": live["email"]},
                    headers={"Origin": "http://evil.example\\@localhost:5173"})
    assert r.status_code == 200, r.text
    assert sql("SELECT user_id FROM password_reset_tokens WHERE user_id IN "
               "(SELECT user_id FROM users WHERE email = :e) ORDER BY created_at DESC LIMIT 1",
               e=live["email"]) == live_id
    assert len(sent) == 1
    link = sent[0][1]
    assert link.startswith("https://app.example.com/reset-password?token="), link
    assert "evil" not in link


def test_sec_reset_link_keeps_a_legit_local_origin(client, ws_a, monkeypatch):
    sent = []
    monkeypatch.setattr("app.routers.auth_router._send_password_reset_email",
                        lambda to, link: sent.append(link))
    r = client.post("/api/auth/forgot-password", json={"email": ws_a["email"]},
                    headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert sent and sent[0].startswith("http://localhost:5173/reset-password?token=")


def _inbox(client, ws, host):
    domain = f"in{uuid.uuid4().hex[:6]}{RUN}.example.com"
    return client.post("/inboxes", headers=ws["headers"], json={
        "email_address": f"sender@{domain}", "smtp_host": host, "smtp_port": 587,
        "smtp_username": "sender", "smtp_password": "x-smtp-pass", "imap_host": host, "imap_port": 993,
        "imap_username": "sender", "imap_password": "x-imap-pass", "warmup_enabled": False})


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "10.0.0.8", "localhost"])
def test_sec_inbox_internal_host_refused_when_deployed(client, ws_a, monkeypatch, host):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    r = _inbox(client, ws_a, host)
    assert r.status_code == 400, r.text
    assert "Mail server not allowed" in r.json()["detail"]


def test_sec_inbox_metadata_host_refused_locally_too(client, ws_a):
    assert settings.is_local_environment
    r = _inbox(client, ws_a, "169.254.169.254")
    assert r.status_code == 400, r.text
    r = _inbox(client, ws_a, "127.0.0.1")  # local test servers keep working
    assert r.status_code == 201, r.text


def test_sec_inbox_update_refused_when_deployed(client, ws_a, monkeypatch):
    r = _inbox(client, ws_a, "127.0.0.1")
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    r = client.put(f"/inboxes/{inbox_id}", headers=auth(ws_a["token"]), json={"smtp_host": "169.254.169.254"})
    assert r.status_code == 400, r.text
    r = client.post(f"/inboxes/{inbox_id}/test-smtp", headers=auth(ws_a["token"]))
    assert r.status_code == 400, r.text
