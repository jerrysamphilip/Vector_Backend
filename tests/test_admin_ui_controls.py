"""Admin UI controls round, in-process API tests (FastAPI TestClient + the test MySQL schema).

- List rename / description edit (PATCH /lists/{id}) for created and uploaded lists.
- Invitation resend (POST /api/users/{id}/resend-invite): new set-password link, old ones revoked,
  once a minute per user, tenant / role / permission guarded.
- Sales hierarchy from the users admin screen (PUT /team/hierarchy/{id}): cycle and input guards.
- Add sending domain (POST /deliverability/domains) and its DNS guidance (GET .../dns-records).
- Contact scoring guards: the stale sweep honours the per-workspace lock, grade history is not
  written twice, and out-of-office replies do not count as replies.
"""
import uuid
from datetime import datetime

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql


def _member(client, tenant_id: str, label: str, role: str = "AGENT", status: str = "ACTIVE", **cols) -> dict:
    from app.core.security import hash_password
    from app.models.user import User

    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@auc{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status=status, auth_provider="local",
                    email_verified=True, **cols)
        db.add(user)
        db.commit()
        user_id = user.user_id
    out = {"user_id": user_id, "email": email, "password": password}
    if status == "ACTIVE":
        out["headers"] = auth(login(client, email, password))
    return out


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "auc")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "aucb")


# ── List rename ──────────────────────────────────────────────

def test_auc_f01_rename_and_describe_a_list(client, ws, other):
    h = ws["headers"]
    r = client.post("/lists", headers=h, json={"list_name": "Webinar", "list_type": "STATIC"})
    assert r.status_code == 201, r.text
    lid = r.json()["list_id"]
    r = client.patch(f"/lists/{lid}", headers=h, json={"list_name": "  Webinar Q4  ", "description": "Invitees"})
    assert r.status_code == 200, r.text
    assert (r.json()["list_name"], r.json()["description"]) == ("Webinar Q4", "Invitees")
    assert client.get(f"/lists/{lid}", headers=h).json()["list_name"] == "Webinar Q4"
    # Only the description: the name is kept
    r = client.patch(f"/lists/{lid}", headers=h, json={"description": ""})
    assert r.status_code == 200 and r.json()["list_name"] == "Webinar Q4" and r.json()["description"] is None
    # Blank name / too long description / another workspace
    assert client.patch(f"/lists/{lid}", headers=h, json={"list_name": "   "}).status_code == 400
    assert client.patch(f"/lists/{lid}", headers=h, json={"list_name": "x" * 256}).status_code == 422
    assert client.patch(f"/lists/{lid}", headers=h, json={"description": "x" * 2001}).status_code == 422
    assert client.patch(f"/lists/{lid}", headers=other["headers"], json={"list_name": "Hijack"}).status_code == 404
    assert client.get(f"/lists/{lid}", headers=h).json()["list_name"] == "Webinar Q4"


def test_auc_f02_rename_uploaded_list_and_agent_is_refused(client, ws):
    from app.models.prospect_list import ProspectList
    owner_id = sql("SELECT user_id FROM users WHERE email = :e", e=ws["email"])
    with SessionLocal() as db:
        upload = ProspectList(tenant_id=ws["tenant_id"], list_name="leads.csv", list_type="STATIC",
                              source_type="UPLOAD", uploaded_by=owner_id)
        db.add(upload)
        db.commit()
        lid = upload.list_id
    r = client.patch(f"/lists/{lid}", headers=ws["headers"], json={"list_name": "Trade show leads"})
    assert r.status_code == 200, r.text
    assert sql("SELECT list_name FROM prospect_lists WHERE list_id = :l", l=lid) == "Trade show leads"
    agent = _member(client, ws["tenant_id"], "agent", custom_permissions=["manage_campaigns"])
    assert client.patch(f"/lists/{lid}", headers=agent["headers"], json={"list_name": "Nope"}).status_code == 403


# ── Invitation resend ────────────────────────────────────────

@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    def fake_send(**kw):
        sent.append(kw)
        return {"sent": True, "ses_message_id": f"msg-{len(sent)}"}

    monkeypatch.setattr("app.services.invitation_email.send_invitation_email", fake_send)
    return sent


