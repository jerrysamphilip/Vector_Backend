"""I-13: no password is ever returned in an API response; invitations email a set-password link.

- Invite / resend: the response and the email carry no password; the invited account has none.
- The invitee joins with the emailed link (/magic-login), then sets a first password without
  being asked for a "current" one (POST /api/auth/me/set-password), then signs in with it.
- Password sign-in for an INVITED account is refused; resend revokes the old link.
- The show-once fallback (the link, never a password) is local development only, never production.
- Platform admin tenant creation follows the same rules.
"""
import json
import logging
import uuid

import pytest

from app.core.config import settings
from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "inl")


@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    def fake_send(**kw):
        sent.append(kw)
        return {"sent": True, "ses_message_id": f"msg-{len(sent)}"}

    monkeypatch.setattr("app.services.invitation_email.send_invitation_email", fake_send)
    return sent


@pytest.fixture
def no_email(monkeypatch):
    def failing_send(**kw):
        raise RuntimeError("SES unavailable")

    monkeypatch.setattr("app.services.invitation_email.send_invitation_email", failing_send)


def _email() -> str:
    return f"inv-{uuid.uuid4().hex[:8]}@inl{RUN}.example.com"


def _token(link: str) -> str:
    return link.split("token=", 1)[1]


def _no_password_in(payload) -> None:
    text = json.dumps(payload).lower()
    assert "password" not in text, text


