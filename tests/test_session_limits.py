"""Session limits at POST /api/auth/refresh (ADR 0007): idle timeout, absolute cap, reuse detection."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.core.security import hash_token
from app.main import app
from tests.conftest import register_tenant, sql

COOKIE_MODE = {"X-Auth-Mode": "cookie"}
ACTIVITY = "X-User-Activity"


def browser():
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def csrf(c, **extra) -> dict:
    return {"X-CSRF-Token": c.cookies.get("csrf_token"), **COOKIE_MODE, **extra}


def body_login(client, t) -> dict:
    r = client.post("/api/auth/login", json={"email": t["email"], "password": t["password"]})
    assert r.status_code == 200, r.text
    return r.json()


def refresh(client, token, headers=None):
    return client.post("/api/auth/refresh", json={"refresh_token": token}, headers=headers or {})


def row(token: str, column: str):
    return sql(f"SELECT {column} FROM refresh_tokens WHERE token_hash = :h", h=hash_token(token))


def shift(token: str, column: str, minutes: float) -> None:
    """Move a timestamp of the token's row `minutes` into the past (relative to now)."""
    sql(f"UPDATE refresh_tokens SET {column} = :v WHERE token_hash = :h",
        v=datetime.utcnow() - timedelta(minutes=minutes), h=hash_token(token))


def cleared(response) -> set:
    out = set()
    for header in response.headers.get_list("set-cookie"):
        low = header.lower()
        if "max-age=0" in low:
            out.add(header.split("=", 1)[0].strip())
    return out


def test_refresh_within_limits_rotates_in_the_same_session(client):
    t = register_tenant(client, "sl1")
    first = body_login(client, t)["refresh_token"]
    session_id, started = row(first, "session_id"), row(first, "session_started_at")
    assert session_id and started and row(first, "last_activity_at")

    r = refresh(client, first, {ACTIVITY: "3"})
    assert r.status_code == 200, r.text
    second = r.json()["refresh_token"]
    assert row(second, "session_id") == session_id
    assert row(second, "session_started_at") == started
    assert row(first, "revoked_reason") == "rotated"
    # A new sign-in is a new session
    other = body_login(client, t)["refresh_token"]
    assert row(other, "session_id") != session_id


def test_idle_timeout_refuses_refresh_and_clears_cookies(client):
    t = register_tenant(client, "sl2")
    c = browser()
    r = c.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}, headers=COOKIE_MODE)
    assert r.status_code == 200, r.text
    token = c.cookies.get("refresh_token")
    shift(token, "last_activity_at", 31)

    # Background polling (no activity header) after 31 idle minutes: signed out
    r = c.post("/api/auth/refresh", headers=csrf(c))
    assert r.status_code == 401
    assert r.json()["detail"] == {"code": "SESSION_IDLE_TIMEOUT",
                                  "message": "Signed out after 30 minutes of inactivity"}
    assert {"access_token", "refresh_token", "csrf_token"} <= cleared(r)
    assert row(token, "revoked_reason") == "idle"

    # An interaction reported as older than the idle window does not rescue the session
    tokens = body_login(client, t)
    shift(tokens["refresh_token"], "last_activity_at", 40)
    r = refresh(client, tokens["refresh_token"], {ACTIVITY: str(31 * 60)})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "SESSION_IDLE_TIMEOUT"


def test_absolute_cap_refuses_even_an_active_session(client):
    t = register_tenant(client, "sl3")
    token = body_login(client, t)["refresh_token"]
    shift(token, "session_started_at", 12 * 60 + 1)
    r = refresh(client, token, {ACTIVITY: "0"})
    assert r.status_code == 401
    assert r.json()["detail"] == {"code": "SESSION_EXPIRED", "message": "Session expired, please sign in again"}
    assert row(token, "revoked_reason") == "absolute"

    # Just inside the cap, an active session keeps going; the new token expires with the session
    token = body_login(client, t)["refresh_token"]
    shift(token, "session_started_at", 11 * 60)
    r = refresh(client, token, {ACTIVITY: "0"})
    assert r.status_code == 200, r.text
    expires = row(r.json()["refresh_token"], "expires_at")
    assert expires <= datetime.utcnow() + timedelta(hours=1, minutes=1)


