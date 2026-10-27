"""
L1 service layer (ADR 0009): the contact services against an in-memory fake of the
``ContactRepository`` Protocol and a fake unit of work. No database: these tests pin the business
rules (permissions, validation order, merge rules, company links, commits) independently of SQL.
"""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.contacts import ContactFilter, FilterGroup, TaskFilter, VisibilityScope
from app.domain.paging import Page, Paging, Sort
from app.schemas.contact_schema import FieldWrite
from app.schemas.saved_view_schema import ViewWrite
from app.services import contact_companies
from app.services import contact_service as rules
from app.services.contact_field_service import ContactFieldService
from app.services.contact_query_service import ContactQueryService, contact_criteria
from app.services.saved_view_service import SavedViewService
from app.services.task_service import TaskService


# ── Fakes ──────────────────────────────────────────────────────────

class FakeRepo:
    """Just enough of ContactRepository for the services under test; records what was staged."""

    def __init__(self):
        self.contacts, self.lists, self.fields, self.views, self.links = {}, {}, {}, {}, []
        self.members, self.enrollments, self.personas = {}, {}, {}
        self.staged, self.removed, self.flushes, self.deleted_ids = [], [], 0, []
        self.search_calls = []

    # entities
    def add(self, tenant_id, entity):
        entity.tenant_id = getattr(entity, "tenant_id", None) or tenant_id
        self.staged.append(entity)
        return entity

    def stage(self, entity):
        self.staged.append(entity)
        if hasattr(entity, "link_id") or type(entity).__name__ == "ContactCompany":
            self.links.append(entity)
        return entity

    def remove(self, entity):
        self.removed.append(entity)
        if entity in self.links:
            self.links.remove(entity)

    def flush(self):
        self.flushes += 1

    # contacts
    def get_live(self, tenant_id, prospect_id):
        p = self.contacts.get(prospect_id)
        return p if p is not None and p.tenant_id == tenant_id and p.deleted_at is None else None

    def list_in_tenant(self, tenant_id, list_id):
        lst = self.lists.get(list_id)
        return lst if lst is not None and lst.tenant_id == tenant_id else None

    def search(self, criteria, sort, paging):
        self.search_calls.append((criteria, sort, paging))
        rows = [p for p in self.contacts.values() if p.tenant_id == criteria.tenant_id]
        return Page(items=rows, total=len(rows), paging=paging)

    def users_by_ids(self, ids):
        return {}

    def live_account_names(self, ids):
        return {}

    def activity_touches(self, ids):
        return []

    def outbound_sends(self, ids):
        return []

    # fields
    def field_definitions(self, tenant_id, order="none"):
        return [f for f in self.fields.values() if f.tenant_id == tenant_id]

    def field_label_taken(self, tenant_id, label):
        return any(f.label.lower() == label.lower() for f in self.field_definitions(tenant_id))

    def field_key_taken(self, tenant_id, key):
        return any(f.field_key == key for f in self.field_definitions(tenant_id)) or any(
            getattr(s, "field_key", None) == key for s in self.staged)

    def count_fields(self, tenant_id):
        return len(self.field_definitions(tenant_id))

    def field_definition(self, tenant_id, field_id):
        f = self.fields.get(field_id)
        return f if f is not None and f.tenant_id == tenant_id else None

    def delete_field_definition(self, tenant_id, field_id):
        return 1 if self.fields.pop(field_id, None) is not None else 0

    # companies
    def links_of(self, prospect_id):
        return [r for r in self.links if r.prospect_id == prospect_id]

    def linked_account_ids(self, prospect_id, except_account=None):
        return [r.account_id for r in self.links_of(prospect_id) if r.account_id != except_account]

    # merge
    def memberships(self, prospect_id):
        return [m for m in self.members.values() if m.prospect_id == prospect_id]

    def enrollment_rows(self, prospect_id):
        return [e for e in self.enrollments.values() if e.prospect_id == prospect_id]

    def persona(self, prospect_id):
        return self.personas.get(prospect_id)

    def reassign_children(self, source_id, target_id):
        return 2, 1

    def repoint_merged(self, source_id, target_id):
        pass

    def delete_rows(self, ids):
        self.deleted_ids.extend(ids)

    # views
    def view(self, tenant_id, view_id):
        v = self.views.get(view_id)
        return v if v is not None and v.tenant_id == tenant_id else None

    def views_list(self):
        return list(self.views.values())


