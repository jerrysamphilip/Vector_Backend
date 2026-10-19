"""Review fixes: literal LIKE search, custom-report filter validation, Google account linking,
version exposure on / and /health (I-12, I-17)."""
import google.oauth2.id_token as google_id_token

from app.core.config import settings
from tests.conftest import RUN, register_tenant, sql


def _names(r):
    assert r.status_code == 200, r.text
    return sorted(a["name"] for a in r.json()["items"])


def test_company_search_matches_percent_and_underscore_literally(client, tenant_a):
    h = tenant_a["headers"]
    tag = f"lk{RUN}"
    for name in (f"{tag} 100% Pure", f"{tag} 1000 Pure", f"{tag} a_b", f"{tag} axb"):
        assert client.post("/accounts", headers=h, json={"name": name}).status_code == 201
    assert _names(client.get("/accounts", headers=h, params={"q": f"{tag} 100%"})) == [f"{tag} 100% Pure"]
    assert _names(client.get("/accounts", headers=h, params={"q": f"{tag} a_b"})) == [f"{tag} a_b"]
    assert all("%" in n for n in _names(client.get("/accounts", headers=h, params={"q": "%"})))  # '%' alone is literal


def _report(client, headers, filters):
    return client.post("/reports/custom/run", headers=headers,
                       json={"object": "deals", "group_by": "stage", "filters": filters})


def test_custom_report_filter_values_are_validated(client, tenant_a):
    h = tenant_a["headers"]
    assert _report(client, h, [{"field": "status", "operator": "is", "value": "OPEN"}]).status_code == 200
    assert _report(client, h, [{"field": "status", "operator": "is", "value": ["OPEN", "WON"]}]).status_code == 200
    r = _report(client, h, [{"field": "status", "operator": "is", "value": {"$gt": ""}}])
    assert r.status_code == 422 and "single text" in r.json()["detail"]
    r = _report(client, h, [{"field": "status", "operator": "contains", "value": ["OPEN"]}])
    assert r.status_code == 422 and "list of values" in r.json()["detail"]
    assert _report(client, h, [{"field": {"x": 1}, "operator": "is", "value": "x"}]).status_code == 400
    assert _report(client, h, [{"field": "status", "operator": "contains", "value": "%"}]).status_code == 200


# ── Google sign-in linking (I-12) ──

def _google(monkeypatch, sub, email):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "unit-client")
    monkeypatch.setattr(google_id_token, "verify_oauth2_token", lambda *a, **k: {
        "sub": sub, "email": email, "email_verified": True, "given_name": "G", "family_name": "U"})


def test_google_does_not_auto_link_existing_password_account(client, tenant_a, monkeypatch):
    _google(monkeypatch, f"g-{RUN}-1", tenant_a["email"])
    monkeypatch.setattr(settings, "SSO_LINK_ALLOWED_DOMAINS", "")
    r = client.post("/api/auth/google/callback", json={"credential": "x"})
    assert r.status_code == 409 and "password" in r.json()["detail"]
    assert sql("SELECT google_id FROM users WHERE email = :e", e=tenant_a["email"]) is None


def test_google_links_existing_account_for_allowed_domain_and_audits(client, monkeypatch):
    tenant_b = register_tenant(client, "glink")
    domain = tenant_b["email"].split("@")[1]
    _google(monkeypatch, f"g-{RUN}-2", tenant_b["email"])
    monkeypatch.setattr(settings, "SSO_LINK_ALLOWED_DOMAINS", f"other.example, {domain.upper()}")
    r = client.post("/api/auth/google/callback", json={"credential": "x"})
    assert r.status_code == 200, r.text
    assert sql("SELECT google_id FROM users WHERE email = :e", e=tenant_b["email"]) == f"g-{RUN}-2"
    assert sql("SELECT COUNT(*) FROM audit_logs WHERE tenant_id = :t AND action = 'LINK_GOOGLE_SSO'",
               t=tenant_b["tenant_id"]) == 1
    # a different Google identity for the same email is never re-linked
    _google(monkeypatch, f"g-{RUN}-3", tenant_b["email"])
    assert client.post("/api/auth/google/callback", json={"credential": "x"}).status_code == 409


def test_google_new_user_needs_self_signup(client, monkeypatch):
    _google(monkeypatch, f"g-{RUN}-4", f"new-{RUN}@gnew{RUN}.example.com")
    monkeypatch.setattr(settings, "ALLOW_SELF_SIGNUP", False)
    assert client.post("/api/auth/google/callback", json={"credential": "x"}).status_code == 403


# ── Version exposure (I-17) ──

def test_health_and_root_show_version_outside_production(client):
    assert client.get("/health").json()["version"] == settings.APP_VERSION
    root = client.get("/").json()
    assert root["version"] == settings.APP_VERSION and root["docs"] == "/docs"


def test_health_and_root_hide_version_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert "version" not in client.get("/health").json()
    assert "version" not in client.get("/").json()