def _age_invitations(email: str, seconds: int = 120):
    sql("UPDATE invitations SET created_at = NOW() - INTERVAL :s SECOND WHERE email = :e", s=seconds, e=email)


def test_auc_f10_resend_issues_new_credentials_and_revokes_old(client, ws, mailbox):
    h = ws["headers"]
    email = f"invitee-{RUN}@auc{RUN}.example.com"
    r = client.post("/api/users/invite", headers=h, json={"email": email, "role": "AGENT"})
    assert r.status_code == 201, r.text
    uid = r.json()["user_id"]
    first = mailbox[-1]
    sql("UPDATE users SET custom_permissions = :p WHERE user_id = :u", p='["manage_campaigns", "view_analytics"]', u=uid)
    # Straight away: once a minute
    r = client.post(f"/api/users/{uid}/resend-invite", headers=h)
    assert r.status_code == 429, r.text
    assert 0 < int(r.headers["Retry-After"]) <= 60
    _age_invitations(email)
    r = client.post(f"/api/users/{uid}/resend-invite", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resent"] is True and body["email"] == email and body["role"] == "AGENT"
    assert body["email_delivery"]["sent"] is True and body["show_once_link"] is None
    second = mailbox[-1]
    assert second["to_email"] == email
    assert "temporary_password" not in second
    assert second["magic_login_link"] != first["magic_login_link"]
    # One pending invitation (the new one); the old one is revoked and its links are spent
    assert sql("SELECT COUNT(*) FROM invitations WHERE email = :e AND status = 'PENDING'", e=email) == 1
    assert sql("SELECT COUNT(*) FROM invitations WHERE email = :e AND status = 'REVOKED'", e=email) >= 1
    assert sql("SELECT COUNT(*) FROM magic_login_tokens WHERE user_id = :u AND used_at IS NULL", u=uid) == 1
    assert sql("SELECT status FROM users WHERE user_id = :u", u=uid) == "INVITED"
    # Customised permissions survive a resend
    perms = client.get(f"/api/users/{uid}/permissions", headers=h).json()
    assert perms["is_custom"] is True and sorted(perms["permissions"]) == ["manage_campaigns", "view_analytics"]
    # The old link no longer signs in (and the invited account has no password at all)
    old_token = first["magic_login_link"].split("token=", 1)[1]
    assert client.post("/api/auth/magic-login", json={"token": old_token}).status_code == 401
    assert sql("SELECT password_hash FROM users WHERE user_id = :u", u=uid) is None
    # And again within the minute: refused
    assert client.post(f"/api/users/{uid}/resend-invite", headers=h).status_code == 429


def test_auc_f11_resend_guards(client, ws, other, mailbox):
    h = ws["headers"]
    active = _member(client, ws["tenant_id"], "joined")
    assert client.post(f"/api/users/{active['user_id']}/resend-invite", headers=h).status_code == 409
    assert client.post(f"/api/users/{uuid.uuid4()}/resend-invite", headers=h).status_code == 404
    email = f"guard-{RUN}@auc{RUN}.example.com"
    uid = client.post("/api/users/invite", headers=h, json={"email": email, "role": "SUPER_ADMIN"}).json()["user_id"]
    _age_invitations(email)
    # Another workspace cannot see it
    assert client.post(f"/api/users/{uid}/resend-invite", headers=other["headers"]).status_code == 404
    # An admin cannot resend a Super Admin invitation; an agent cannot resend at all
    admin = _member(client, ws["tenant_id"], "adm", role="ADMIN")
    assert client.post(f"/api/users/{uid}/resend-invite", headers=admin["headers"]).status_code == 403
    agent = _member(client, ws["tenant_id"], "agt")
    assert client.post(f"/api/users/{uid}/resend-invite", headers=agent["headers"]).status_code == 403
    # The Super Admin owner can
    assert client.post(f"/api/users/{uid}/resend-invite", headers=h).status_code == 200


# ── Sales hierarchy from the users screen ────────────────────

def test_auc_f20_hierarchy_rejects_cycles_and_bad_input(client, ws):
    h = ws["headers"]
    head = _member(client, ws["tenant_id"], "head", role="MANAGER")
    mgr = _member(client, ws["tenant_id"], "mgr", role="MANAGER")
    rep = _member(client, ws["tenant_id"], "rep")
    assert client.put(f"/team/hierarchy/{head['user_id']}", headers=h, json={"sales_level": 1}).status_code == 200
    assert client.put(f"/team/hierarchy/{mgr['user_id']}", headers=h,
                      json={"sales_level": 2, "manager_id": head["user_id"]}).status_code == 200
    assert client.put(f"/team/hierarchy/{rep['user_id']}", headers=h,
                      json={"sales_level": 4, "manager_id": mgr["user_id"]}).status_code == 200
    # Reports to self
    r = client.put(f"/team/hierarchy/{mgr['user_id']}", headers=h, json={"sales_level": 2, "manager_id": mgr["user_id"]})
    assert r.status_code == 400
    # Reports to someone in their own team (levels made inconsistent on purpose): the cycle check holds
    sql("UPDATE users SET sales_level = 1 WHERE user_id = :u", u=rep["user_id"])
    r = client.put(f"/team/hierarchy/{head['user_id']}", headers=h, json={"sales_level": 2, "manager_id": rep["user_id"]})
    assert r.status_code == 400 and "own manager" in r.json()["detail"]
    sql("UPDATE users SET sales_level = 4 WHERE user_id = :u", u=rep["user_id"])
    # Bounded manager id; another workspace's user; agents cannot edit
    assert client.put(f"/team/hierarchy/{rep['user_id']}", headers=h,
                      json={"sales_level": 4, "manager_id": "x" * 37}).status_code == 422
    assert client.put(f"/team/hierarchy/{rep['user_id']}", headers=rep["headers"],
                      json={"sales_level": 1}).status_code == 403
    tree = {u["user_id"]: u for u in client.get("/team/hierarchy", headers=h).json()["users"]}
    assert tree[rep["user_id"]]["manager_id"] == mgr["user_id"] and tree[mgr["user_id"]]["direct_reports"] == 1


def test_auc_f21_invited_user_can_be_placed_in_hierarchy(client, ws, mailbox):
    h = ws["headers"]
    head = _member(client, ws["tenant_id"], "head2", role="MANAGER", sales_level=1)
    uid = client.post("/api/users/invite", headers=h,
                      json={"email": f"placed-{RUN}@auc{RUN}.example.com", "role": "AGENT"}).json()["user_id"]
    r = client.put(f"/team/hierarchy/{uid}", headers=h, json={"sales_level": 2, "manager_id": head["user_id"]})
    assert r.status_code == 200, r.text
    listed = {u["user_id"]: u for u in client.get("/team/hierarchy", headers=h).json()["users"]}
    assert listed[uid]["sales_level"] == 2 and listed[uid]["status"] == "INVITED"


# ── Sending domains ──────────────────────────────────────────

class FakeSes:
    def __init__(self, fail=False):
        self.fail = fail
        self.verified = []

    def verify_domain_dkim(self, Domain):
        if self.fail:
            raise RuntimeError("no credentials")
        self.verified.append(Domain)
        return {"DkimTokens": ["aaa111", "bbb222", "ccc333"]}

    def get_identity_dkim_attributes(self, Identities):
        if self.fail:
            raise RuntimeError("no credentials")
        return {"DkimAttributes": {d: {"DkimTokens": ["aaa111", "bbb222", "ccc333"],
                                       "DkimVerificationStatus": "Pending"} for d in Identities}}


@pytest.fixture
def ses(monkeypatch):
    from app.services.deliverability_service import deliverability_service
    fake = FakeSes()
    monkeypatch.setattr(deliverability_service, "ses_v1", fake)
    return fake


def test_auc_f30_add_domain_returns_dns_guidance(client, ws, other, ses):
    h = ws["headers"]
    name = f"send{RUN}.example.com"
    r = client.post("/deliverability/domains", headers=h, json={"domain_name": f"  {name.upper()}. "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["domain"]["domain_name"] == name and body["dkim_tokens_available"] is True
    assert body["domain"]["dkim_status"] == "PENDING" and body["domain"]["spf_status"] == "UNKNOWN"
    hosts = {(rec["kind"], rec["host"]) for rec in body["dns_records"]}
    assert ("DKIM", f"aaa111._domainkey.{name}") in hosts and ("SPF", f"mail.{name}") in hosts
    assert ("DMARC", f"_dmarc.{name}") in hosts
    assert ses.verified == [name]
    assert name in [d["domain_name"] for d in client.get("/deliverability/domains", headers=h).json()]
    assert sql("SELECT COUNT(*) FROM sending_domains WHERE tenant_id = :t AND domain_name = :d",
               t=ws["tenant_id"], d=name) == 1
    # Duplicate in the same workspace (any case) is refused; another workspace may add it
    assert client.post("/deliverability/domains", headers=h, json={"domain_name": name.upper()}).status_code == 409
    assert client.post("/deliverability/domains", headers=other["headers"], json={"domain_name": name}).status_code == 201
    # DNS records for it, for any role of the workspace; not for a workspace without it
    agent = _member(client, ws["tenant_id"], "domagent")
    r = client.get(f"/deliverability/domains/{name}/dns-records", headers=agent["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["dkim_status"] == "PENDING" and len(r.json()["dns_records"]) >= 5
    third = f"only{RUN}.example.org"
    assert client.post("/deliverability/domains", headers=h, json={"domain_name": third}).status_code == 201
    assert client.get(f"/deliverability/domains/{third}/dns-records", headers=other["headers"]).status_code == 404


@pytest.mark.parametrize("bad", ["", "localhost", "http://example.com", "user@example.com", "example.com/x",
                                 "10.0.0.1", "exa mple.com", "*.example.com", "x" * 301])
def test_auc_f31_add_domain_validation(client, ws, ses, bad):
    r = client.post("/deliverability/domains", headers=ws["headers"], json={"domain_name": bad})
    assert r.status_code in (400, 422), r.text


def test_auc_f32_add_domain_admin_only_and_works_without_ses(client, ws, monkeypatch):
    from app.services.deliverability_service import deliverability_service
    monkeypatch.setattr(deliverability_service, "ses_v1", FakeSes(fail=True))
    agent = _member(client, ws["tenant_id"], "domagent2")
    name = f"nosesa{RUN}.example.net"
    assert client.post("/deliverability/domains", headers=agent["headers"], json={"domain_name": name}).status_code == 403
    r = client.post("/deliverability/domains", headers=ws["headers"], json={"domain_name": name})
    assert r.status_code == 201, r.text
    assert r.json()["dkim_tokens_available"] is False and r.json()["domain"]["dkim_status"] == "UNKNOWN"
    assert any("<token>" in rec["host"] for rec in r.json()["dns_records"])


# ── Contact scoring guards ───────────────────────────────────

TAG_MODEL = {
    "fit_rules": [{"id": "hot", "label": "Hot tag", "field": "tags", "operator": "in", "value": "hot", "points": 80}],
    "fit_weight": 100, "engagement_weight": 0, "half_life_days": 0, "lookback_days": 180,
    "grade_thresholds": {"A": 75, "B": 50, "C": 25},
}
REPLY_MODEL = {
    "fit_rules": [], "fit_weight": 0, "engagement_weight": 100, "half_life_days": 0, "lookback_days": 180,
    "grade_thresholds": {"A": 75, "B": 50, "C": 10},
}


def _contact(client, h, local, **kw):
    r = client.post("/contacts", headers=h, json={"email": f"{local}.{RUN}.{uuid.uuid4().hex[:4]}@corp{RUN}.example.com", **kw})
    assert r.status_code == 201, r.text
    return r.json()["prospect_id"]


@pytest.fixture(scope="module")
def sws(client):
    return register_tenant(client, "aucs")


def test_auc_f40_stale_sweep_skips_a_locked_workspace(client, sws, monkeypatch):
    from app.services import contact_scoring
    h = sws["headers"]
    assert client.put("/contacts/scoring-model", headers=h, json=TAG_MODEL).status_code == 200
    pid = _contact(client, h, "locked", tags=["hot"])
    sql("UPDATE prospects SET score = NULL, score_grade = NULL, scored_at = NULL, updated_at = updated_at "
        "WHERE prospect_id = :p", p=pid)
    sql("UPDATE contact_scoring_models SET recompute_requested_at = NULL WHERE tenant_id = :t", t=sws["tenant_id"])
    taken = []
    monkeypatch.setattr(contact_scoring, "_try_lock", lambda name: taken.append(name) or None)
    stats = contact_scoring.run_scoring_job(tenant_id=sws["tenant_id"])
    assert stats["refreshed"] == 0 and stats.get("skipped_locked") == 1
    assert f"score:{sws['tenant_id']}" in taken
    assert sql("SELECT scored_at FROM prospects WHERE prospect_id = :p", p=pid) is None
    # Lock free again: the next pass scores it
    monkeypatch.undo()
    contact_scoring.run_scoring_job(tenant_id=sws["tenant_id"])
    assert sql("SELECT score FROM prospects WHERE prospect_id = :p", p=pid) == 80


def test_auc_f41_grade_history_not_written_twice(client, sws):
    from app.models.crm import PropertyChange
    from app.services import contact_scoring
    h = sws["headers"]
    assert client.put("/contacts/scoring-model", headers=h, json=TAG_MODEL).status_code == 200
    pid = _contact(client, h, "hist", tags=["hot"])
    assert sql("SELECT score_grade FROM prospects WHERE prospect_id = :p", p=pid) == "A"
    sql("DELETE FROM property_changes WHERE object_id = :p AND field = 'score_grade'", p=pid)
    # Another run already logged C -> A, but this run read the contact while it was still C
    sql("UPDATE prospects SET score_grade = 'C', updated_at = updated_at WHERE prospect_id = :p", p=pid)
    with SessionLocal() as db:
        db.add(PropertyChange(tenant_id=sws["tenant_id"], object_type="CONTACT", object_id=pid, field="score_grade",
                              old_value="C", new_value="A", source="SYSTEM"))
        db.commit()
    with SessionLocal() as db:
        contact_scoring.score_contacts(db, sws["tenant_id"], [pid])
        db.commit()
    count = "SELECT COUNT(*) FROM property_changes WHERE object_id = :p AND field = 'score_grade'"
    assert sql(count, p=pid) == 1
    # A real change is still recorded: the grade went A -> B (logged), now back to A
    sql("UPDATE prospects SET score_grade = 'B', updated_at = updated_at WHERE prospect_id = :p", p=pid)
    with SessionLocal() as db:
        db.add(PropertyChange(tenant_id=sws["tenant_id"], object_type="CONTACT", object_id=pid, field="score_grade",
                              old_value="A", new_value="B", source="SYSTEM"))
        db.commit()
    with SessionLocal() as db:
        contact_scoring.score_contacts(db, sws["tenant_id"], [pid])
        db.commit()
    assert sql(count, p=pid) == 3
    assert sql("SELECT new_value FROM property_changes WHERE object_id = :p AND field = 'score_grade' "
               "ORDER BY changed_at DESC LIMIT 1", p=pid) == "A"


def test_auc_f42_out_of_office_replies_do_not_count(client, sws):
    from app.models.email_message import EmailMessage
    h = sws["headers"]
    assert client.put("/contacts/scoring-model", headers=h, json=REPLY_MODEL).status_code == 200
    pid = _contact(client, h, "ooo")
    now = datetime.utcnow()
    with SessionLocal() as db:
        db.add(EmailMessage(prospect_id=pid, direction="INBOUND", status="RECEIVED", to_email="me@example.com",
                            subject="Automatic reply: Hello", body_text="I am out of the office until Monday.", sent_at=now))
        db.add(EmailMessage(prospect_id=pid, direction="INBOUND", status="RECEIVED", to_email="me@example.com",
                            subject="Re: Hello", body_text="Yes, let's talk on Tuesday.", sent_at=now))
        db.add(EmailMessage(prospect_id=pid, direction="INBOUND", status="RECEIVED", to_email="me@example.com",
                            subject="Re: Hello", body_text="I'm on vacation and will reply when back.", sent_at=now))
        db.commit()
    r = client.post(f"/contacts/{pid}/score/recompute", headers=h)
    assert r.status_code == 200, r.text
    sig = {s["signal"]: s["count"] for s in r.json()["engagement_signals"]}
    assert sig["email_replied"] == 1
