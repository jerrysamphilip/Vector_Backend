"""A7 route layer: imports, automation rules, webhooks and contact scoring routers with fake services injected
through ``app.dependency_overrides``. Asserts status codes, bodies and what each handler passes down."""
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import ConflictError, NotFoundError, register_domain_error_handler
from app.domain.imports import ImportLimits
from app.routers import automation_router, contact_scoring_router, import_router, webhooks_router
from app.routers.contacts_router import tenant_user as contacts_tenant_user

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="SUPER_ADMIN", custom_permissions=None, sales_level=None,
                     email="me@x.example")


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"ok": True} if result is None else result
        self.error = error
        self.limits = ImportLimits(max_bytes=1024)

    def require_manager(self, user):
        self.calls.append(("require_manager", (user,), {}))

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return self.result
        return call


@pytest.fixture
def client_with():
    def build(provider, fake, router, user_deps):
        app = FastAPI()
        register_domain_error_handler(app)
        app.include_router(router.router)
        for dep in user_deps:
            app.dependency_overrides[dep] = lambda: ME
        app.dependency_overrides[provider] = lambda: fake
        return TestClient(app)
    return build


# ── Imports ──

def _imports(client_with, fake):
    return client_with(import_router.imports_service, fake, import_router, [import_router.tenant_user])


def test_import_preview_passes_the_file_and_sheet(client_with):
    fake = Recorder({"headers": ["Email"]})
    r = _imports(client_with, fake).post("/imports/preview", files={"file": ("a.csv", io.BytesIO(b"Email\n"))},
                                         data={"sheet": "Leads"})
    assert r.status_code == 200 and r.json() == {"headers": ["Email"]}
    name, args, _ = fake.calls[-1]
    assert name == "preview" and args[0] is ME and args[1].name == "a.csv" and args[1].data == b"Email\n"
    assert args[2] == "Leads"


def test_upload_reads_only_just_past_the_limit(client_with):
    fake = Recorder({"import": {"import_id": "i1"}})
    r = _imports(client_with, fake).post("/imports/uploads", files={"file": ("a.csv", io.BytesIO(b"x" * 5000))})
    assert r.status_code == 201
    assert len(fake.calls[-1][1][1].data) == 1024 + import_router._UPLOAD_SLACK


def test_sync_import_201_or_202(client_with, monkeypatch):
    fake = Recorder((201, {"status": "COMPLETED"}))
    client = _imports(client_with, fake)
    monkeypatch.setattr(import_router, "CHUNK", 7)
    r = client.post("/imports", files={"file": ("a.csv", io.BytesIO(b"Email\n"))}, data={"mapping": "{}"})
    assert r.status_code == 201 and r.json() == {"status": "COMPLETED"}
    assert fake.calls[-1][0] == "run_sync" and fake.calls[-1][2] == {"chunk_size": 7}
    fake.result = (202, {"status": "QUEUED"})
    r = client.post("/imports", files={"file": ("a.csv", io.BytesIO(b"Email\n"))}, data={"mapping": "{}"})
    assert r.status_code == 202 and r.json() == {"status": "QUEUED"}


def test_job_routes_call_one_service_method_each(client_with):
    fake = Recorder({"import_id": "i1"})
    client = _imports(client_with, fake)
    body = {"mapping": {"Email": "contact.email"}, "options": {"mode": "CREATE"}}
    assert client.post("/imports/i1/dry-run", json=body).status_code == 200
    assert fake.calls[-1] == ("dry_run", (ME, "i1", {"Email": "contact.email"}, {"mode": "CREATE"}), {})
    assert client.post("/imports/i1/start", json=body).status_code == 202
    assert fake.calls[-1][0] == "start"
    assert client.post("/imports/i1/cancel").status_code == 200 and fake.calls[-1][0] == "cancel"
    assert client.post("/imports/i1/resume").status_code == 202 and fake.calls[-1][0] == "resume"
    assert client.get("/imports/i1").status_code == 200 and fake.calls[-1] == ("detail", (ME, "i1"), {})
    assert client.get("/imports", params={"page": 2}).status_code == 200
    assert fake.calls[-1] == ("history", (ME, 2, 25), {})


def test_mappings_routes_are_not_read_as_import_ids(client_with):
    fake = Recorder([{"name": "Trade show"}])
    client = _imports(client_with, fake)
    assert client.get("/imports/mappings").json() == [{"name": "Trade show"}] and fake.calls[-1][0] == "mappings"
    r = client.post("/imports/mappings", json={"name": "N", "mapping": {"E": "contact.email"}})
    assert r.status_code == 201 and fake.calls[-1] == ("save_mapping", (ME, "N", {"E": "contact.email"}, None), {})
    assert client.delete("/imports/mappings/m1").status_code == 200 and fake.calls[-1][1] == (ME, "m1")


