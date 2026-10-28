"""lrest route services with fake repositories and a counting unit of work: subject A/B tests, company
profiles, account connections and AI generation (one service call per route, guards before writes)."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied, ValidationFailed
from app.services import ai_generation_service as ai_mod
from app.services import company_profile_service as cp_mod
from app.services import connection_service as conn_mod
from app.services import subject_variant_routes as sv_mod
from app.services.ai_generation_service import AiGenerationService
from app.services.company_profile_service import CompanyProfileService
from app.services.connection_service import ConnectionService, connection_dict
from app.services.subject_variant_routes import SubjectVariantRoutesService, variant_out
from app.services.subject_variant_service import SubjectVariantError

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="MANAGER", email="me@x.io")
ADMIN = SimpleNamespace(user_id="boss", tenant_id="t1", role="ADMIN")


class Uow:
    def __init__(self, **repos):
        self.__dict__.update(repos)
        self.session = SimpleNamespace(name="session")
        self.entered = self.commits = self.rollbacks = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ── subject A/B tests ───────────────────────────────────────────────

class Campaigns:
    def __init__(self, campaigns):
        self.campaigns = campaigns

    def get(self, tenant_id, campaign_id):
        c = self.campaigns.get(campaign_id)
        return c if c and c.tenant_id == tenant_id else None


VARIANT = SimpleNamespace(variant_id="v1", label="A", subject="Hi", weight=1, is_winner=False, promoted_at=None)
WINNER = SimpleNamespace(variant_id="v2", label="B", subject="Yo", weight=1, is_winner=True,
                         promoted_at=datetime(2026, 1, 2, 3, 4, 5))


class FakeVariants:
    """Stands in for SubjectVariantService; ``fail`` raises SubjectVariantError(status) from get_step."""
    fail = None

    def __init__(self, db, uow=None):
        self.uow = uow

    def get_step(self, campaign_id, step):
        if FakeVariants.fail:
            raise SubjectVariantError(FakeVariants.fail, "Sequence step not found")
        return SimpleNamespace(sequence_id="s1", step_number=step)

    def list_variants(self, sequence_id):
        return [VARIANT, WINNER]

    @staticmethod
    def promoted(variants):
        return next((v for v in variants if v.is_winner), None)

    def stats(self, seq):
        return {"step": seq.step_number}

    def messages(self, seq, label, limit, offset):
        return {"label": label, "limit": limit, "offset": offset}

    def create(self, campaign, seq, subject, weight, user_id):
        return VARIANT, ["w"], 3

    def update(self, campaign, seq, label, subject, weight, user_id):
        return VARIANT, [], 0

    def delete(self, campaign, seq, label, user_id):
        return 1

    def promote(self, campaign, seq, label, user_id):
        return {"promoted": label}


@pytest.fixture
def variants(monkeypatch):
    FakeVariants.fail = None
    monkeypatch.setattr(sv_mod, "SubjectVariantService", FakeVariants)
    campaign = SimpleNamespace(campaign_id="c1", tenant_id="t1", created_by="me")
    uow = Uow(campaigns=Campaigns({"c1": campaign, "c2": SimpleNamespace(campaign_id="c2", tenant_id="t2")}))
    return SubjectVariantRoutesService(uow), uow


def test_variant_reads(variants):
    service, uow = variants
    listing = service.listing("c1", 2, ME)
    assert listing["step"] == 2 and listing["promoted_label"] == "B" and listing["ab_active"] is True
    assert listing["variants"][1]["promoted_at"] == "2026-01-02T03:04:05"
    assert service.stats("c1", 2, ME) == {"step": 2}
    assert service.messages("c1", 2, "b", 5, 0, ME) == {"label": "B", "limit": 5, "offset": 0}
    assert service.messages("c1", 2, None, 5, 0, ME)["label"] is None
    assert uow.commits == 0


def test_variant_writes_commit(variants):
    service, uow = variants
    out = service.create("c1", 1, "Hi", None, ME)
    assert out["updated_messages"] == 3 and out["warnings"] == ["w"] and out["listing"]["step"] == 1
    assert service.update("c1", 1, "A", "New", None, ME)["variant_id"] == "v1"
    service.delete("c1", 1, "A", ME)
    assert service.promote("c1", 1, "A", ME) == {"promoted": "A"}
    assert uow.commits == 4


def test_variant_guards(variants):
    service, uow = variants
    with pytest.raises(NotFoundError):
        service.listing("c2", 1, ME)  # another workspace's campaign
    with pytest.raises(ValidationFailed):
        service.update("c1", 1, "A", None, None, ME)
    other = SimpleNamespace(user_id="u2", tenant_id="t1", role="AGENT")
    with pytest.raises(PermissionDenied):
        service.promote("c1", 1, "A", other)
    assert service.promote("c1", 1, "A", ADMIN) == {"promoted": "A"}


@pytest.mark.parametrize("status,error", [(404, NotFoundError), (409, ConflictError), (422, ValidationFailed),
                                          (400, BadRequestError), (418, BadRequestError)])
def test_variant_errors_become_domain_errors_and_roll_back(variants, status, error):
    service, uow = variants
    FakeVariants.fail = status
    for call in (lambda: service.listing("c1", 1, ME), lambda: service.stats("c1", 1, ME),
                 lambda: service.messages("c1", 1, None, 1, 0, ME), lambda: service.create("c1", 1, "x", 1, ME),
                 lambda: service.update("c1", 1, "A", "x", None, ME), lambda: service.delete("c1", 1, "A", ME),
                 lambda: service.promote("c1", 1, "A", ME)):
        with pytest.raises(error):
            call()
    assert uow.commits == 0 and uow.rollbacks == 7


def test_variant_out():
    assert variant_out(VARIANT) == {"variant_id": "v1", "label": "A", "subject": "Hi", "weight": 1,
                                    "is_winner": False, "promoted_at": None}


# ── company profiles ────────────────────────────────────────────────

class Profiles:
    def __init__(self):
        self.rows = {}
        self.cleared = []
        self.deleted = []

    def list(self, tenant_id):
        return [p for p in self.rows.values() if p.tenant_id == tenant_id]

    def get(self, tenant_id, profile_id):
        p = self.rows.get(profile_id)
        return p if p and p.tenant_id == tenant_id else None

    def default(self, tenant_id):
        rows = self.list(tenant_id)
        return sorted(rows, key=lambda p: not p.is_default)[0] if rows else None

    def clear_default(self, tenant_id, keep_profile_id=None):
        self.cleared.append((tenant_id, keep_profile_id))
        for p in self.list(tenant_id):
            if p.profile_id != keep_profile_id:
                p.is_default = False

    def add(self, profile):
        self.rows[profile.profile_id] = profile
        return profile

    def delete(self, profile):
        self.deleted.append(profile.profile_id)
        del self.rows[profile.profile_id]

    def refresh(self, profile):
        pass


@pytest.fixture
def profiles(monkeypatch):
    invalidated = []
    monkeypatch.setattr(cp_mod, "invalidate_sender_identity", invalidated.append)
    monkeypatch.setattr(cp_mod, "get_sender_identity", lambda tenant_id, db, fresh=False:
                        SimpleNamespace(company_name="Acme", postal_address=None))
    repo = Profiles()
    uow = Uow(profiles=repo)
    return CompanyProfileService(uow), uow, repo, invalidated


def _data(**kw):
    base = dict(profile_name="Main", company_name="Acme", company_description=None, default_sender_name=None,
                default_cta_link=None, postal_address=None, is_default=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_profile_crud(profiles):
    service, uow, repo, invalidated = profiles
    created = service.create(_data(is_default=True, postal_address="  1 Road "), ME)
    assert created.tenant_id == "t1" and created.postal_address == "1 Road" and repo.cleared == [("t1", None)]
    assert service.list(ME) == [created] and service.get(created.profile_id, ME) is created
    updated = service.update(created.profile_id, _data(company_name="New", postal_address=" ", is_default=True,
                                                       profile_name=None), ME)
    assert updated.company_name == "New" and updated.postal_address is None and updated.profile_name == "Main"
    assert repo.cleared[-1] == ("t1", created.profile_id)
    assert service.delete(created.profile_id, ME) == {"message": "Company profile deleted",
                                                      "profile_id": created.profile_id}
    assert invalidated == ["t1", "t1", "t1"] and uow.commits == 3


def test_profile_not_found_in_another_tenant(profiles):
    service, uow, repo, _ = profiles
    repo.add(SimpleNamespace(profile_id="x", tenant_id="t2", is_default=True))
    for call in (lambda: service.get("x", ME), lambda: service.update("x", _data(), ME),
                 lambda: service.delete("x", ME)):
        with pytest.raises(NotFoundError):
            call()
    assert uow.commits == 0


def test_sender_identity(profiles):
    service, uow, repo, invalidated = profiles
    assert service.sender_identity(ME).profile_id is None
    with pytest.raises(BadRequestError):
        service.set_sender_identity(" ", "1 Road", ME)
    with pytest.raises(BadRequestError):
        service.set_sender_identity("Acme", "abc", ME)
    out = service.set_sender_identity(" Acme ", " 1 Road, Town ", ME)
    assert (out.company_name, out.postal_address, out.is_complete) == ("Acme", "1 Road, Town", True)
    profile = repo.rows[out.profile_id]
    assert profile.profile_name == "Default" and profile.is_default
    profile.is_default = False
    again = service.set_sender_identity("Acme 2", "2 Road, Town", ME)
    assert again.profile_id == out.profile_id and profile.is_default and repo.cleared == [("t1", None)]
    identity = service.sender_identity(ME)
    assert identity.profile_id == out.profile_id and identity.is_complete is False
    assert invalidated == ["t1", "t1"]


# ── connections ─────────────────────────────────────────────────────

CONN = SimpleNamespace(connection_id="k1", provider="GOOGLE", account_email="me@gmail.com", sync_email=True,
                       sync_calendar=True, last_sync_at=None, last_error=None, created_at=None, user_id="me")


class Connections:
    def __init__(self):
        self.deleted = []
        self.users = {"me": ME}

    def for_user(self, user_id):
        return [CONN] if user_id == "me" else []

    def owned(self, user_id, connection_id):
        return CONN if (user_id, connection_id) == ("me", "k1") else None

    def user(self, user_id):
        return self.users.get(user_id)

    def delete(self, entity):
        self.deleted.append(entity)


@pytest.fixture
def connections(monkeypatch):
    calls = []
    monkeypatch.setattr(conn_mod.account_sync, "configured", lambda p: p == "GOOGLE")
    monkeypatch.setattr(conn_mod.account_sync, "authorize_url", lambda p, user, nonce: f"https://auth/{p}/{nonce}")
    monkeypatch.setattr(conn_mod.account_sync, "sync_connection", lambda db, c: calls.append(("sync", c)) or {"emails": 1})
    monkeypatch.setattr(conn_mod.account_sync, "complete", lambda db, user, provider, code: calls.append(("complete", code)) or CONN)
    repo = Connections()
    uow = Uow(connections=repo)
    return ConnectionService(uow), uow, repo, calls


def test_connections_mine_update_sync_disconnect(connections):
    service, uow, repo, calls = connections
    mine = service.mine(ME)
    assert mine["connections"] == [connection_dict(CONN)] and mine["available"] == {"GOOGLE": True, "MICROSOFT": False}
    assert service.update("k1", {"sync_calendar": 0}, ME)["sync_calendar"] is False
    CONN.sync_calendar = True
    assert service.sync_now("k1", ME) == {"emails": 1, "connection": connection_dict(CONN)}
    assert service.disconnect("k1", ME) == {"status": "disconnected"} and repo.deleted == [CONN]
    with pytest.raises(NotFoundError):
        service.update("other", {}, ME)
    assert uow.commits == 2


def test_connection_start(connections):
    assert ConnectionService.start("google", ME, lambda: "n1") == {"authorize_url": "https://auth/GOOGLE/n1"}


def test_connection_callback_outcomes(connections, monkeypatch):
    service, uow, repo, calls = connections
    state = {"provider": "GOOGLE", "user_id": "me", "nonce": "n"}
    monkeypatch.setattr(conn_mod.account_sync, "read_state", lambda s: dict(state))
    ok = lambda nonce: nonce == "n"  # noqa: E731
    assert service.complete("google", "s", "code", "", "", ok) == {"sync": "connected", "provider": "google"}
    assert calls == [("complete", "code"), ("sync", CONN)]
    assert service.complete("google", "s", "c", "", "", lambda n: False)["message"].startswith("Finish the sign-in")
    assert service.complete("google", "s", "c", "access_denied", "", ok) == {"sync": "error", "message": "access_denied"}
    assert service.complete("google", "s", "c", "x", "Denied by user", ok)["message"] == "Denied by user"
    assert service.complete("microsoft", "s", "c", "", "", ok)["message"] == "Provider mismatch"
    repo.users.clear()
    assert service.complete("google", "s", "c", "", "", ok)["message"] == "User not found"

    def bad_state(s):
        raise conn_mod.account_sync.SyncError("expired")
    monkeypatch.setattr(conn_mod.account_sync, "read_state", bad_state)
    assert service.complete("google", "s", "c", "", "", ok) == {"sync": "error", "message": "expired"}


def test_connection_callback_sync_error(connections, monkeypatch):
    service, uow, repo, calls = connections
    monkeypatch.setattr(conn_mod.account_sync, "read_state", lambda s: {"provider": "GOOGLE", "user_id": "me", "nonce": "n"})

    def failing(db, user, provider, code):
        raise conn_mod.account_sync.SyncError("Token request failed (400)")
    monkeypatch.setattr(conn_mod.account_sync, "complete", failing)
    assert service.complete("google", "s", "c", "", "", lambda n: True)["message"] == "Token request failed (400)"


# ── AI generation ───────────────────────────────────────────────────

class Personas:
    def __init__(self):
        self.contacts = {("t1", "p1"): SimpleNamespace(prospect_id="p1", tenant_id="t1")}
        self.types = {"OTHER": SimpleNamespace(persona_type="OTHER")}
        self.added = []

    def contact(self, tenant_id, prospect_id):
        return self.contacts.get((tenant_id, prospect_id))

    def blueprints(self, active_only):
        return list(self.types.values()) if active_only else []

    def blueprint_for_type(self, persona_type):
        return self.types.get(persona_type)

    def add(self, entity):
        self.added.append(entity)
        return entity

    def refresh(self, entity):
        pass


@pytest.fixture
def ai(monkeypatch):
    repo = Personas()
    uow = Uow(personas=repo)
    monkeypatch.setattr(ai_mod, "get_or_create_prospect_persona", lambda db, p: SimpleNamespace(
        persona_type="OTHER", confidence_score=0.5, classification_method="RULE_BASED"))
    return AiGenerationService(uow), uow, repo


def test_ai_classify_batch_skips_other_tenants(ai):
    service, uow, repo = ai
    assert service.classify_batch(["p1", "p2"], ME) == [{"prospect_id": "p1", "persona_type": "OTHER",
                                                          "confidence_score": 0.5, "classification_method": "RULE_BASED"}]


def test_ai_generate_email(ai, monkeypatch):
    service, uow, repo = ai
    seen = []
    monkeypatch.setattr(ai_mod, "generate_email_for_prospect", lambda db, p, name, link: seen.append((name, link)) or {"subject": "s"})
    assert service.generate_email("p1", "X", " none ", ME) == {"subject": "s"}
    assert seen == [("X", "")]  # placeholder CTA links are normalised away
    with pytest.raises(NotFoundError):
        service.generate_email("p2", "X", "", ME)
    monkeypatch.setattr(ai_mod, "generate_email_for_prospect", lambda db, p, name, link: None)
    with pytest.raises(BadRequestError):
        service.generate_email("p1", "X", "", ME)


def test_ai_generate_sequence(ai, monkeypatch):
    service, uow, repo = ai
    results = [{"email_1": {}}, {"error": "No blueprint found"}]

    class Gen:
        def __init__(self, db):
            pass

        def generate_email_sequence(self, **kw):
            return results.pop(0)
    monkeypatch.setattr(ai_mod, "SequenceGeneratorService", Gen)
    assert service.generate_sequence("p1", "X", "", False, "", ME) == {"email_1": {}}
    with pytest.raises(BadRequestError):
        service.generate_sequence("p1", "X", "", False, "", ME)


def test_ai_blueprints(ai):
    service, uow, repo = ai
    assert service.blueprints(True) == [repo.types["OTHER"]] and service.blueprints(False) == []
    assert service.blueprint("other") is repo.types["OTHER"]
    with pytest.raises(NotFoundError):
        service.blueprint("missing")
    with pytest.raises(BadRequestError):
        service.create_blueprint(SimpleNamespace(persona_type="OTHER"))
    data = SimpleNamespace(persona_type="NEW", openers=["o"], value_angles=[], ctas=[], proof_points=[], tone_rules={})
    created = service.create_blueprint(data)
    assert created.persona_type == "NEW" and repo.added == [created] and uow.commits == 1
