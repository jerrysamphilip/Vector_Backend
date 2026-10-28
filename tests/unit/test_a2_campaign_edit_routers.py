"""A2 / F-04 routes: one service call each, the request turned into the domain edit, auth and errors kept."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.errors import ConflictError, register_domain_error_handler
import importlib
from app.services.email_render import RenderContext, SendContext, finalize_for_send, render_step_email

campaign_router = importlib.import_module("app.routers.campaign_router")  # app.routers re-exports the APIRouter
ME = SimpleNamespace(user_id="u1", tenant_id="t1", role="MANAGER", custom_permissions=None)
AGENT = SimpleNamespace(user_id="u2", tenant_id="t1", role="AGENT", custom_permissions=None)


class FakeEditor:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def edit(self, user, campaign_id, edit, if_match=None, dry_run=False):
        self.calls.append(("edit", campaign_id, edit, if_match, dry_run))
        if self.error:
            raise self.error
        return {"dry_run": dry_run, "unsent_emails_affected": 3}

    def preview(self, user, campaign_id, prospect_id, step_number, **kw):
        self.calls.append(("preview", campaign_id, prospect_id, step_number, kw))
        return {"campaign_id": campaign_id, "prospect_id": prospect_id, "step_number": step_number,
                "sequence_id": "s1", "source": "template", "to_email": "a@b.co", "subject": "S", "body": "B",
                "is_html": False}

    def template_assignments(self, user, campaign_id, limit, offset):
        self.calls.append(("assignments", campaign_id, limit, offset))
        return {"campaign_id": campaign_id, "steps": [], "total": 0, "limit": limit, "offset": offset,
                "prospects": []}


def _client(editor, user=ME):
    app = FastAPI()
    register_domain_error_handler(app)
    app.include_router(campaign_router.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[campaign_router.campaign_edit_service] = lambda: editor
    return TestClient(app)


def test_edit_passes_the_domain_edit_and_if_match():
    editor = FakeEditor()
    r = _client(editor).post("/campaigns/c1/edits", headers={"If-Match": "4"}, json={
        "settings": {"sender_name": "Bob", "cta_link": None},
        "steps": [{"sequence_id": "s1"}, {"wait_days": 2, "templates": [{"subject": "S", "body": "B"}]}]})
    assert r.status_code == 200 and r.json()["unsent_emails_affected"] == 3
    _, cid, edit, if_match, dry_run = editor.calls[0]
    assert (cid, if_match, dry_run) == ("c1", 4, False)
    assert edit.settings == {"sender_name": "Bob", "cta_link": None}
    assert edit.steps[0].sequence_id == "s1" and edit.steps[1].templates[0].subject == "S"
    assert edit.inbox_ids is None


def test_check_is_a_dry_run_and_conflicts_are_409():
    editor = FakeEditor()
    assert _client(editor).post("/campaigns/c1/edits/checks", json={"expected_version": 2}).json()["dry_run"] is True
    assert editor.calls[0][4] is True
    r = _client(FakeEditor(ConflictError("sent"))).post("/campaigns/c1/edits", json={"expected_version": 2})
    assert r.status_code == 409


def test_agents_cannot_edit_but_can_preview():
    editor = FakeEditor()
    assert _client(editor, AGENT).post("/campaigns/c1/edits", json={"expected_version": 1}).status_code == 403
    r = _client(editor, AGENT).post("/campaigns/c1/email-previews", json={"prospect_id": "p1", "step_number": 2})
    assert r.status_code == 200 and editor.calls[0][:4] == ("preview", "c1", "p1", 2)
    r = _client(editor, AGENT).get("/campaigns/c1/template-assignments?limit=5&offset=10")
    assert r.status_code == 200 and editor.calls[1] == ("assignments", "c1", 5, 10)


def test_render_then_finalize_like_the_sender():
    prospect = SimpleNamespace(first_name="Cara", last_name="Lee", company_name="Acme", designation=None,
                               email="cara@acme.com", industry=None, linkedin_url=None, poc_city=None, poc_state=None)
    subject, body = render_step_email("Hi {{first_name}}", "Hello {{company_name}}", prospect,
                                      RenderContext(sender_name="Ann", default_sender_name="Team"))
    assert subject == "Hi Cara" and "Acme" in body
    final = finalize_for_send(subject, body, prospect.email, SendContext(
        message_id="m1", sender_email="ann@sender.io", base_url="https://app", sender_name="Ann",
        postal_address="1 Main St", footer_line="Edit Co, 1 Main St"))
    assert "Edit Co, 1 Main St" in final.body and "reply 'unsubscribe'" in final.body
    assert "Regards," in final.body and not final.is_internal
    internal = finalize_for_send(subject, body, "x@sender.io", SendContext(message_id="m1", sender_email="ann@sender.io",
                                                                          base_url="https://app"))
    assert internal.is_internal and "unsubscribe" not in internal.body