class FakeUow:
    def __init__(self, repo):
        self.contacts, self.session = repo, None
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


FILL = ("phone", "mobile_phone", "designation", "industry", "emp_band", "linkedin_url", "poc_city", "poc_state",
        "poc_country", "timezone", "lifecycle_stage", "lead_status", "lead_source", "legal_basis")


def contact(pid, tenant_id="t1", **kw):
    base = dict({f: None for f in FILL}, prospect_id=pid, tenant_id=tenant_id, deleted_at=None, owner_id="u1", first_name=None,
                last_name=None, email=f"{pid}@x.io", tags=None, custom_fields=None, consent_status="OPT_IN",
                account_id=None, company_name=None, row_version=1)
    base.update(kw)
    p = SimpleNamespace(**base)
    p.full_name = " ".join(x for x in (p.first_name, p.last_name) if x) or "Unknown"
    return p


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def uow(repo):
    return FakeUow(repo)


# ── Shared rules ───────────────────────────────────────────────────

def test_manage_and_scope_rules():
    assert rules.can_manage_contacts(user("SUPER_ADMIN"))
    assert not rules.can_manage_contacts(user("PLATFORM_ADMIN"))
    assert not rules.can_manage_contacts(user("AGENT", custom_permissions=["view_analytics"]))
    with pytest.raises(PermissionDenied) as exc:
        rules.require_manager(user("AGENT", custom_permissions=[]))
    assert exc.value.detail == rules.NO_MANAGE_PERMISSION
    # Admins see everyone without touching the database (no session needed)
    assert rules.visibility_scope(None, user("ADMIN")) == VisibilityScope()
    cached = user("AGENT", custom_permissions=["manage_prospects"], sales_level=3)
    cached._visible_user_ids = {"u1", "u2"}
    assert rules.visibility_scope(None, cached) == VisibilityScope(frozenset({"u1", "u2"}), include_unassigned=True)
    assert rules.can_access_contact(user("ADMIN"), contact("p1", owner_id="someone"))


# ── Criteria (filter resolution order) ─────────────────────────────

def test_criteria_resolves_owner_search_and_scope(uow):
    c = contact_criteria(uow, user(), ContactFilter(q=" Ann ", owner="me", grade="a,b", score_min=10))
    assert c.tenant_id == "t1" and c.owner_id == "u1" and not c.unassigned
    assert c.search == ("ann",) and c.grades == ("A", "B") and c.score_min == 10
    assert c.visibility.everyone and c.tree is None and c.in_list is None


def test_criteria_unknown_list_is_404_before_a_bad_grade(uow):
    with pytest.raises(NotFoundError, match="List not found"):
        contact_criteria(uow, user(), ContactFilter(list_id="missing", grade="z", filters="{bad"))
    with pytest.raises(BadRequestError, match="grade must be A, B, C or D"):
        contact_criteria(uow, user(), ContactFilter(grade="z", filters="{bad"))
    with pytest.raises(BadRequestError, match="filters must be JSON"):
        contact_criteria(uow, user(), ContactFilter(filters="{bad"))


def test_criteria_static_and_active_lists(repo, uow):
    repo.lists["s"] = SimpleNamespace(list_id="s", tenant_id="t1", list_type="STATIC", filters=None)
    repo.lists["a"] = SimpleNamespace(list_id="a", tenant_id="t1", list_type="ACTIVE", filters={
        "op": "OR", "conditions": [{"field": "owner_id", "operator": "is", "value": "me"},
                                   {"field": "email", "operator": "contains", "value": "x"}]})
    repo.lists["other"] = SimpleNamespace(list_id="other", tenant_id="t2", list_type="STATIC", filters=None)
    static = contact_criteria(uow, user(), ContactFilter(list_id="s")).in_list
    assert static.list_id == "s" and not static.active and static.tree is None
    active = contact_criteria(uow, user(), ContactFilter(list_id="a")).in_list
    assert active.active and isinstance(active.tree, FilterGroup) and active.tree.children[0].value == "u1"
    with pytest.raises(NotFoundError):  # another tenant's list
        contact_criteria(uow, user(), ContactFilter(list_id="other"))


