"""Contact scoring on fit and engagement (BR-CM-43 / CM-083), in-process API tests.

The scoring model endpoints (admins), the per-contact score with its breakdown, rescoring on
create / edit / logged meeting / completed task, the grade-change history row, list sort and
filters by score and grade, the async full recompute, the worker pass, and visibility
(hierarchy and tenant isolation).
"""
import uuid

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql
from app.core.clock import utcnow

# A small deterministic model: fit from tags only, total = fit
TAG_MODEL = {
    "fit_rules": [
        {"id": "hot", "label": "Hot tag", "field": "tags", "operator": "in", "value": "hot", "points": 80},
        {"id": "warm", "label": "Warm tag", "field": "tags", "operator": "in", "value": "warm", "points": 40},
    ],
    "fit_weight": 100, "engagement_weight": 0, "half_life_days": 0, "lookback_days": 180,
    "grade_thresholds": {"A": 75, "B": 50, "C": 25},
}
# Engagement only: a meeting is worth 15, grade C from 10
ENGAGEMENT_MODEL = {
    "fit_rules": [], "fit_weight": 0, "engagement_weight": 100, "half_life_days": 0, "lookback_days": 180,
    "grade_thresholds": {"A": 75, "B": 50, "C": 10},
}


def _member(client, tenant_id: str, label: str, role: str = "AGENT") -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@score{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True)
        db.add(user)
        db.commit()
        user_id = user.user_id
    return {"user_id": user_id, "headers": auth(login(client, email, password))}