def test_domain_errors_become_http_errors(client_with):
    client = _imports(client_with, Recorder(error=ConflictError("This import is completed")))
    r = client.post("/imports/i1/start", json={"mapping": {}})
    assert r.status_code == 409 and r.json() == {"detail": "This import is completed"}
    client = _imports(client_with, Recorder(error=NotFoundError("Import not found")))
    assert client.get("/imports/i1").status_code == 404


def test_errors_csv_is_an_attachment(client_with):
    fake = Recorder(("people-errors.csv", b"\xef\xbb\xbfRow,Error\n"))
    r = _imports(client_with, fake).get("/imports/i1/errors.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "people-errors.csv" in r.headers["content-disposition"]


# ── Automation rules ──

@pytest.fixture
def rules_client(client_with):
    def build(fake):
        app = FastAPI()
        register_domain_error_handler(app)
        app.include_router(automation_router.router)
        for route in automation_router.router.routes:
            for dep in route.dependant.dependencies:  # the role / permission checks
                app.dependency_overrides[dep.call] = lambda: ME
        app.dependency_overrides[automation_router.rules_service] = lambda: fake
        return TestClient(app)
    return build


RULE = {"rule_id": "r1", "campaign_id": "c1", "name": "Bounce", "description": None, "trigger_type": "EMAIL_BOUNCED",
        "trigger_config": {}, "action_type": "PAUSE_PROSPECT", "action_config": {}, "is_active": True,
        "created_at": "2026-10-27T00:00:00", "updated_at": "2026-10-27T00:00:00"}


def test_rule_create_accepts_the_bounce_trigger(rules_client):
    fake = Recorder(SimpleNamespace(**RULE))
    r = rules_client(fake).post("/automation-rules/", json={
        "campaign_id": "c1", "name": "Bounce", "trigger_type": "EMAIL_BOUNCED", "action_type": "PAUSE_PROSPECT"})
    assert r.status_code == 201, r.text
    assert r.json()["trigger_type"] == "EMAIL_BOUNCED"
    name, args, _ = fake.calls[-1]
    assert name == "create" and args[1].trigger_type.value == "EMAIL_BOUNCED"


def test_rule_list_update_delete(rules_client):
    fake = Recorder([SimpleNamespace(**RULE)])
    client = rules_client(fake)
    assert client.get("/automation-rules/", params={"campaign_id": "c1"}).json()[0]["rule_id"] == "r1"
    assert fake.calls[-1] == ("list", (ME, "c1"), {})
    fake.result = SimpleNamespace(**RULE)
    assert client.put("/automation-rules/r1", json={"description": "x"}).status_code == 200
    assert fake.calls[-1][0] == "update" and fake.calls[-1][1][1] == "r1"
    assert client.delete("/automation-rules/r1").status_code == 204 and fake.calls[-1] == ("delete", (ME, "r1"), {})
    fake.error = NotFoundError("Rule not found")
    assert client.delete("/automation-rules/r1").status_code == 404


# ── Webhooks ──

def test_webhook_routes(client_with):
    fake = Recorder({"items": []})
    client = client_with(webhooks_router.webhook_service, fake, webhooks_router, [webhooks_router.admin_user])
    assert client.get("/integrations/webhooks").json() == {"items": []} and fake.calls[-1] == ("list", (ME,), {})
    r = client.post("/integrations/webhooks", json={"url": "https://x.example/h", "event_types": ["contact.created"]})
    assert r.status_code == 201
    assert fake.calls[-1] == ("create", (ME, "https://x.example/h", None, ["contact.created"], True), {})
    client.patch("/integrations/webhooks/e1", json={"active": False})
    assert fake.calls[-1] == ("update", (ME, "e1", {"active": False}), {})
    client.get("/integrations/webhooks/e1/deliveries", params={"status": "failed", "limit": 5})
    assert fake.calls[-1] == ("deliveries", (ME, "e1", "failed", None, 5, 0), {})
    assert client.post("/integrations/webhooks/e1/deliveries/d1/redeliver").status_code == 201
    assert client.get("/integrations/webhooks/event-types").status_code == 200


# ── Contact scoring ──

def test_scoring_routes_schedule_the_recompute(client_with):
    fake = Recorder({"status": "queued", "contacts": 3, "requested_at": None})
    client = client_with(contact_scoring_router.scoring_service, fake, contact_scoring_router,
                         [contact_scoring_router.admin_user, contacts_tenant_user])
    scheduled = []
    import app.services.contact_scoring as cs
    original = cs.recompute_tenant
    cs.recompute_tenant = lambda tenant_id: scheduled.append(tenant_id)
    try:
        r = client.post("/contacts/scoring-model/recompute")
        assert r.status_code == 202 and r.json()["contacts"] == 3 and scheduled == ["t1"]
    finally:
        cs.recompute_tenant = original
    assert client.get("/contacts/p1/score").status_code == 200 and fake.calls[-1] == ("contact_score", (ME, "p1"), {})
    assert client.post("/contacts/p1/score/recompute").status_code == 200