def test_list_page_passes_sort_and_paging_down(repo, uow):
    from app.models.prospect import Prospect
    repo.contacts["p1"] = Prospect(prospect_id="p1", tenant_id="t1", email="p1@x.io", row_version=1)
    svc = ContactQueryService(uow)
    sort, paging = Sort.parse("-score"), Paging(2, 10)
    out = svc.list_page(user(), ContactFilter(owner="team"), sort, paging)
    assert out["total"] == 1 and out["page"] == 2 and out["page_size"] == 10
    assert out["items"][0]["prospect_id"] == "p1"
    criteria, got_sort, got_paging = repo.search_calls[0]
    assert got_sort is sort and got_paging is paging and criteria.owner_id is None
    assert uow.entered == 1 and uow.commits == 0  # reads never commit


def test_manager_only_reads_refuse_before_touching_storage(uow):
    svc = ContactQueryService(uow)
    agent = user("AGENT", custom_permissions=[])
    with pytest.raises(PermissionDenied):
        svc.deleted_page(agent, None, Paging())
    with pytest.raises(PermissionDenied):
        svc.duplicates(agent, 10)
    with pytest.raises(PermissionDenied, match="Only admins and managers can export"):
        svc.export(user("AGENT"), "csv", None, ContactFilter())
    assert uow.entered == 1  # export enters, then refuses; deleted/duplicates refuse first


def test_detail_of_a_missing_or_other_tenant_contact_is_404(repo, uow):
    repo.contacts["p1"] = contact("p1", tenant_id="t2")
    with pytest.raises(NotFoundError, match="Contact not found"):
        ContactQueryService(uow).detail(user(), "p1")
    assert uow.rollbacks == 1


# ── Custom properties ──────────────────────────────────────────────

def test_field_service_create_update_delete(repo, uow):
    svc = ContactFieldService(uow)
    created = svc.create(user(), FieldWrite(label=" Seats ", field_type="number", sort_order=4))
    assert created["field_key"] == "seats" and created["label"] == "Seats" and created["field_type"] == "NUMBER"
    assert created["sort_order"] == 4 and uow.commits == 1
    definition = repo.staged[-1]
    repo.fields[definition.field_id or "f1"] = definition
    definition.field_id = definition.field_id or "f1"
    with pytest.raises(ConflictError, match="A property called seats already exists"):
        svc.create(user(), FieldWrite(label="seats"))
    with pytest.raises(BadRequestError, match="A choice property needs at least one option"):
        svc.create(user(), FieldWrite(label="Colour", field_type="SELECT"))
    updated = svc.update(user(), definition.field_id, FieldWrite(label="Licences", field_type="TEXT"))
    assert updated["label"] == "Licences" and updated["field_type"] == "TEXT"
    assert svc.delete(user(), definition.field_id) == {"status": "deleted", "field_id": definition.field_id}
    with pytest.raises(NotFoundError, match="Property not found"):
        svc.delete(user(), definition.field_id)


def test_field_service_needs_manage_permission(uow):
    with pytest.raises(PermissionDenied):
        ContactFieldService(uow).create(user("AGENT", custom_permissions=[]), FieldWrite(label="X"))
    assert uow.entered == 0


# ── Companies (CM-014) ─────────────────────────────────────────────

def link(pid, account_id, primary=False, label=None):
    return SimpleNamespace(link_id=f"{pid}-{account_id}", prospect_id=pid, account_id=account_id,
                           is_primary=primary, label=label)


def test_sync_primary_mirrors_account_id(repo):
    repo.links = [link("p1", "old", primary=True), link("p1", "kept", primary=True, label="Billing")]
    p = contact("p1", account_id="new")
    contact_companies.sync_primary(repo, p, "u1")
    assert [r.account_id for r in repo.removed] == ["old"]
    assert not next(r for r in repo.links if r.account_id == "kept").is_primary
    new = repo.staged[-1]
    assert (new.account_id, new.is_primary, new.created_by) == ("new", True, "u1") and repo.flushes == 1


def test_move_links_on_merge(repo):
    repo.links = [link("dup", "a1"), link("dup", "a2"), link("keep", "a1")]
    contact_companies.move_links(repo, contact("dup"), contact("keep", account_id="a3"))
    assert sorted((r.prospect_id, r.account_id, r.is_primary) for r in repo.links) == [
        ("keep", "a1", False), ("keep", "a2", False)]
    assert [r.account_id for r in repo.removed] == ["a1"]