def _contact(client, h, local, **kw):
    r = client.post("/contacts", headers=h, json={"email": f"{local}.{RUN}.{uuid.uuid4().hex[:4]}@corp{RUN}.example.com", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _put_model(client, h, model):
    r = client.put("/contacts/scoring-model", headers=h, json=model)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def tenant_s(client):
    return register_tenant(client, "score")


# ── Model ─────────────────────────────────────────────────────

def test_scoring_model_seeded_with_defaults(client, tenant_s):
    from app.services.scoring_engine import default_model
    r = client.get("/contacts/scoring-model", headers=tenant_s["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == default_model()
    assert {f["key"] for f in body["fields"]} >= {"designation", "employees", "annual_revenue", "industry", "country",
                                                   "lifecycle_stage", "email_type"}
    assert {s["key"] for s in body["signals"]} == {"email_opened", "email_clicked", "email_replied", "positive_reply",
                                                   "meeting", "call", "task_completed"}
    assert sql("SELECT COUNT(*) FROM contact_scoring_models WHERE tenant_id = :t", t=tenant_s["tenant_id"]) == 1


def test_scoring_model_admin_only(client, tenant_s):
    agent = _member(client, tenant_s["tenant_id"], "agent")
    manager = _member(client, tenant_s["tenant_id"], "mgr", role="MANAGER")
    for who in (agent, manager):
        assert client.get("/contacts/scoring-model", headers=who["headers"]).status_code == 403
        assert client.put("/contacts/scoring-model", headers=who["headers"], json=TAG_MODEL).status_code == 403
        assert client.post("/contacts/scoring-model/recompute", headers=who["headers"]).status_code == 403
    assert client.get("/contacts/scoring-model").status_code in (401, 403)


@pytest.mark.parametrize("bad, status", [
    ({"fit_weight": 60, "engagement_weight": 60}, 400),
    ({"grade_thresholds": {"A": 10, "B": 50, "C": 5}}, 400),
    ({"fit_rules": [{"field": "salary", "operator": "equals", "value": "x", "points": 1}]}, 400),
    ({"fit_rules": [{"field": "employees", "operator": "between", "value": "lots", "points": 1}]}, 400),
    ({"engagement": {"telepathy": {"points": 1, "max": 1}}}, 400),
    ({"fit_rules": [{"field": "industry", "operator": "in", "value": "x", "points": 101}]}, 422),
    ({"fit_rules": [{"field": "industry", "operator": "in", "value": "x" * 501, "points": 1}]}, 422),
    ({"fit_rules": [{"field": "industry", "operator": "in", "value": "x", "points": 1}] * 51}, 422),
    ({"half_life_days": 9999}, 422),
    ({"grade_thresholds": {"A": 101, "B": 50, "C": 25}}, 422),
])
def test_scoring_model_validation(client, tenant_s, bad, status):
    before = client.get("/contacts/scoring-model", headers=tenant_s["headers"]).json()["model"]
    r = client.put("/contacts/scoring-model", headers=tenant_s["headers"], json=bad)
    assert r.status_code == status, r.text
    assert client.get("/contacts/scoring-model", headers=tenant_s["headers"]).json()["model"] == before


def test_scoring_model_update_rescoring_everyone(client, tenant_s):
    h = tenant_s["headers"]
    pid = _contact(client, h, "upd", tags=["hot"])["prospect_id"]
    body = _put_model(client, h, TAG_MODEL)
    assert [r["id"] for r in body["model"]["fit_rules"]] == ["hot", "warm"]
    # The background recompute ran after the response (TestClient runs background tasks)
    after = client.get("/contacts/scoring-model", headers=h).json()
    assert after["recompute_requested_at"] is None and after["last_recomputed_at"]
    assert sql("SELECT score FROM prospects WHERE prospect_id = :p", p=pid) == 80
    assert sql("SELECT score_grade FROM prospects WHERE prospect_id = :p", p=pid) == "A"


# ── One contact ───────────────────────────────────────────────

def test_contact_scored_on_create_with_breakdown(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, {})  # back to the defaults
    contact = _contact(client, h, "ceo", first_name="Cleo", designation="Chief Executive Officer")
    assert contact["score"] is not None and contact["score_grade"] in "ABCD"
    r = client.get(f"/contacts/{contact['prospect_id']}/score", headers=h)
    assert r.status_code == 200, r.text
    s = r.json()
    matched = {rule["id"] for rule in s["fit_rules"] if rule["matched"]}
    assert {"exec", "business_email"} <= matched
    assert s["fit"] == 40 and s["engagement"] == 0 and s["score"] == 20 and s["grade"] == "D"
    assert s["stored"]["score"] == 20 and s["stored"]["scored_at"]
    assert len(s["engagement_signals"]) == 7


def test_edit_rescoring_and_personal_email(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, {})
    pid = _contact(client, h, "edit")["prospect_id"]
    before = client.get(f"/contacts/{pid}/score", headers=h).json()["fit"]
    r = client.patch(f"/contacts/{pid}", headers=h, json={"designation": "VP Engineering", "industry": "Software"})
    assert r.status_code == 200, r.text
    assert r.json()["fit_score"] == before + 30 + 15
    r = client.post("/contacts", headers=h, json={"email": f"someone.{RUN}.{uuid.uuid4().hex[:4]}@gmail.com"})
    assert r.status_code == 201
    s = client.get(f"/contacts/{r.json()['prospect_id']}/score", headers=h).json()
    assert "personal_email" in {rule["id"] for rule in s["fit_rules"] if rule["matched"]} and s["fit"] == 0


def test_meeting_raises_engagement_and_grade_change_is_in_history(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, ENGAGEMENT_MODEL)
    pid = _contact(client, h, "meet")["prospect_id"]
    assert client.get(f"/contacts/{pid}", headers=h).json()["score_grade"] == "D"
    r = client.post(f"/contacts/{pid}/activities", headers=h, json={"activity_type": "MEETING", "subject": "Demo"})
    assert r.status_code == 201, r.text
    detail = client.get(f"/contacts/{pid}", headers=h).json()
    assert (detail["engagement_score"], detail["score"], detail["score_grade"]) == (15, 15, "C")
    hist = client.get(f"/contacts/{pid}/history", headers=h, params={"field": "score_grade"}).json()["items"]
    assert [(i["old_value"], i["new_value"], i["source"]) for i in hist] == [("D", "C", "SYSTEM")]
    assert hist[0]["label"] == "Score grade"
    # A note is not engagement: no rescore, no new history row
    client.post(f"/contacts/{pid}/activities", headers=h, json={"activity_type": "NOTE", "body": "fyi"})
    assert len(client.get(f"/contacts/{pid}/history", headers=h, params={"field": "score_grade"}).json()["items"]) == 1
    sig = {s["signal"]: s for s in client.get(f"/contacts/{pid}/score", headers=h).json()["engagement_signals"]}
    assert sig["meeting"]["count"] == 1 and sig["meeting"]["contributed"] == 15


def test_completed_task_counts_towards_engagement(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, ENGAGEMENT_MODEL)
    pid = _contact(client, h, "task")["prospect_id"]
    r = client.post("/tasks", headers=h, json={"title": "Send deck", "prospect_id": pid})
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    assert client.patch(f"/tasks/{task_id}", headers=h, json={"status": "DONE"}).status_code == 200
    assert sql("SELECT engagement_score FROM prospects WHERE prospect_id = :p", p=pid) == 3


def test_email_engagement_counted_by_recompute(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, ENGAGEMENT_MODEL)
    pid = _contact(client, h, "mail")["prospect_id"]
    from app.models.email_message import EmailEvent, EmailMessage
    with SessionLocal() as db:
        out = EmailMessage(prospect_id=pid, direction="OUTBOUND", status="SENT", subject="Hi", to_email="x@example.com",
                           sent_at=utcnow())
        db.add(out)
        db.flush()
        for etype in (EmailEvent.EVENT_OPEN, EmailEvent.EVENT_OPEN, EmailEvent.EVENT_CLICK, EmailEvent.EVENT_POSITIVE_REPLY):
            db.add(EmailEvent(message_id=out.message_id, event_type=etype, event_time=utcnow()))
        db.add(EmailMessage(prospect_id=pid, direction="INBOUND", status="RECEIVED", subject="Re: Hi",
                            to_email="me@example.com", sent_at=utcnow()))
        db.commit()
    r = client.post(f"/contacts/{pid}/score/recompute", headers=h)
    assert r.status_code == 200, r.text
    sig = {s["signal"]: s["count"] for s in r.json()["engagement_signals"]}
    # Two opens of one email count once
    assert (sig["email_opened"], sig["email_clicked"], sig["positive_reply"], sig["email_replied"]) == (1, 1, 1, 1)
    assert r.json()["engagement"] == 3 + 6 + 20 + 15
    assert r.json()["stored"]["score"] == 44


# ── List ──────────────────────────────────────────────────────

def test_list_sort_and_filter_by_score_and_grade(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, TAG_MODEL)
    marker = f"lst{uuid.uuid4().hex[:6]}"
    hot = _contact(client, h, f"{marker}hot", tags=["hot"])["prospect_id"]
    warm = _contact(client, h, f"{marker}warm", tags=["warm"])["prospect_id"]
    cold = _contact(client, h, f"{marker}cold")["prospect_id"]

    def ids(**params):
        r = client.get("/contacts", headers=h, params={"q": marker, **params})
        assert r.status_code == 200, r.text
        return [c["prospect_id"] for c in r.json()["items"]]

    assert ids(sort_by="score", sort_order="desc") == [hot, warm, cold]
    assert ids(sort_by="score", sort_order="asc") == [cold, warm, hot]
    assert ids(grade="A") == [hot]
    assert sorted(ids(grade="a,c")) == sorted([hot, warm])
    assert sorted(ids(score_min=30)) == sorted([hot, warm])
    assert ids(score_max=10) == [cold]
    assert ids(score_min=30, score_max=50) == [warm]
    item = client.get("/contacts", headers=h, params={"q": f"{marker}hot"}).json()["items"][0]
    assert (item["score"], item["fit_score"], item["engagement_score"], item["score_grade"]) == (80, 80, 0, "A")
    assert "score" in [c["key"] for c in client.get("/contacts/meta", headers=h).json()["columns"]]
    assert client.get("/contacts", headers=h, params={"grade": "Z"}).status_code == 400
    assert client.get("/contacts", headers=h, params={"score_min": 101}).status_code == 422
    assert client.get("/contacts", headers=h, params={"grade": "A" * 21}).status_code == 422


# ── Recompute and worker ──────────────────────────────────────

def test_recompute_is_async_and_keeps_updated_at(client, tenant_s):
    h = tenant_s["headers"]
    _put_model(client, h, TAG_MODEL)
    pid = _contact(client, h, "recomp", tags=["warm"])["prospect_id"]
    updated = sql("SELECT updated_at FROM prospects WHERE prospect_id = :p", p=pid)
    sql("UPDATE prospects SET score = NULL, score_grade = NULL, scored_at = NULL, updated_at = updated_at "
        "WHERE prospect_id = :p", p=pid)
    r = client.post("/contacts/scoring-model/recompute", headers=h)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued" and r.json()["contacts"] >= 1
    assert sql("SELECT score FROM prospects WHERE prospect_id = :p", p=pid) == 40
    assert sql("SELECT updated_at FROM prospects WHERE prospect_id = :p", p=pid) == updated
    model = client.get("/contacts/scoring-model", headers=h).json()
    assert model["recompute_requested_at"] is None and model["last_recompute_count"] >= 1
    assert model["stats"]["scored"] == model["stats"]["contacts"]


def test_worker_job_scores_stale_and_requested(client, tenant_s):
    from app.services import contact_scoring
    h = tenant_s["headers"]
    _put_model(client, h, TAG_MODEL)
    pid = _contact(client, h, "stale", tags=["hot"])["prospect_id"]
    sql("UPDATE prospects SET score = NULL, score_grade = NULL, scored_at = NULL, updated_at = updated_at "
        "WHERE prospect_id = :p", p=pid)
    stats = contact_scoring.run_scoring_job(tenant_id=tenant_s["tenant_id"])
    assert stats["refreshed"] >= 1
    assert sql("SELECT score FROM prospects WHERE prospect_id = :p", p=pid) == 80
    # A requested full recompute is picked up by the worker too
    sql("UPDATE contact_scoring_models SET recompute_requested_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE tenant_id = :t", t=tenant_s["tenant_id"])
    contact_scoring.run_scoring_job(tenant_id=tenant_s["tenant_id"])
    assert sql("SELECT recompute_requested_at FROM contact_scoring_models WHERE tenant_id = :t",
               t=tenant_s["tenant_id"]) is None


# ── Visibility ────────────────────────────────────────────────

def test_score_respects_visibility_and_tenants(client, tenant_s, tenant_b):
    h = tenant_s["headers"]
    admin_contact = _contact(client, h, "vis")["prospect_id"]
    agent = _member(client, tenant_s["tenant_id"], "own")
    own = _contact(client, agent["headers"], "agentown")["prospect_id"]
    assert client.get(f"/contacts/{own}/score", headers=agent["headers"]).status_code == 200
    assert client.get(f"/contacts/{admin_contact}/score", headers=agent["headers"]).status_code == 404
    assert client.post(f"/contacts/{admin_contact}/score/recompute", headers=agent["headers"]).status_code == 404
    assert client.get(f"/contacts/{admin_contact}/score", headers=tenant_b["headers"]).status_code == 404
    # The agent's list only holds their own contacts, also when sorted / filtered by score
    listed = client.get("/contacts", headers=agent["headers"], params={"sort_by": "score", "page_size": 200}).json()
    assert admin_contact not in [c["prospect_id"] for c in listed["items"]]
    # Another workspace's model is independent
    other = client.get("/contacts/scoring-model", headers=tenant_b["headers"]).json()["model"]
    assert other["fit_rules"] != TAG_MODEL["fit_rules"]
