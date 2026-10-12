"""Browser sessions in httpOnly cookies (+ CSRF) and two-factor sign-in (TOTP)."""

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import totp
from app.main import app
from tests.conftest import RUN, SessionLocal, auth, register_tenant, sql

COOKIE_MODE = {"X-Auth-Mode": "cookie"}


def browser():
    """A fresh client with its own cookie jar (the shared `client` must not pick up cookies)."""
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def set_cookies(response) -> dict:
    """Set-Cookie headers of a response by cookie name (lower-cased attribute text)."""
    out = {}
    for header in response.headers.get_list("set-cookie"):
        name = header.split("=", 1)[0].strip()
        out[name] = header.lower()
    return out


def csrf(c) -> dict:
    return {"X-CSRF-Token": c.cookies.get("csrf_token"), **COOKIE_MODE}


def cookie_login(c, email, password):
    r = c.post("/api/auth/login", json={"email": email, "password": password}, headers=COOKIE_MODE)
    assert r.status_code == 200, r.text
    return r


def add_member(tenant_id: str, label: str) -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"member-{label}-{RUN}@{label}{RUN}.example.com"
    password = f"member-{label}-pass-{RUN}"
    with SessionLocal() as db:
        db.add(User(tenant_id=tenant_id, first_name="Mem", last_name="Ber", email=email,
                    password_hash=hash_password(password), role="AGENT", status="ACTIVE",
                    auth_provider="local", email_verified=True))
        db.commit()
    return {"email": email, "password": password}


def enable_mfa(client, headers) -> dict:
    r = client.post("/api/auth/mfa/setup", headers=headers)
    assert r.status_code == 200, r.text
    setup = r.json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/Outreach360:")
    assert "issuer=Outreach360" in setup["otpauth_uri"]
    secret = setup["secret"]
    r = client.post("/api/auth/mfa/enable", headers=headers, json={"code": totp.now_code(secret)})
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    return {"secret": secret, "recovery_codes": codes}


def next_code(secret: str) -> str:
    """A valid code not used yet: the next time step (accepted within the ±1 step window)."""
    return totp.code_at(secret, totp.current_step() + 1)


# ── Cookies & CSRF ──

def test_cookie_login_sets_httponly_cookies_and_no_body_tokens(client):
    t = register_tenant(client, "ck1")
    c = browser()
    r = cookie_login(c, t["email"], t["password"])
    body = r.json()
    assert "access_token" not in body and "refresh_token" not in body
    assert body["user"]["email"] == t["email"]

    cookies = set_cookies(r)
    assert "httponly" in cookies["access_token"] and "samesite=lax" in cookies["access_token"]
    assert "path=/" in cookies["access_token"]
    assert "httponly" in cookies["refresh_token"] and "samesite=strict" in cookies["refresh_token"]
    assert "httponly" not in cookies["csrf_token"] and "samesite=lax" in cookies["csrf_token"]

    r = c.get("/api/auth/session")
    assert r.status_code == 200, r.text
    assert r.json()["authenticated"] is True and r.json()["user"]["email"] == t["email"]
    assert c.get("/inboxes").status_code == 200

    # Token mode (no header) is unchanged: tokens in the body, no cookies
    r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
    assert r.status_code == 200 and r.json()["access_token"] and r.json()["refresh_token"]
    assert not set_cookies(r)

    assert browser().get("/api/auth/session").status_code == 401


def test_cookie_auth_requires_csrf_on_writes(client):
    t = register_tenant(client, "ck2")
    c = browser()
    cookie_login(c, t["email"], t["password"])

    r = c.put("/api/auth/me", json={"first_name": "NoCsrf"})
    assert r.status_code == 403
    r = c.put("/api/auth/me", json={"first_name": "NoCsrf"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    r = c.put("/api/auth/me", json={"first_name": "WithCsrf"}, headers=csrf(c))
    assert r.status_code == 200, r.text
    assert r.json()["first_name"] == "WithCsrf"


def test_bearer_needs_no_csrf(client):
    t = register_tenant(client, "ck3")
    r = client.put("/api/auth/me", headers=t["headers"], json={"first_name": "Bearer"})
    assert r.status_code == 200, r.text
    # The Bearer header wins over a cookie: a bad Bearer is rejected even with a valid cookie
    c = browser()
    cookie_login(c, t["email"], t["password"])
    assert c.get("/inboxes", headers=auth("not-a-jwt")).status_code == 401


def test_refresh_via_cookie_rotates(client):
    t = register_tenant(client, "ck4")
    c = browser()
    cookie_login(c, t["email"], t["password"])
    old_refresh = c.cookies.get("refresh_token")
    assert old_refresh

    assert c.post("/api/auth/refresh", headers=COOKIE_MODE).status_code == 403  # no CSRF header
    r = c.post("/api/auth/refresh", headers=csrf(c))
    assert r.status_code == 200, r.text
    assert "access_token" not in r.json() and "refresh_token" not in r.json()
    new_refresh = c.cookies.get("refresh_token")
    assert new_refresh and new_refresh != old_refresh
    assert c.get("/api/auth/session").status_code == 200

    # The old refresh token was revoked by the rotation
    r = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 401

    # Body refresh (script clients) still works and returns tokens in the body
    tokens = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}).json()
    r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200 and r.json()["refresh_token"] != tokens["refresh_token"]


