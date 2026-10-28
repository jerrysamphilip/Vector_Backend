"""lrest route layer: tracking, subject A/B tests, account connections, company profiles and AI email
routers with fake services through ``app.dependency_overrides``. Asserts status, body and the arguments
each handler passes down (one service call per route)."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import NotFoundError, PermissionDenied, register_domain_error_handler
from app.routers import (
    ai_email_router, company_profile_router, connections_router, subject_variant_router, tracking_router,
)
from app.services.ai_generation_service import AiGenerationService
from app.services.connection_service import ConnectionService

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="ADMIN", email="me@x.io")


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"ok": True} if result is None else result
        self.error = error

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return self.result
        return call


PROVIDERS = (tracking_router.tracking_service, subject_variant_router.subject_variant_service,
             connections_router.connection_service, company_profile_router.company_profile_service,
             ai_email_router.ai_generation_service)


@pytest.fixture
def make_client():
    def build(fake):
        app = FastAPI()
        register_domain_error_handler(app)
        for module in (tracking_router, subject_variant_router, connections_router, company_profile_router,
                       ai_email_router):
            app.include_router(module.router)
            for route in module.router.routes:
                for dep in route.dependant.dependencies:
                    if getattr(dep.call, "__name__", "") in ("_role_checker", "_checker"):
                        app.dependency_overrides[dep.call] = lambda: ME
        for provider in PROVIDERS:
            app.dependency_overrides[provider] = lambda: fake
        return TestClient(app, follow_redirects=False)
    return build


# ── tracking ────────────────────────────────────────────────────────

def test_open_pixel_always_served(make_client):
    fake = Recorder()
    r = make_client(fake).get("/tracking/open/m1.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/gif"
    assert fake.calls == [("record_open", ("m1",), {})]


def test_click_redirects_when_the_service_allows(make_client):
    fake = Recorder(True)
    r = make_client(fake).get("/tracking/click/m1", params={"url": "https://ok.io/a&amp;b", "sig": "s"})
    assert r.status_code == 302 and r.headers["location"] == "https://ok.io/a&b"
    assert fake.calls == [("record_click", ("m1", "https://ok.io/a&amp;b", "https://ok.io/a&b", "s"), {})]


def test_click_unrecognised_shows_leaving_page_and_bad_scheme_is_400(make_client):
    fake = Recorder(False)
    client = make_client(fake)
    r = client.get("/tracking/click/m1", params={"url": "https://evil.io/x"})
    assert r.status_code == 200 and "You are leaving to evil.io" in r.text
    r = client.get("/tracking/click/m1", params={"url": "javascript:alert(1)"})
    assert r.status_code == 400 and len(fake.calls) == 1


def test_unsubscribe_page_and_post(make_client):
    client = make_client(Recorder(True))
    assert client.get("/tracking/unsubscribe/m1").status_code == 200
    r = client.post("/tracking/unsubscribe/m1", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and "Unsubscribed successfully" in r.text
    missing = make_client(Recorder(False))
    assert missing.get("/tracking/unsubscribe/x").status_code == 404
    assert missing.post("/tracking/unsubscribe/x", data={"confirm": "1"}).status_code == 404
    broken = make_client(Recorder(error=RuntimeError("db")))
    assert broken.get("/tracking/unsubscribe/x").status_code == 500
    assert broken.post("/tracking/unsubscribe/x", data={}).status_code == 500


def test_unsubscribe_post_passes_one_click_flag(make_client):
    fake = Recorder(True)
    client = make_client(fake)
    client.post("/tracking/unsubscribe/m1", data={"List-Unsubscribe": "One-Click"})
    client.post("/tracking/unsubscribe/m1", data={"confirm": "1"})
    assert [c[1] for c in fake.calls] == [("m1", True), ("m1", False)]


# ── subject A/B tests ───────────────────────────────────────────────

BASE = "/campaigns/c1/sequences/2/subject-variants"


def test_subject_variant_routes_pass_down(make_client):
    fake = Recorder({"variant_id": "v"})
    client = make_client(fake)
    assert client.get(BASE).status_code == 200
    assert client.post(BASE, json={"subject": "  Hi  ", "weight": 3}).status_code == 201
    assert client.get(BASE + "/stats").status_code == 200
    assert client.get(BASE + "/messages", params={"label": "b", "limit": 5, "offset": 1}).status_code == 200
    assert client.patch(BASE + "/A", json={"weight": 2}).status_code == 200
    assert client.delete(BASE + "/A").status_code == 204
    assert client.post(BASE + "/A/promote").status_code == 200
    assert fake.calls == [
        ("listing", ("c1", 2, ME), {}),
        ("create", ("c1", 2, "Hi", 3, ME), {}),
        ("stats", ("c1", 2, ME), {}),
        ("messages", ("c1", 2, "b", 5, 1, ME), {}),
        ("update", ("c1", 2, "A", None, 2, ME), {}),
        ("delete", ("c1", 2, "A", ME), {}),
        ("promote", ("c1", 2, "A", ME), {}),
    ]


def test_subject_variant_validation_and_errors(make_client):
    client = make_client(Recorder(error=NotFoundError("Campaign not found")))
    assert client.get("/campaigns/c1/sequences/9/subject-variants").status_code == 422
    assert client.patch(BASE + "/E", json={"weight": 1}).status_code == 422
    assert client.post(BASE, json={"subject": "   "}).status_code == 422
    r = client.get(BASE)
    assert r.status_code == 404 and r.json() == {"detail": "Campaign not found"}


# ── connections ─────────────────────────────────────────────────────

def test_connection_routes(make_client, monkeypatch):
    fake = Recorder({"connections": []})
    client = make_client(fake)
    assert client.get("/connections").json() == {"connections": []}
    assert client.patch("/connections/k1", json={"sync_email": False}).status_code == 200
    assert client.post("/connections/k1/sync").status_code == 200
    assert client.delete("/connections/k1").status_code == 200
    assert fake.calls == [("mine", (ME,), {}), ("update", ("k1", {"sync_email": False}, ME), {}),
                          ("sync_now", ("k1", ME), {}), ("disconnect", ("k1", ME), {})]


def test_connection_start_sets_the_binding_cookie(make_client, monkeypatch):
    monkeypatch.setattr(connections_router.ConnectionService, "start",
                        staticmethod(lambda provider, user, issue: {"authorize_url": f"{provider}:{issue()}"}))
    r = make_client(Recorder()).post("/connections/google/start")
    assert r.status_code == 200 and r.json()["authorize_url"].startswith("google:")
    assert "oauth_nonce" in r.headers.get("set-cookie", "")


def test_connection_callback_redirects_with_the_outcome(make_client, monkeypatch):
    fake = Recorder({"sync": "connected", "provider": "google"})
    monkeypatch.setattr(connections_router.settings, "CONNECTIONS_POST_CONNECT_URL", "/app/settings?tab=sync", raising=False)
    r = make_client(fake).get("/connections/google/callback", params={"state": "s", "code": "c"})
    assert r.status_code == 302 and r.headers["location"] == "/app/settings?tab=sync&sync=connected&provider=google"
    name, args, _ = fake.calls[0]
    assert name == "complete" and args[:5] == ("google", "s", "c", "", "")
    assert args[5]("any-nonce") is False  # no cookie on this request


def test_connection_service_start_rules(monkeypatch):
    with pytest.raises(NotFoundError):
        ConnectionService.start("yahoo", ME, lambda: "n")
    from app.services import connection_service as cs
    monkeypatch.setattr(cs.account_sync, "configured", lambda p: False)
    from app.core.errors import BadRequestError
    with pytest.raises(BadRequestError) as e:
        ConnectionService.start("microsoft", ME, lambda: "n")
    assert "MS365_CLIENT_ID" in e.value.message


# ── company profiles ────────────────────────────────────────────────

PROFILE = SimpleNamespace(profile_id="p1", profile_name="Main", company_name="Acme", company_description=None,
                          default_sender_name=None, default_cta_link=None, postal_address="1 Road", is_default=True)


def test_company_profile_routes(make_client):
    fake = Recorder(PROFILE)
    client = make_client(fake)
    assert client.get("/api/company-profiles/p1").json()["company_name"] == "Acme"
    assert client.post("/api/company-profiles", json={"profile_name": "B", "company_name": "B"}).status_code == 200
    assert client.put("/api/company-profiles/p1", json={"company_name": "C"}).status_code == 200
    names = [c[0] for c in fake.calls]
    assert names == ["get", "create", "update"]
    listing = make_client(Recorder([PROFILE])).get("/api/company-profiles")
    assert listing.json()[0]["profile_id"] == "p1"
    deleted = make_client(Recorder({"message": "Company profile deleted", "profile_id": "p1"}))
    assert deleted.delete("/api/company-profiles/p1").json()["profile_id"] == "p1"


def test_sender_identity_routes(make_client):
    from app.services.company_profile_service import SenderIdentitySettings
    fake = Recorder(SenderIdentitySettings("Acme", "1 Road, Town", "p1", True))
    client = make_client(fake)
    assert client.get("/api/company-profiles/sender-identity").json() == {
        "company_name": "Acme", "postal_address": "1 Road, Town", "profile_id": "p1", "is_complete": True}
    r = client.put("/api/company-profiles/sender-identity", json={"company_name": "Acme", "postal_address": "1 Road, Town"})
    assert r.status_code == 200 and fake.calls[-1] == ("set_sender_identity", ("Acme", "1 Road, Town", ME), {})
    assert client.put("/api/company-profiles/sender-identity", json={"company_name": "", "postal_address": "x"}).status_code == 422


def test_company_profile_not_found(make_client):
    r = make_client(Recorder(error=NotFoundError("Company profile not found"))).get("/api/company-profiles/zz")
    assert r.status_code == 404 and r.json() == {"detail": "Company profile not found"}


# ── AI email ────────────────────────────────────────────────────────

def test_ai_routes_without_database(make_client):
    client = make_client(Recorder())
    r = client.post("/prospects/classifications/test", json={"designation": "VP Market Access"})
    assert r.status_code == 200 and r.json()["designation"] == "VP Market Access"
    r = client.post("/emails/validations", json={"email": "a@gmail.com"})
    assert r.json() == {"email": "a@gmail.com", "is_business": False, "domain": "gmail.com"}
    assert client.get("/emails/cta-options/intro").json()["purpose"] == "intro"
    assert client.get("/emails/cta-options/other").status_code == 400
    assert all(p["persona_type"] != "OTHER" for p in client.get("/personas").json())
    assert "spam_score" in client.post("/emails/compliance-checks", json={"subject": "Hi", "body": "Hello"}).json()


def test_ai_routes_with_the_service(make_client):
    fake = Recorder([])
    client = make_client(fake)
    assert client.post("/prospects/classifications/batch", json={"prospect_ids": ["p1"]}).json() == []
    assert client.get("/persona-blueprints", params={"active_only": "false"}).json() == []
    assert fake.calls == [("classify_batch", (["p1"], ME), {}), ("blueprints", (False,), {})]
    gen = Recorder({"subject": "s", "body": "b", "persona_type": "OTHER", "opener_used": "o", "cta_used": "c",
                    "tone": "t"})
    client = make_client(gen)
    r = client.post("/emails/generations", json={"prospect_id": "p1", "product_name": "X", "cta_link": "x.io"})
    assert r.status_code == 200 and gen.calls[0] == ("generate_email", ("p1", "X", "x.io", ME), {})
    r = client.post("/emails/sequences/generations", json={"prospect_id": "p1", "product_name": "X"})
    assert r.status_code == 200 and gen.calls[1][1] == ("p1", "X", "", True, "", ME)


def test_ai_route_errors_keep_their_status(make_client):
    client = make_client(Recorder(error=NotFoundError("Prospect p9 not found")))
    r = client.post("/emails/generations", json={"prospect_id": "p9"})
    assert r.status_code == 404 and r.json() == {"detail": "Prospect p9 not found"}
    assert client.get("/persona-blueprints/x").status_code == 404
    boom = make_client(Recorder(error=RuntimeError("secret sql")))
    r = boom.get("/persona-blueprints")
    assert r.status_code == 500 and "secret" not in r.text
    denied = make_client(Recorder(error=PermissionDenied("no")))
    assert denied.post("/persona-blueprints", json={"persona_type": "X", "openers": [], "value_angles": [],
                                                    "ctas": [], "proof_points": [], "tone_rules": {}}).status_code == 403


def test_ai_static_helpers():
    assert AiGenerationService.cta_options("reminder")["options"]
    assert AiGenerationService.validate_email("nope")["domain"] == ""
