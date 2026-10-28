"""
A1 service layer (ADR 0009): AccountService and AccountBackfillService against an in-memory fake of the
``AccountRepository`` Protocol and a fake unit of work. No database: the helpers shared with the contacts
aggregate that still take a session (visibility, amounts, history) are replaced with stubs.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, OperationFailed, PermissionDenied
from app.domain.accounts import AccountFilter
from app.domain.contacts import VisibilityScope
from app.domain.paging import Page, Paging, Sort
from app.models.account import Account
from app.models.account_backfill import AccountBackfillItem, AccountBackfillRun
from app.models.contact_field import ContactFieldDefinition
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.schemas.account_schema import AccountUpdate, AccountWrite
from app.services import account_backfill_service as backfill_mod
from app.services import account_service as svc_mod
from app.services.account_backfill_service import AccountBackfillService
from app.services.account_service import AccountService, propagate_to_contacts

NOW = datetime(2026, 10, 27, 12, 0, 0)
EXPORT_LOG: list = []


# ── Fakes ──────────────────────────────────────────────────────────

class FakeRepo:
    def __init__(self):
        self.accounts, self.contacts, self.runs_by_id = {}, {}, {}
        self.staged, self.purged, self.flushes = [], [], 0
        self.searched = []

    # entities
    def add(self, tenant_id, entity):
        entity.tenant_id = getattr(entity, "tenant_id", None) or tenant_id
        self.staged.append(entity)
        if isinstance(entity, Account):
            entity.account_id = entity.account_id or f"new{len(self.accounts)}"
            self.accounts[entity.account_id] = entity
        if isinstance(entity, AccountBackfillRun):
            entity.run_id = entity.run_id or f"run{len(self.runs_by_id)}"
            self.runs_by_id[entity.run_id] = entity
        if isinstance(entity, AccountBackfillItem):
            entity.item_id = f"i{len(self.staged):05d}"
        return entity

    def flush(self):
        self.flushes += 1

    def remove(self, entity):
        self.staged.remove(entity)

    def of(self, kind):
        return [e for e in self.staged if isinstance(e, kind)]

    # companies
    def live(self, tenant_id, account_id):
        a = self.accounts.get(account_id)
        return a if a is not None and a.tenant_id == tenant_id and a.deleted_at is None else None

    def deleted(self, tenant_id, account_id):
        a = self.accounts.get(account_id)
        return a if a is not None and a.tenant_id == tenant_id and a.deleted_at is not None else None

    def search(self, criteria, sort, paging):
        self.searched.append((criteria, sort, paging))
        rows = [(a, 0) for a in self.accounts.values() if a.tenant_id == criteria.tenant_id]
        return Page(rows, len(rows), paging)

    def count(self, criteria):
        return self.total if hasattr(self, "total") else len(self.accounts)

    def export_chunk(self, criteria, offset, limit):
        return [(a, 3) for a in list(self.accounts.values())[offset:offset + limit]]

    def name_taken(self, tenant_id, name, except_id=None):
        return any(a.name == name and a.account_id != except_id for a in self.accounts.values())

    def domain_taken(self, tenant_id, domain, except_id=None, live_only=False):
        return any(a.domain == domain and a.account_id != except_id and (not live_only or a.deleted_at is None)
                   for a in self.accounts.values())

    def live_by(self, tenant_id, *, domain=None, name=None):
        for a in self.accounts.values():
            if a.deleted_at is None and (domain is None or a.domain == domain) and (name is None or a.name == name):
                return a
        return None

    def user_in_tenant(self, tenant_id, user_id):
        return user_id in ("u1", "u2")

    def users_by_ids(self, ids):
        return {}

    def primary_contacts(self, tenant_id, account_id):
        return [p for p in self.contacts.values() if p.account_id == account_id]

    # backfill
    def unlinked_contacts(self, tenant_id, scope):
        return [(p.prospect_id, p.email, p.company_name, p.industry, p.emp_band)
                for p in sorted(self.contacts.values(), key=lambda p: p.prospect_id)
                if p.account_id is None and p.deleted_at is None]

    def company_index_rows(self, tenant_id):
        live = [(a.account_id, a.name, a.domain) for a in self.accounts.values() if a.deleted_at is None]
        deleted = [(a.name, a.domain) for a in self.accounts.values() if a.deleted_at is not None]
        return live, deleted

    def unlinked_by_ids(self, tenant_id, ids):
        return [p for p in self.contacts.values() if p.prospect_id in ids and p.account_id is None]

    def contacts_by_ids(self, tenant_id, ids):
        return {p.prospect_id: p for p in self.contacts.values() if p.prospect_id in ids}

    def accounts_by_ids(self, tenant_id, ids):
        return {k: a for k, a in self.accounts.items() if k in ids}

    def running_run(self, tenant_id, since):
        return next((r for r in self.runs_by_id.values() if r.status == "RUNNING" and r.started_at > since), None)

    def run(self, tenant_id, run_id):
        r = self.runs_by_id.get(run_id)
        return r if r is not None and r.tenant_id == tenant_id else None

    def runs(self, tenant_id, started_by, limit):
        return list(self.runs_by_id.values())[:limit]

    def run_items(self, run_id, kinds, offset, limit):
        items = [i for i in self.of(AccountBackfillItem) if i.run_id == run_id and i.kind in kinds]
        return items[offset:offset + limit]

    def account_in_use(self, account_id):
        return any(p.account_id == account_id for p in self.contacts.values())

    def account_edited(self, account_id):
        return False

    def purge_created(self, account):
        self.purged.append(account.account_id)
        del self.accounts[account.account_id]


class FakeUow:
    def __init__(self, repo):
        self.accounts, self.session = repo, None
        self.commits = self.rollbacks = self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def savepoint(self):
        return nullcontext()


def user(role="ADMIN", user_id="u1", tenant_id="t1", **kw):
    base = dict(role=role, user_id=user_id, tenant_id=tenant_id, custom_permissions=None, sales_level=None,
                first_name="Ann", last_name="Lee", email="ann@x.io")
    base.update(kw)
    return SimpleNamespace(**base)


def account(account_id, name, domain=None, tenant_id="t1", **kw):
    a = Account(account_id=account_id, tenant_id=tenant_id, name=name, domain=domain, row_version=1, **kw)
    return a


def contact(pid, email, company_name=None, account_id=None, **kw):
    return Prospect(prospect_id=pid, tenant_id="t1", email=email, company_name=company_name, account_id=account_id,
                    deleted_at=None, row_version=1, **kw)


@pytest.fixture(autouse=True)
def stub_session_helpers(monkeypatch):
    """The helpers shared with the contacts aggregate that take a session."""
    for mod in (svc_mod, backfill_mod):
        monkeypatch.setattr(mod, "visibility_scope", lambda db, u: VisibilityScope())
    monkeypatch.setattr(svc_mod, "can_see_amounts", lambda db, u: True)
    monkeypatch.setattr(svc_mod, "masked_for", lambda db, u, data: data)
    monkeypatch.setattr(svc_mod, "can_see_account", lambda db, u, a: a.tenant_id == u.tenant_id)
    monkeypatch.setattr(svc_mod, "can_see_owner", lambda db, u, owner: True)
    monkeypatch.setattr(svc_mod, "clean_custom_fields", lambda db, t, values, object_type: dict(values))
    monkeypatch.setattr(svc_mod, "required_missing", lambda db, t, record, object_type: [])
    monkeypatch.setattr(svc_mod, "definitions_in_order", lambda db, t, object_type: [
        ContactFieldDefinition(field_id="f1", field_key="tier", label="Tier", field_type="TEXT", object_type=object_type)])
    monkeypatch.setattr(svc_mod, "claim_version", lambda db, obj, expected, record: 2)
    monkeypatch.setattr(svc_mod, "record_export", lambda uow, *args: EXPORT_LOG.append(args))
    monkeypatch.setattr(svc_mod.crm, "record_changes", lambda *a, **k: 0)
    monkeypatch.setattr(backfill_mod.contact_companies, "sync_primary", lambda db, p, uid=None: None)


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def uow(repo):
    return FakeUow(repo)


# ── AccountService ─────────────────────────────────────────────────

def test_list_page_falls_back_to_name_sort_when_amounts_are_hidden(monkeypatch, repo, uow):
    monkeypatch.setattr(svc_mod, "can_see_amounts", lambda db, u: False)
    repo.accounts["a1"] = account("a1", "Acme", custom_fields={"tier": "Gold"})
    result = AccountService(uow).list_page(user(), AccountFilter(owner="me"), Sort("annual_revenue"), Paging(1, 25))
    criteria, sort, _ = repo.searched[0]
    assert sort.field == "name" and criteria.owner_id == "u1" and not criteria.archived
    assert result["items"][0]["custom_fields"] == {"tier": "Gold"} and result["total"] == 1
    assert [(p["field_key"], p["object_type"]) for p in result["custom_properties"]] == [("tier", "COMPANY")]


def test_list_archived_needs_manage_permission(uow):
    with pytest.raises(PermissionDenied):
        AccountService(uow).list_page(user("AGENT", custom_permissions=[]), AccountFilter(status="archived"),
                                      Sort("name"), Paging())


def test_list_rejects_bad_filter_json(uow):
    with pytest.raises(BadRequestError):
        AccountService(uow).list_page(user(), AccountFilter(filters='{"field": "email", "operator": "is"}'),
                                      Sort("name"), Paging())


def test_create_validates_in_order_and_commits_once(repo, uow):
    service = AccountService(uow)
    with pytest.raises(BadRequestError):
        service.create(user(), AccountWrite(phone="1"))
    repo.accounts["a1"] = account("a1", "Acme")
    with pytest.raises(ConflictError):
        service.create(user(), AccountWrite(name="Acme"))
    with pytest.raises(PermissionDenied):
        service.create(user("AGENT", custom_permissions=[]), AccountWrite(name="Other", owner_id="u2"))
    assert uow.commits == 0
    created = service.create(user(), AccountWrite(website="https://www.initech.com/about",
                                                  custom_fields={"tier": "Gold"}))
    assert created["name"] == "Initech" and created["domain"] == "initech.com"
    assert created["custom_fields"] == {"tier": "Gold"} and uow.commits == 1
    history = repo.of(PropertyChange)
    assert [(h.object_type, h.field) for h in history] == [("COMPANY", "created")]


def test_create_reports_missing_required_properties(monkeypatch, uow):
    monkeypatch.setattr(svc_mod, "required_missing", lambda db, t, record, object_type: ["Tier"])
    with pytest.raises(BadRequestError, match="Required: Tier"):
        AccountService(uow).create(user(), AccountWrite(name="Acme"))
    assert uow.commits == 0


def test_update_rename_conflict_and_permissions(monkeypatch, repo, uow):
    repo.accounts["a1"] = account("a1", "Acme", owner_id="u9")
    repo.accounts["a2"] = account("a2", "Globex")
    service = AccountService(uow)
    with pytest.raises(NotFoundError):
        service.update(user(), "nope", AccountUpdate(name="X"), None)
    with pytest.raises(ConflictError):
        service.update(user(), "a1", AccountUpdate(name="Globex"), None)
    monkeypatch.setattr(svc_mod, "can_edit_account", lambda db, u, a: False)
    with pytest.raises(PermissionDenied):
        service.update(user(), "a1", AccountUpdate(name="Acme 2"), None)
    assert uow.commits == 0


def test_update_propagates_name_and_industry_with_history(monkeypatch, repo, uow):
    monkeypatch.setattr(AccountService, "_record", lambda self, ctx, u, account_id: {"account_id": account_id})
    repo.accounts["a1"] = account("a1", "Acme", industry="SaaS")
    repo.contacts["p1"] = contact("p1", "a@acme.com", "Acme", "a1", industry="SaaS")
    repo.contacts["p2"] = contact("p2", "b@acme.com", "acme inc", "a1", industry="Banking")
    repo.contacts["p3"] = contact("p3", "c@other.com", "Other", "a9", industry="SaaS")
    AccountService(uow).update(user(), "a1", AccountUpdate(name="Acme Labs", industry="Fintech",
                                                           custom_fields={"tier": "Gold"}), 1)
    assert repo.accounts["a1"].custom_fields == {"tier": "Gold"}
    p1, p2, p3 = repo.contacts["p1"], repo.contacts["p2"], repo.contacts["p3"]
    assert (p1.company_name, p1.industry) == ("Acme Labs", "Fintech")
    assert (p2.company_name, p2.industry) == ("Acme Labs", "Banking")  # industry set on purpose is kept
    assert (p3.company_name, p3.industry) == ("Other", "SaaS")
    changes = sorted((c.object_id, c.field, c.old_value, c.new_value, c.source) for c in repo.of(PropertyChange))
    assert changes == [("p1", "company_name", "Acme", "Acme Labs", "COMPANY"),
                       ("p1", "industry", "SaaS", "Fintech", "COMPANY"),
                       ("p2", "company_name", "acme inc", "Acme Labs", "COMPANY")]
    assert uow.commits == 1


def test_propagate_without_relevant_change_touches_nothing(repo):
    a = account("a1", "Acme")
    repo.contacts["p1"] = contact("p1", "a@acme.com", "Acme", "a1")
    before = {"name": "Acme", "industry": None, "emp_band": None, "phone": "1"}
    assert propagate_to_contacts(repo, a, before, {**before, "phone": "2"}, "u1") == 0
    assert repo.staged == []


def test_delete_rules(repo, uow):
    repo.accounts["a1"] = account("a1", "Acme")
    service = AccountService(uow)
    with pytest.raises(PermissionDenied):
        service.delete(user("AGENT"), "a1")
    with pytest.raises(PermissionDenied):
        service.delete(user("MANAGER", custom_permissions=[]), "a1")
    assert service.delete(user(), "a1")["status"] == "deleted"
    assert repo.accounts["a1"].deleted_at is not None and uow.commits == 1


def test_restore_refuses_when_the_domain_is_taken(repo, uow):
    repo.accounts["a1"] = account("a1", "Acme", "acme.com", deleted_at=NOW)
    repo.accounts["a2"] = account("a2", "Acme New", "acme.com")
    with pytest.raises(ConflictError):
        AccountService(uow).restore(user(), "a1")
    with pytest.raises(NotFoundError):
        AccountService(uow).restore(user(), "a2")


def test_export_permission_limit_and_formula_guard(repo, uow):
    service = AccountService(uow)
    with pytest.raises(PermissionDenied):
        service.export(user("AGENT"), "csv", None, AccountFilter())
    repo.accounts["a1"] = account("a1", "=HYPERLINK(\"x\")", custom_fields={"tier": "+1"})
    export = service.export(user(), "csv", "name,contact_count", AccountFilter())
    text = export.content.decode("utf-8-sig").splitlines()
    assert text[0] == "Company,Contacts,Tier"
    assert text[1].startswith("\"'=HYPERLINK") and text[1].endswith(",3,'+1")
    assert export.filename.startswith("companies-") and export.media_type == "text/csv"
    tenant_id, user_id, object_type, summary, rows, fmt = EXPORT_LOG[-1]
    assert (tenant_id, user_id, object_type, rows, fmt) == ("t1", "u1", "COMPANY", 1, "csv")
    assert '"columns": "name,contact_count"' in summary
    repo.total = 100_001
    with pytest.raises(BadRequestError, match="at most 100,000"):
        service.export(user(), "xlsx", None, AccountFilter())


def test_export_drops_revenue_for_users_who_cannot_see_amounts(monkeypatch, repo, uow):
    monkeypatch.setattr(svc_mod, "can_see_amounts", lambda db, u: False)
    repo.accounts["a1"] = account("a1", "Acme", annual_revenue=5)
    export = AccountService(uow).export(user(), "csv", "name,annual_revenue", AccountFilter())
    assert export.content.decode("utf-8-sig").splitlines()[0] == "Company,Tier"


# ── AccountBackfillService ─────────────────────────────────────────

def _seed_backfill(repo):
    repo.accounts["a1"] = account("a1", "Acme", "acme.com")
    repo.contacts["p1"] = contact("p1", "x@acme.com", None)
    repo.contacts["p2"] = contact("p2", "y@initech.com", "Initech")
    repo.contacts["p3"] = contact("p3", "z@gmail.com", None)


def test_preview_writes_nothing(repo, uow):
    _seed_backfill(repo)
    result = AccountBackfillService(uow, clock=lambda: NOW).preview(user())
    assert result["stats"]["contacts_to_link"] == 2 and result["stats"]["companies_to_create"] == 1
    assert result["stats"]["contacts_skipped"] == 1 and uow.commits == 0 and repo.staged == []


def test_backfill_needs_manage_permission(uow):
    with pytest.raises(PermissionDenied):
        AccountBackfillService(uow).preview(user("AGENT", custom_permissions=[]))
    with pytest.raises(PermissionDenied):
        AccountBackfillService(uow).run(user("AGENT", custom_permissions=[]))


def test_run_records_items_and_undo_reverses_them(repo, uow):
    _seed_backfill(repo)
    service = AccountBackfillService(uow, clock=lambda: NOW)
    run = service.run(user())
    assert run["status"] == "COMPLETED" and run["stats"]["contacts_linked"] == 2
    assert run["stats"]["companies_created"] == 1 and run["stats"]["company_names_filled"] == 1
    assert uow.commits == 3  # run row, one batch, completion
    created = next(a for a in repo.accounts.values() if a.name == "Initech")
    assert repo.contacts["p1"].account_id == "a1" and repo.contacts["p1"].company_name == "Acme"
    assert repo.contacts["p2"].account_id == created.account_id
    kinds = sorted(i.kind for i in repo.of(AccountBackfillItem))
    assert kinds == ["ACCOUNT_CREATED", "CONTACT_FIELD", "CONTACT_LINKED", "CONTACT_LINKED"]

    # someone moves p2 to another company before the undo: that link is kept, so is the company
    repo.contacts["p2"].account_id = "a1"
    undone = service.undo(user(), run["run_id"])
    assert undone["status"] == "UNDONE"
    assert undone["stats"]["undo"] == {"fields_restored": 1, "links_removed": 1, "links_kept": 1,
                                       "companies_removed": 1}
    assert repo.contacts["p1"].account_id is None and repo.contacts["p1"].company_name is None
    assert created.account_id in repo.purged
    with pytest.raises(ConflictError):
        service.undo(user(), run["run_id"])


def test_run_refuses_while_another_is_running(repo, uow):
    repo.add("t1", AccountBackfillRun(tenant_id="t1", status="RUNNING", started_at=NOW - timedelta(minutes=5)))
    with pytest.raises(ConflictError):
        AccountBackfillService(uow, clock=lambda: NOW).run(user())


def test_failed_run_is_recorded(monkeypatch, repo, uow):
    _seed_backfill(repo)

    def boom(self, groups):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backfill_mod._Writer, "apply", boom)
    with pytest.raises(OperationFailed):
        AccountBackfillService(uow, clock=lambda: NOW).run(user())
    run = next(iter(repo.runs_by_id.values()))
    assert run.status == "FAILED" and run.error == "disk full" and uow.rollbacks >= 1


def test_undo_unknown_or_running_run(repo, uow):
    service = AccountBackfillService(uow, clock=lambda: NOW)
    with pytest.raises(NotFoundError):
        service.undo(user(), "nope")
    repo.add("t1", AccountBackfillRun(run_id="r1", tenant_id="t1", status="RUNNING", started_by="u1",
                                      started_at=NOW - timedelta(minutes=1)))
    with pytest.raises(ConflictError):
        service.undo(user(), "r1")


def test_legacy_backfill_keeps_its_response(repo, uow):
    _seed_backfill(repo)
    result = AccountBackfillService(uow, clock=lambda: NOW).legacy(user())
    assert result["status"] == "ok" and result["linked_contacts"] == 2 and result["run_id"]