def test_logout_revokes_and_clears_cookies(client):
    t = register_tenant(client, "ck5")
    c = browser()
    cookie_login(c, t["email"], t["password"])
    refresh = c.cookies.get("refresh_token")

    r = c.post("/api/auth/logout", headers=csrf(c))
    assert r.status_code == 204
    cleared = set_cookies(r)
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert name in cleared and ("max-age=0" in cleared[name] or "expires=" in cleared[name])
    assert c.get("/api/auth/session").status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": refresh}).status_code == 401

    # Token-mode logout with the refresh token in the body
    tokens = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}).json()
    assert client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


# ── Two-factor ──

def test_mfa_login_flow_replay_and_recovery_codes(client):
    t = register_tenant(client, "mfa1")
    h = t["headers"]
    mfa = enable_mfa(client, h)
    enable_code = totp.now_code(mfa["secret"])

    r = client.get("/api/auth/session", headers=h)
    assert r.status_code == 200 and r.json()["mfa_enabled"] is True and r.json()["recovery_codes_remaining"] == 10
    assert client.post("/api/auth/mfa/setup", headers=h).status_code == 409  # already on

    # Password alone no longer yields a session
    r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mfa_required"] is True and body["mfa_token"]
    assert "access_token" not in body and "refresh_token" not in body
    mfa_token = body["mfa_token"]
    # ...and the mfa_token is not an access token
    assert client.get("/inboxes", headers=auth(mfa_token)).status_code == 401

    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"
                                                  if enable_code != "000000" else "111111"})
    assert r.status_code == 401
    # Replay: the code used to enable two-factor can't be used again
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": enable_code})
    assert r.status_code == 401

    code = next_code(mfa["secret"])
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code})
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert client.get("/inboxes", headers=auth(tokens["access_token"])).status_code == 200
    # The mfa_token is spent
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code})
    assert r.status_code == 401

    # Replay of the same code on a new sign-in is refused
    mfa_token = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}).json()["mfa_token"]
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code})
    assert r.status_code == 401

    # A recovery code works once
    recovery = mfa["recovery_codes"][0]
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "recovery_code": recovery.upper()})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    assert sql("SELECT COUNT(*) FROM audit_logs WHERE user_id = (SELECT user_id FROM users WHERE email = :e) "
               "AND action IN ('ENABLE_MFA', 'RECOVERY_CODE_USED_MFA')", e=t["email"]) == 2
    mfa_token = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}).json()["mfa_token"]
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "recovery_code": recovery})
    assert r.status_code == 401
    r = client.get("/api/auth/session", headers=h)
    assert r.json()["recovery_codes_remaining"] == 9

    # Regenerate recovery codes (needs a fresh code). Every code in the ±1 window may be used up by
    # now, so move the last-used step back as if a minute had passed.
    sql("UPDATE users SET mfa_last_step = :s WHERE email = :e", s=totp.current_step() - 2, e=t["email"])
    r = client.post("/api/auth/mfa/recovery-codes", headers=h, json={"code": totp.now_code(mfa["secret"])})
    assert r.status_code == 200, r.text
    new_codes = r.json()["recovery_codes"]
    assert len(new_codes) == 10 and mfa["recovery_codes"][1] not in new_codes
    # The old codes are gone; disable needs the password and a code
    disable = {"password": t["password"], "recovery_code": new_codes[0]}
    r = client.post("/api/auth/mfa/disable", headers=h, json={**disable, "password": "wrong-password"})
    assert r.status_code == 401
    r = client.post("/api/auth/mfa/disable", headers=h, json=disable)
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
    assert r.status_code == 200 and r.json()["access_token"] and not r.json().get("mfa_required")


def test_mfa_verify_locks_after_failures(client):
    t = register_tenant(client, "mfa2")
    mfa = enable_mfa(client, t["headers"])
    mfa_token = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}).json()["mfa_token"]
    good = next_code(mfa["secret"])
    bad = "123456" if good != "123456" else "654321"
    for _ in range(5):
        r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": bad})
        assert r.status_code == 401
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": mfa_token, "code": good})
    assert r.status_code == 429


