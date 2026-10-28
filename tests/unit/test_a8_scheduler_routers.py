"""A8 route layer: the scheduler router with a fake scheduler and users injected through dependency_overrides."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.errors import register_domain_error_handler
from app.routers import email_scheduler_router as router_mod

ADMIN = SimpleNamespace(user_id="pa", tenant_id=None, role="PLATFORM_ADMIN", email="ops@platform.example.com")
AGENT = SimpleNamespace(user_id="ag", tenant_id="t1", role="AGENT", email="agent@x.example.com")


class FakeScheduler:
    batch_size = 123

    def __init__(self):
        self.calls = []

    async def process_scheduled_emails(self, db=None):
        self.calls.append(db)
        return {"processed": 1, "sent": 1, "next_run_in": 2.0}


@pytest.fixture
def build(monkeypatch):
    flags = {}
    monkeypatch.setattr(router_mod.platform_flags, "get", lambda name: flags.get(name))
    monkeypatch.setattr(router_mod.platform_flags, "scheduler_paused", lambda: bool(flags.get("scheduler_paused")))
    monkeypatch.setattr(router_mod.platform_flags, "set_flag", lambda name, value: flags.__setitem__(name, value))

    def make(user):
        app = FastAPI()
        register_domain_error_handler(app)
        app.include_router(router_mod.router)
        fake = FakeScheduler()
        app.dependency_overrides[router_mod.platform_admin] = lambda: user
        app.dependency_overrides[router_mod.scheduler_viewer] = lambda: user
        app.dependency_overrides[router_mod.scheduler_service] = lambda: fake
        app.dependency_overrides[get_db] = lambda: "db-session"
        return TestClient(app), fake, flags
    return make


def test_a8_process_sync_runs_one_pass_in_the_request_session(build):
    client, fake, _ = build(ADMIN)
    r = client.post("/scheduler/process/sync")
    assert r.status_code == 200 and r.json() == {"status": "completed",
                                                 "stats": {"processed": 1, "sent": 1, "next_run_in": 2.0}}
    assert fake.calls == ["db-session"]


def test_a8_status_shows_the_pause_owner_to_platform_admins_only(build):
    client, _, flags = build(ADMIN)
    flags[router_mod.platform_flags.SCHEDULER_PAUSED] = "ops@platform.example.com"
    assert client.get("/scheduler/status").json() == {"running": False, "batch_size": 123,
                                                      "paused_by": "ops@platform.example.com"}
    client, _, _ = build(AGENT)  # same shared flag: still paused, but not who paused it
    assert client.get("/scheduler/status").json() == {"running": False, "batch_size": 123}


def test_a8_stop_and_start_flip_the_shared_flag(build):
    client, _, flags = build(ADMIN)
    assert client.post("/scheduler/stop").json()["status"] == "stopped"
    assert flags[router_mod.platform_flags.SCHEDULER_PAUSED] == "ops@platform.example.com"
    assert client.post("/scheduler/stop").json()["status"] == "not_running"
    assert client.post("/scheduler/start").json() == {"status": "started"}
    assert client.post("/scheduler/start").json()["status"] == "already_running"


def test_a8_scheduler_routes_need_a_signed_in_user():
    app = FastAPI()
    app.include_router(router_mod.router)
    assert TestClient(app).get("/scheduler/status").status_code in (401, 403)