def _invite(client, ws, email: str, role: str = "AGENT") -> dict:
    r = client.post("/api/users/invite", headers=ws["headers"], json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def _join_and_set_password(client, link: str, new_password: str) -> None:
    r = client.post("/api/auth/magic-login", json={"token": _token(link)})
    assert r.status_code == 200 and r.json()["first_login"] is True, r.text
    _no_password_in(r.json())
    r = client.post("/api/auth/me/set-password", headers=auth(r.json()["access_token"]),
                    json={"new_password": new_password})
    assert r.status_code == 200, r.text


def test_inl_01_invite_carries_no_password_and_account_has_none(client, ws, mailbox, caplog):
    email = _email()
    caplog.set_level(logging.DEBUG)
    body = _invite(client, ws, email)
    _no_password_in(body)
    assert body["email_delivery"]["sent"] is True and body["show_once_link"] is None
    mail = mailbox[-1]
    assert mail["to_email"] == email and "/magic-login?token=" in mail["magic_login_link"]
    assert not any("password" in k for k in mail)
    assert sql("SELECT password_hash FROM users WHERE user_id = :u", u=body["user_id"]) is None
    assert sql("SELECT status FROM users WHERE user_id = :u", u=body["user_id"]) == "INVITED"
    # Neither the link's token nor anything password-like reaches the logs
    logged = caplog.text
    assert _token(mail["magic_login_link"]) not in logged


def test_inl_02_password_login_for_invited_user_fails(client, ws, mailbox):
    email = _email()
    _invite(client, ws, email)
    for guess in ("", "anything-at-all", "P@ssw0rd123"):
        r = client.post("/api/auth/login", json={"email": email, "password": guess})
        assert r.status_code in (401, 422), r.text


def test_inl_03_link_then_set_password_then_login(client, ws, mailbox):
    email = _email()
    body = _invite(client, ws, email)
    link = mailbox[-1]["magic_login_link"]
    new_password = f"chosen-{RUN}-pw"
    _join_and_set_password(client, link, new_password)
    assert sql("SELECT status FROM users WHERE user_id = :u", u=body["user_id"]) == "ACTIVE"
    assert sql("SELECT status FROM invitations WHERE invitation_id = :i", i=body["invitation_id"]) == "ACCEPTED"
    token = login(client, email, new_password)
    # The link works once
    assert client.post("/api/auth/magic-login", json={"token": _token(link)}).status_code == 401
    # Once a password exists, set-password is refused: change it with the current one instead
    r = client.post("/api/auth/me/set-password", headers=auth(token), json={"new_password": "another-pw-123"})
    assert r.status_code == 409, r.text
    r = client.put("/api/auth/me/password", headers=auth(token),
                   json={"current_password": "wrong-one", "new_password": "another-pw-123"})
    assert r.status_code == 401
    login(client, email, new_password)


def test_inl_04_set_password_validates_length(client, ws, mailbox):
    email = _email()
    _invite(client, ws, email)
    r = client.post("/api/auth/magic-login", json={"token": _token(mailbox[-1]["magic_login_link"])})
    headers = auth(r.json()["access_token"])
    r = client.post("/api/auth/me/set-password", headers=headers, json={"new_password": "short"})
    assert r.status_code == 400
    assert sql("SELECT password_hash FROM users WHERE email = :e", e=email) is None


def test_inl_05_resend_issues_new_link_and_revokes_old(client, ws, mailbox):
    email = _email()
    body = _invite(client, ws, email)
    first = mailbox[-1]["magic_login_link"]
    sql("UPDATE invitations SET created_at = NOW() - INTERVAL 120 SECOND WHERE email = :e", e=email)
    r = client.post(f"/api/users/{body['user_id']}/resend-invite", headers=ws["headers"])
    assert r.status_code == 200, r.text
    _no_password_in(r.json())
    second = mailbox[-1]["magic_login_link"]
    assert second != first
    assert client.post("/api/auth/magic-login", json={"token": _token(first)}).status_code == 401
    assert sql("SELECT password_hash FROM users WHERE user_id = :u", u=body["user_id"]) is None
    _join_and_set_password(client, second, f"resent-{RUN}-pw")
    login(client, email, f"resent-{RUN}-pw")


def test_inl_06_show_once_link_only_in_local_dev(client, ws, no_email, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    email = _email()
    body = _invite(client, ws, email)
    _no_password_in(body)
    assert body["email_delivery"]["sent"] is False
    shown = body["show_once_link"]
    assert shown["email"] == email and "/magic-login?token=" in shown["magic_login_link"]
    # It is a real, working set-password link
    _join_and_set_password(client, shown["magic_login_link"], f"local-{RUN}-pw")


@pytest.mark.parametrize("environment,k8s", [("production", None), ("development", "10.0.0.1")])
def test_inl_07_production_refuses_show_once(client, ws, no_email, monkeypatch, environment, k8s):
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    if k8s:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", k8s)
    else:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    body = _invite(client, ws, _email())
    _no_password_in(body)
    assert body["email_delivery"]["sent"] is False and body["show_once_link"] is None


# ── Platform admin tenant creation ───────────────────────────

@pytest.fixture(scope="module")
def padmin(client):
    from app.core.security import hash_password
    from app.models.user import User

    email = f"padmin-{uuid.uuid4().hex[:6]}@inl{RUN}.example.com"
    password = f"padmin-pass-{RUN}"
    with SessionLocal() as db:
        db.add(User(tenant_id=None, first_name="Plat", last_name="Admin", email=email,
                    password_hash=hash_password(password), role="PLATFORM_ADMIN", status="ACTIVE",
                    auth_provider="local", email_verified=True))
        db.commit()
    return auth(login(client, email, password))


@pytest.fixture
def tenant_mail(monkeypatch):
    sent = []

    def fake_send(to_email, tenant_name, magic_login_link):
        sent.append({"to_email": to_email, "magic_login_link": magic_login_link})
        return {"sent": True, "ses_message_id": "m", "error": None}

    monkeypatch.setattr("app.services.platform_admin_service.send_tenant_invite_email", fake_send)
    return sent


def _create_tenant(client, padmin) -> tuple[str, dict]:
    email = f"sa-{uuid.uuid4().hex[:8]}@inl{RUN}.example.com"
    r = client.post("/api/admin/tenants", headers=padmin, json={
        "tenant_name": f"Inl {uuid.uuid4().hex[:8]}", "admin_email": email,
        "admin_first_name": "Sam", "admin_last_name": "Admin"})
    assert r.status_code == 201, r.text
    return email, r.json()


def test_inl_08_platform_tenant_admin_gets_link_not_password(client, padmin, tenant_mail):
    email, body = _create_tenant(client, padmin)
    _no_password_in(body)
    assert body["email_sent"] is True and body["magic_login_link"] is None
    assert sql("SELECT password_hash FROM users WHERE user_id = :u", u=body["admin_user_id"]) is None
    assert client.post("/api/auth/login", json={"email": email, "password": "x" * 16}).status_code == 401
    _join_and_set_password(client, tenant_mail[-1]["magic_login_link"], f"tenant-{RUN}-pw")
    login(client, email, f"tenant-{RUN}-pw")


def test_inl_09_platform_show_once_only_in_local_dev(client, padmin, monkeypatch):
    monkeypatch.setattr("app.services.platform_admin_service.send_tenant_invite_email",
                        lambda **kw: {"sent": False, "ses_message_id": None, "error": "no SES"})
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    _, body = _create_tenant(client, padmin)
    _no_password_in(body)
    assert "/magic-login?token=" in body["magic_login_link"]

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    _, body = _create_tenant(client, padmin)
    assert body["email_sent"] is False and body["magic_login_link"] is None