def test_mfa_cookie_mode_verify_sets_cookies(client):
    t = register_tenant(client, "mfa3")
    mfa = enable_mfa(client, t["headers"])
    c = browser()
    r = cookie_login(c, t["email"], t["password"])
    assert r.json()["mfa_required"] is True and "access_token" not in set_cookies(r)
    assert c.get("/api/auth/session").status_code == 401
    r = c.post("/api/auth/mfa/verify", headers=COOKIE_MODE,
               json={"mfa_token": r.json()["mfa_token"], "code": next_code(mfa["secret"])})
    assert r.status_code == 200, r.text
    assert "access_token" not in r.json()
    assert "httponly" in set_cookies(r)["access_token"]
    assert c.get("/api/auth/session").json()["mfa_enabled"] is True


def test_magic_login_requires_mfa(client):
    from app.core.security import hash_token
    from app.models.magic_login_token import MagicLoginToken

    t = register_tenant(client, "mfa4")
    mfa = enable_mfa(client, t["headers"])
    user_id = sql("SELECT user_id FROM users WHERE email = :e", e=t["email"])
    raw = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        db.add(MagicLoginToken(user_id=user_id, token_hash=hash_token(raw),
                               expires_at=datetime.utcnow() + timedelta(hours=1)))
        db.commit()

    r = client.post("/api/auth/magic-login", json={"token": raw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mfa_required"] is True and "access_token" not in body
    # The magic link is spent even though the sign-in isn't finished
    assert client.post("/api/auth/magic-login", json={"token": raw}).status_code == 401
    r = client.post("/api/auth/mfa/verify", json={"mfa_token": body["mfa_token"], "code": next_code(mfa["secret"])})
    assert r.status_code == 200 and r.json()["access_token"]


def test_tenant_require_mfa_blocks_users_without_mfa(client):
    t = register_tenant(client, "mfa5")
    h = t["headers"]
    # Can't require it before the admin has it
    assert client.put("/api/auth/mfa/tenant-policy", headers=h, json={"require_mfa": True}).status_code == 400
    admin_mfa = enable_mfa(client, h)
    member = add_member(t["tenant_id"], "mfa5")
    member_token = client.post("/api/auth/login", json={"email": member["email"],
                                                        "password": member["password"]}).json()["access_token"]
    mh = auth(member_token)
    # Only a SUPER_ADMIN may set the policy
    assert client.put("/api/auth/mfa/tenant-policy", headers=mh, json={"require_mfa": True}).status_code == 403
    r = client.put("/api/auth/mfa/tenant-policy", headers=h, json={"require_mfa": True})
    assert r.status_code == 200 and r.json() == {"require_mfa": True}

    try:
        # Sign-in still works, but the session is flagged and everything else is blocked
        r = client.post("/api/auth/login", json={"email": member["email"], "password": member["password"]})
        assert r.status_code == 200 and r.json()["user"]["mfa_setup_required"] is True
        mh = auth(r.json()["access_token"])
        r = client.get("/inboxes", headers=mh)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "MFA_SETUP_REQUIRED"
        assert client.get("/api/users", headers=mh).status_code == 403
        assert client.put("/api/auth/me", headers=mh, json={"first_name": "X"}).status_code == 403
        r = client.get("/api/auth/session", headers=mh)
        assert r.status_code == 200 and r.json()["mfa_setup_required"] is True and r.json()["mfa_required"] is True
        assert client.get("/api/auth/me", headers=mh).status_code == 200

        # The admin (who has two-factor) is unaffected
        assert client.get("/inboxes", headers=h).status_code == 200

        # Setting it up unblocks the member; turning it off is then refused
        member_mfa = enable_mfa(client, mh)
        r = client.put("/api/auth/me", headers=mh, json={"first_name": "Unblocked"})
        assert r.status_code == 200, r.text
        r = client.get("/inboxes", headers=mh)  # an AGENT lacks manage_inboxes, but no MFA block now
        assert r.status_code == 403 and r.json()["detail"] != {} and "MFA_SETUP_REQUIRED" not in r.text
        assert client.get("/api/auth/session", headers=mh).json()["mfa_setup_required"] is False
        r = client.post("/api/auth/mfa/disable", headers=mh, json={
            "password": member["password"], "recovery_code": member_mfa["recovery_codes"][0]})
        assert r.status_code == 403
    finally:
        r = client.put("/api/auth/mfa/tenant-policy", headers=h, json={"require_mfa": False})
        assert r.status_code == 200 and r.json() == {"require_mfa": False}
    assert admin_mfa["secret"]


@pytest.mark.parametrize("code", ["", "12345", "abcdef"])
def test_totp_rejects_malformed_codes(code):
    secret = totp.generate_secret()
    assert totp.matching_step(secret, code) is None
    assert totp.matching_step(secret, totp.now_code(secret)) is not None
