"""A5: the auth, user-management and platform-admin routers with fake services (dependency_overrides):
status codes, bodies, cookies and the arguments passed down. Role / permission dependencies run for real
on an overridden current user."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.errors import register_domain_error_handler
from app.routers import auth_router, platform_admin_router, user_router
from app.services.identity_common import AuthenticationFailed
from app.services.session_limits import CODE_REUSED, CODE_SUPERSEDED, SessionRefused

TOKENS = {"access_token": "acc", "refresh_token": "ref", "token_type": "bearer", "user": {"user_id": "me"},
          "first_login": False}


def _user(role="ADMIN", **kw):
    base = dict(user_id="me", tenant_id="t1", role=role, status="ACTIVE", custom_permissions=None,
                first_name="Me", last_name="Too", email="me@x.com", auth_provider="local", email_verified=True,
                avatar_url=None, sales_level=None, manager_id=None, mfa_enabled=False, mfa_recovery_codes=None,
                tenant=None, created_at=None, last_login_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


class Recorder:
    """Fake service: records every call; returns ``results[name]`` (or ``default``), or raises ``error``."""

    def __init__(self, default=None, error=None, **results):
        self.calls = []
        self.results = results
        self.default = {"ok": True} if default is None else default
        self.error = error

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if self.error:
                raise self.error
            return self.results.get(name, self.default)
        return call


@pytest.fixture
def make_client():
    def build(user=None, **fakes):
        app = FastAPI()
        register_domain_error_handler(app)
        for r in (auth_router, user_router, platform_admin_router):
            app.include_router(r.router)
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: user
        providers = {"auth": auth_router.auth_service, "mfa": auth_router.mfa_service,
                     "team": user_router.team_service, "invitations": user_router.invitation_service,
                     "platform": platform_admin_router.platform_service}
        for key, fake in fakes.items():
            app.dependency_overrides[providers[key]] = (lambda f: (lambda: f))(fake)
        return TestClient(app)
    return build


# ── /api/auth ──

def test_login_returns_tokens_in_the_body_and_passes_a_throttle(make_client):
    fake = Recorder(login=dict(TOKENS))
    r = make_client(auth=fake).post("/api/auth/login", json={"email": "a@x.com", "password": "pw"})
    assert r.status_code == 200 and r.json()["access_token"] == "acc"
    name, (email, password, throttle) = fake.calls[0]
    assert (name, email, password) == ("login", "a@x.com", "pw") and hasattr(throttle, "failed")


def test_login_in_cookie_mode_moves_tokens_into_cookies(make_client):
    fake = Recorder(login=dict(TOKENS))
    r = make_client(auth=fake).post("/api/auth/login", json={"email": "a@x.com", "password": "pw"},
                                    headers={"X-Auth-Mode": "cookie"})
    assert r.status_code == 200 and "access_token" not in r.json() and r.json()["user"] == {"user_id": "me"}
    assert {"access_token", "refresh_token", "csrf_token"} <= set(r.cookies.keys())


def test_mfa_required_result_is_not_delivered_as_cookies(make_client):
    fake = Recorder(login={"mfa_required": True, "mfa_token": "m", "first_login": False})
    r = make_client(auth=fake).post("/api/auth/login", json={"email": "a@x.com", "password": "pw"},
                                    headers={"X-Auth-Mode": "cookie"})
    assert r.json() == {"mfa_required": True, "mfa_token": "m", "first_login": False} and not r.cookies


def test_domain_errors_keep_the_http_body(make_client):
    fake = Recorder(error=AuthenticationFailed("Invalid email or password"))
    r = make_client(auth=fake).post("/api/auth/login", json={"email": "a@x.com", "password": "pw"})
    assert (r.status_code, r.json()) == (401, {"detail": "Invalid email or password"})


def test_refresh_refusal_clears_cookies_except_for_the_two_tab_race(make_client):
    fake = Recorder(error=SessionRefused(CODE_REUSED, "Session revoked, please sign in again"))
    r = make_client(auth=fake).post("/api/auth/refresh", json={"refresh_token": "r"},
                                    headers={"X-User-Activity": "5"})
    assert r.status_code == 401 and r.json()["detail"]["code"] == CODE_REUSED
    assert "refresh_token=" in r.headers.get("set-cookie", "")
    assert fake.calls == [("refresh", ("r", "5"))]
    fake = Recorder(error=SessionRefused(CODE_SUPERSEDED, "Refresh token already exchanged", clear_cookies=False))
    r = make_client(auth=fake).post("/api/auth/refresh", json={"refresh_token": "r"})
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_logout_passes_refresh_and_bearer_token_and_clears_cookies(make_client):
    fake = Recorder()
    r = make_client(auth=fake).post("/api/auth/logout", json={"refresh_token": "r"},
                                    headers={"Authorization": "Bearer acc-token"})
    assert r.status_code == 204 and "refresh_token=" in r.headers.get("set-cookie", "")
    assert fake.calls == [("logout", ("r", "acc-token"))]


def test_me_needs_a_user_and_reports_the_profile(make_client):
    assert make_client().get("/api/auth/me").status_code == 401
    r = make_client(user=_user("MANAGER")).get("/api/auth/me")
    assert r.status_code == 200 and r.json()["permissions"] == ["manage_campaigns", "manage_templates",
                                                                 "view_analytics"]


def test_change_password_issues_a_fresh_cookie_session_only_in_cookie_mode(make_client):
    from app.services.auth_service import PasswordChange
    me = _user()
    fake = Recorder(change_password=PasswordChange({"message": "Password changed successfully"}, dict(TOKENS)))
    r = make_client(user=me, auth=fake).put("/api/auth/me/password", json={"current_password": "a",
                                                                          "new_password": "b"},
                                            headers={"X-Auth-Mode": "cookie"})
    assert r.json() == {"message": "Password changed successfully"} and "access_token" in r.cookies
    assert fake.calls[0] == ("change_password", (me, "a", "b", True))


def test_tenant_two_factor_policy_is_super_admin_only(make_client):
    fake = Recorder(set_tenant_policy={"require_mfa": True})
    assert make_client(user=_user("ADMIN"), mfa=fake).put("/api/auth/mfa/tenant-policy",
                                                          json={"require_mfa": True}).status_code == 403
    boss = _user("SUPER_ADMIN")
    r = make_client(user=boss, mfa=fake).put("/api/auth/mfa/tenant-policy", json={"require_mfa": True})
    assert r.json() == {"require_mfa": True} and fake.calls == [("set_tenant_policy", (boss, True))]


# ── /api/users ──

def test_team_list_is_for_admins(make_client):
    member = _user("AGENT", user_id="m1")
    fake = Recorder(list_members=[member])
    assert make_client(user=_user("MANAGER"), team=fake).get("/api/users").status_code == 403
    r = make_client(user=_user("ADMIN"), team=fake).get("/api/users")
    assert r.status_code == 200 and r.json()[0]["user_id"] == "m1"
    assert r.json()[0]["permissions"] == ["manage_campaigns"]


def test_invite_needs_manage_team_and_passes_the_request(make_client):
    fake = Recorder(invite={"invitation_id": "i1"})
    restricted = _user("ADMIN", custom_permissions=["export_data"])
    r = make_client(user=restricted, invitations=fake).post("/api/users/invite", json={"email": "n@x.com"})
    assert r.status_code == 403 and not fake.calls
    admin = _user("ADMIN")
    r = make_client(user=admin, invitations=fake).post("/api/users/invite", json={"email": "n@x.com", "role": "MANAGER"})
    assert r.status_code == 201 and r.json() == {"invitation_id": "i1"}
    name, (actor, email, role, request) = fake.calls[0]
    assert (name, actor, email, role) == ("invite", admin, "n@x.com", "MANAGER") and hasattr(request, "headers")


def test_deactivate_and_role_change_bodies(make_client):
    fake = Recorder()
    admin = _user("ADMIN")
    r = make_client(user=admin, team=fake).delete("/api/users/u9")
    assert r.json() == {"message": "User deactivated", "user_id": "u9"} and fake.calls == [("deactivate", (admin, "u9"))]
    assert make_client(user=admin, team=fake).put("/api/users/u9/role", json={"role": "AGENT"}).status_code == 403
    boss = _user("SUPER_ADMIN")
    r = make_client(user=boss, team=fake).put("/api/users/u9/role", json={"role": "AGENT"})
    assert r.json() == {"message": "Role updated to AGENT", "user_id": "u9"}


def test_platform_admins_are_kept_out_of_team_routes(make_client):
    r = make_client(user=_user("PLATFORM_ADMIN", tenant_id=None), team=Recorder()).get("/api/users")
    assert (r.status_code, r.json()["detail"]) == (403, "Platform admins cannot access tenant data.")


def test_accept_invite_is_public(make_client):
    fake = Recorder(accept={"message": "Account created successfully"})
    r = make_client(invitations=fake).post("/api/users/accept-invite", json={
        "token": "t", "first_name": "A", "last_name": "B", "password": "pw-12345"})
    assert r.status_code == 201 and fake.calls == [("accept", ("t", "A", "B", "pw-12345"))]


# ── /api/admin ──

def test_platform_routes_are_platform_admin_only(make_client):
    fake = Recorder(list_tenants=[])
    r = make_client(user=_user("SUPER_ADMIN"), platform=fake).get("/api/admin/tenants")
    assert r.status_code == 403 and not fake.calls


def test_platform_tenant_list_passes_query_params(make_client):
    fake = Recorder(list_tenants=[{"tenant_id": "t1", "tenant_name": "Acme", "status": "ACTIVE", "user_count": 2,
                                   "campaign_count": 0, "created_at": None}])
    admin = _user("PLATFORM_ADMIN", tenant_id=None)
    r = make_client(user=admin, platform=fake).get("/api/admin/tenants", params={"search": "ac", "status": "active"})
    assert r.status_code == 200 and r.json()[0]["user_count"] == 2
    assert fake.calls == [("list_tenants", ("ac", "active"))]
    r = make_client(user=admin, platform=Recorder(error=AuthenticationFailed("x"))).put(
        "/api/admin/tenants/t1/status", json={"status": "SUSPENDED"})
    assert r.status_code == 401