# ── Merge (BR-CM-33) ───────────────────────────────────────────────

def test_merge_rules(repo):
    primary = contact("keep", first_name="Ann", tags=["VIP"], custom_fields={"size": ""})
    dup = contact("dup", first_name="Annie", last_name="Lee", tags=["vip", "new"], custom_fields={"size": "Big"},
                  consent_status="UNSUBSCRIBED", email="ann.lee@x.io")
    repo.members = {
        "m1": SimpleNamespace(list_id="L1", prospect_id="keep", notes="a"),
        "m2": SimpleNamespace(list_id="L1", prospect_id="dup", notes="b"),
        "m3": SimpleNamespace(list_id="L2", prospect_id="dup", notes=None),
    }
    repo.enrollments = {"e1": SimpleNamespace(campaign_id="C1", prospect_id="dup")}
    moved = rules.merge_into(repo, primary, [dup], "u1", {"email": "dup", "first_name": "dup", "password": "dup"})
    assert moved == {"lists": 1, "campaigns": 1, "emails": 2, "activities": 1}
    assert repo.members["m1"].notes == "a\n\nb" and repo.members["m3"].prospect_id == "keep"
    assert primary.first_name == "Annie" and primary.last_name == "Lee"  # chosen value, then blanks filled
    assert primary.tags == ["VIP", "new"] and primary.custom_fields == {"size": "Big"}
    assert primary.consent_status == "UNSUBSCRIBED"  # never lose an opt-out
    assert primary.email == "ann.lee@x.io" and repo.deleted_ids == ["dup"]
    note = repo.staged[-1]
    assert note.activity_type == "NOTE" and "ann.lee@x.io" in note.body and note.created_by == "u1"


# ── Saved views and tasks ──────────────────────────────────────────

def test_saved_view_rules(repo, uow):
    svc = SavedViewService(uow)
    with pytest.raises(BadRequestError, match="Give the view a name"):
        svc.create(user(), ViewWrite(name="  "))
    with pytest.raises(PermissionDenied, match="Only admins can share views"):
        svc.create(user("AGENT", custom_permissions=[]), ViewWrite(name="Mine", shared=True))
    with pytest.raises(BadRequestError, match="Unknown property: nope"):
        svc.create(user(), ViewWrite(name="V", filters={"field": "nope", "operator": "is", "value": 1}))
    out = svc.create(user(), ViewWrite(name=" Hot ", object_type="contact"))
    assert out["name"] == "Hot" and out["object_type"] == "CONTACT" and out["is_mine"] and uow.commits == 1
    repo.views["v2"] = SimpleNamespace(view_id="v2", tenant_id="t1", owner_id="u9", shared=False, object_type="CONTACT",
                                       name="Private", filters=None, columns=None, sort=None)
    with pytest.raises(NotFoundError, match="View not found"):
        svc.delete(user(), "v2")


class FakeTasks:
    def __init__(self):
        self.calls = []

    def page(self, tenant_id, f, me, visible, now, paging):
        self.calls.append((tenant_id, f, me, visible, paging))
        return Page(items=[], total=0, paging=paging)

    def my_counts(self, tenant_id, me, now):
        return 3, 1, 0


def test_task_list_passes_filter_and_counts(uow):
    tasks = FakeTasks()
    svc = TaskService(uow, tasks=lambda session: tasks)
    out = svc.list(user(), TaskFilter(scope="all", status="ALL"), Paging(1, 50))
    assert out == {"items": [], "total": 0, "page": 1, "page_size": 50,
                   "my_open": 3, "my_overdue": 1, "my_reminders_due": 0}
    tenant_id, f, me, visible, _paging = tasks.calls[0]
    assert (tenant_id, f.scope, me, visible) == ("t1", "all", "u1", None)


def test_task_create_needs_a_title(uow):
    from app.schemas.task_schema import TaskWrite
    with pytest.raises(BadRequestError, match="Give the task a title"):
        TaskService(uow, tasks=lambda s: FakeTasks()).create(user(), TaskWrite(title=" "))
    assert uow.entered == 0
