"""
A4 route layer (ADR 0009): inbox, conversation and deliverability routers with fake services injected
through ``app.dependency_overrides``; the signed-in user comes from an overridden ``get_current_user`` so
the real role / permission checks run. Asserts status codes, bodies and what each handler passes down.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.errors import BadRequestError, NotFoundError, register_domain_error_handler
from app.domain.inbox import ConversationQuery, ReplyLabel
from app.routers import conversation_router, deliverability_router, inbox_router
from app.services.inbox_service import MailSyncFailed

ADMIN = SimpleNamespace(user_id="u1", tenant_id="t1", role="ADMIN", custom_permissions=None, email="a@x.io")
AGENT = SimpleNamespace(user_id="u2", tenant_id="t1", role="AGENT", custom_permissions=None, email="b@x.io")


class Recorder:
    """Fake service: records every call and returns a canned result (or raises)."""

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


@pytest.fixture
def make_client():
    def build(user=ADMIN, inbox=None, conversations=None, domains=None):
        app = FastAPI()
        register_domain_error_handler(app)
        for module in (inbox_router, conversation_router, deliverability_router):
            app.include_router(module.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[inbox_router.inbox_service] = lambda: inbox or Recorder()
        app.dependency_overrides[conversation_router.conversation_service] = lambda: conversations or Recorder()
        app.dependency_overrides[deliverability_router.domain_service] = lambda: domains or Recorder()
        return TestClient(app)
    return build


# ── inboxes ───────────────────────────────────────────────────────────────────
def test_sync_passes_days_and_maps_failure_to_502(make_client):
    fake = Recorder({"status": "success"})
    assert make_client(inbox=fake).post("/inboxes/i1/sync?days=3").json() == {"status": "success"}
    name, args, _ = fake.calls[0]
    assert name == "sync_history" and args[1:] == ("i1", 3) and args[0] is ADMIN

    failing = Recorder(error=MailSyncFailed("IMAP sync failed for a@x.io."))
    r = make_client(inbox=failing).post("/inboxes/i1/sync")
    assert r.status_code == 502 and r.json() == {"detail": "IMAP sync failed for a@x.io."}


def test_mail_host_refusal_is_a_400_with_the_same_body(make_client):
    fake = Recorder(error=BadRequestError("Mail server not allowed: 10.0.0.1 is private"))
    r = make_client(inbox=fake).get("/inboxes/i1/test-imap")
    assert r.status_code == 400 and r.json() == {"detail": "Mail server not allowed: 10.0.0.1 is private"}


def test_inbox_routes_are_admin_only(make_client):
    fake = Recorder([])
    assert make_client(user=AGENT, inbox=fake).get("/inboxes").status_code == 403
    assert fake.calls == []
    assert make_client(inbox=fake).get("/inboxes").json() == []
    assert fake.calls[0][0] == "list"


def test_delete_and_missing_inbox(make_client):
    fake = Recorder(None)
    fake.result = None
    assert make_client(inbox=fake).delete("/inboxes/i9").status_code == 204
    assert fake.calls[0][:2] == ("delete", (ADMIN, "i9"))
    r = make_client(inbox=Recorder(error=NotFoundError("Inbox not found"))).get("/inboxes/i9/test-smtp")
    assert r.status_code == 405  # test-smtp is a POST
    r = make_client(inbox=Recorder(error=NotFoundError("Inbox not found"))).post("/inboxes/i9/test-smtp")
    assert r.status_code == 404 and r.json() == {"detail": "Inbox not found"}


def test_microsoft_callback_redirects_with_service_result(make_client, monkeypatch):
    from app.services import ms365_oauth, oauth_binding
    monkeypatch.setattr(ms365_oauth, "read_state", lambda state: {"inbox_id": "i1", "tenant_id": "t1", "nonce": "n"})
    monkeypatch.setattr(oauth_binding, "matches", lambda request, nonce: True)
    fake = Recorder({"ms365": "connected", "inbox": "i1"})
    r = make_client(inbox=fake).get("/inboxes/oauth/microsoft/callback?state=s&code=c", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("?ms365=connected&inbox=i1")
    assert fake.calls[0][:2] == ("finish_microsoft", ("t1", "i1", "c"))


# ── conversations ─────────────────────────────────────────────────────────────
def test_conversation_list_builds_the_query(make_client):
    fake = Recorder({"items": [], "total": 0, "page": 2, "page_size": 5, "has_more": False})
    r = make_client(conversations=fake).get("/conversations?page=2&page_size=5&search=%20acme%20&label=ooo")
    assert r.status_code == 200
    query = fake.calls[0][1][1]
    assert isinstance(query, ConversationQuery)
    assert (query.paging.page, query.paging.page_size, query.search, query.label) == (2, 5, "acme",
                                                                                        ReplyLabel.OUT_OF_OFFICE)
    assert make_client(conversations=fake).get("/conversations?label=bogus").status_code == 422


def test_conversation_thread_404(make_client):
    fake = Recorder(None)
    fake.result = None
    r = make_client(conversations=fake).get("/conversations/c1")
    assert r.status_code == 404 and r.json() == {"detail": "Conversation not found"}


# ── deliverability ────────────────────────────────────────────────────────────
def test_deliverability_routes_call_the_service(make_client):
    fake = Recorder({"status": "scan_completed", "results": {}})
    assert make_client(domains=fake).post("/deliverability/domains/acme.com/scans").status_code == 202
    assert fake.calls[0][:2] == ("scan", (ADMIN, "acme.com"))
    fake = Recorder([])
    make_client(domains=fake).get("/deliverability/integrations/metrics?provider=snds&domain_name=acme.com&limit=7")
    assert fake.calls[0][:2] == ("external_metrics", (ADMIN, "snds", "acme.com", 7))
    assert make_client(domains=fake).get(
        "/deliverability/integrations/metrics?provider=snds&domain_name=a.com&limit=0").status_code == 422


def test_deliverability_admin_routes_refuse_agents(make_client):
    fake = Recorder()
    assert make_client(user=AGENT, domains=fake).post("/deliverability/domains", json={"domain_name": "a.com"}
                                                      ).status_code == 403
    assert make_client(user=AGENT, domains=fake).delete("/deliverability/domains/a.com").status_code == 403
    assert fake.calls == []
    assert make_client(domains=fake).post("/deliverability/domains", json={"domain_name": "a.com"}).status_code == 201
    assert fake.calls[0][:2] == ("add", (ADMIN, "a.com"))


def test_unknown_domain_is_404(make_client):
    fake = Recorder(error=NotFoundError("Domain not found"))
    r = make_client(domains=fake).get("/deliverability/domains/other.com/sent-log")
    assert r.status_code == 404 and r.json() == {"detail": "Domain not found"}