def test_only_the_activity_header_advances_last_activity(client):
    t = register_tenant(client, "sl4")
    token = body_login(client, t)["refresh_token"]
    shift(token, "last_activity_at", 20)
    before = row(token, "last_activity_at")

    # Without the header (background polling) the idle clock keeps running
    r = refresh(client, token)
    assert r.status_code == 200, r.text
    token = r.json()["refresh_token"]
    assert row(token, "last_activity_at") == before

    # Malformed header: ignored
    r = refresh(client, token, {ACTIVITY: "yes"})
    assert r.status_code == 200, r.text
    token = r.json()["refresh_token"]
    assert row(token, "last_activity_at") == before

    # With the header: last activity = now - reported seconds
    r = refresh(client, token, {ACTIVITY: "5"})
    assert r.status_code == 200, r.text
    token = r.json()["refresh_token"]
    after = row(token, "last_activity_at")
    assert after > before + timedelta(minutes=19)
    assert abs((datetime.utcnow() - timedelta(seconds=5) - after).total_seconds()) < 60

    # 25 minutes later polling still works (inside the window); 31 minutes later it does not
    shift(token, "last_activity_at", 25)
    r = refresh(client, token)
    assert r.status_code == 200, r.text
    token = r.json()["refresh_token"]
    shift(token, "last_activity_at", 31)
    assert refresh(client, token).status_code == 401


def test_reuse_of_a_rotated_token_revokes_the_session(client):
    t = register_tenant(client, "sl5")
    other = body_login(client, t)["refresh_token"]  # another device of the same user
    first = body_login(client, t)["refresh_token"]
    second = refresh(client, first).json()["refresh_token"]
    third = refresh(client, second).json()["refresh_token"]
    session_id = row(first, "session_id")

    # Replayed after the grace window (e.g. by a thief who copied it): the whole session is revoked
    shift(first, "revoked_at", 5)
    r = refresh(client, first)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "REFRESH_TOKEN_REUSED"
    assert row(third, "revoked_reason") == "reuse"
    assert int(sql("SELECT COUNT(*) FROM refresh_tokens WHERE session_id = :s AND revoked_at IS NULL",
                   s=session_id)) == 0
    assert refresh(client, third).status_code == 401
    # The user's other session is untouched
    assert refresh(client, other).status_code == 200


def test_concurrent_refresh_within_grace_is_not_reuse(client):
    t = register_tenant(client, "sl6")
    c = browser()
    r = c.post("/api/auth/login", json={"email": t["email"], "password": t["password"]}, headers=COOKIE_MODE)
    assert r.status_code == 200, r.text
    old = c.cookies.get("refresh_token")
    old_csrf = c.cookies.get("csrf_token")
    assert c.post("/api/auth/refresh", headers=csrf(c)).status_code == 200
    new = c.cookies.get("refresh_token")

    # A second tab still sending the old cookie a moment later: refused, nothing revoked, cookies kept
    tab = browser()
    tab.cookies.set("refresh_token", old)
    tab.cookies.set("csrf_token", old_csrf)
    r = tab.post("/api/auth/refresh", headers={"X-CSRF-Token": old_csrf, **COOKIE_MODE})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "REFRESH_TOKEN_SUPERSEDED"
    assert not r.headers.get_list("set-cookie")
    assert row(new, "revoked_at") is None
    assert c.post("/api/auth/refresh", headers=csrf(c)).status_code == 200


def test_logout_marks_the_reason_and_replay_is_refused(client):
    t = register_tenant(client, "sl7")
    token = body_login(client, t)["refresh_token"]
    assert client.post("/api/auth/logout", json={"refresh_token": token}).status_code == 204
    assert row(token, "revoked_reason") == "logout"
    r = refresh(client, token)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "REFRESH_TOKEN_REUSED"
