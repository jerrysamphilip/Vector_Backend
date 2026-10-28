"""
a1b service layer (chunk A1, lists): ListService and ProspectListService against an in-memory fake of
``ListRepository`` and a fake unit of work. Pins permissions, validation order, the unique list name (C-21),
the paged / unpaged GET /lists shapes and commits, without SQL.
"""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.lists import ListFilter, ProspectListFilter
from app.domain.paging import Page, Paging
from app.services.list_service import ListService
from app.services.prospect_list_service import ProspectListService, revalidate_prospect


class FakeLists:
    def __init__(self):
        self.lists, self.members, self.staged, self.removed, self.prospects = {}, {}, [], [], {}
        self.page_calls = []

    def get(self, tenant_id, entity_id):
        plist = self.lists.get(entity_id)
        return plist if plist is not None and plist.tenant_id == tenant_id else None

    def add(self, tenant_id, entity):
        entity.tenant_id = tenant_id
        entity.list_id = entity.list_id or f"L{len(self.lists) + 1}"
        self.lists[entity.list_id] = entity
        return entity

    def stage(self, entity):
        self.staged.append(entity)
        self.members.setdefault(entity.list_id, set()).add(entity.prospect_id)
        return entity

    def remove(self, entity):
        self.removed.append(entity)
        self.lists.pop(entity.list_id, None)

    def flush(self):
        pass

    def name_taken(self, tenant_id, name, except_id=None):
        return any(lst.tenant_id == tenant_id and lst.list_name.lower() == name.lower() and lst.list_id != except_id
                   for lst in self.lists.values())

    def search(self, tenant_id, f, limit):
        self.page_calls.append(("search", f, limit))
        return [lst for lst in self.lists.values() if lst.tenant_id == tenant_id][:limit]

    def page(self, tenant_id, f, paging):
        self.page_calls.append(("page", f, paging))
        rows = [lst for lst in self.lists.values() if lst.tenant_id == tenant_id]
        return Page(items=rows[paging.offset:paging.offset + paging.limit], total=len(rows), paging=paging)

    def user_names(self, ids):
        return {i: "Ann Lee" for i in ids}

    def visible_member_count(self, scope, visibility):
        return len(self.members.get(scope.list_id, ()))

    def live_prospect_ids(self, tenant_id, ids):
        return {i for i in ids if i in self.prospects}

    def member_ids(self, list_id, ids):
        return {i for i in ids if i in self.members.get(list_id, set())}

    def remove_members(self, list_id, ids):
        before = self.members.get(list_id, set())
        gone = before & set(ids)
        self.members[list_id] = before - gone
        return len(gone)

    def delete_members(self, list_id):
        self.members.pop(list_id, None)

    def upload_page(self, tenant_id, f, paging):
        self.page_calls.append(("upload", f, paging))
        rows = [lst for lst in self.lists.values() if lst.tenant_id == tenant_id]
        return Page(items=rows, total=len(rows), paging=paging)

    def static_live_count(self, list_id):
        return len(self.members.get(list_id, ()))

    def all_member_ids(self, list_id):
        return sorted(self.members.get(list_id, ()))

    def ids_in_other_lists(self, ids, list_id):
        return {i for lid, m in self.members.items() if lid != list_id for i in m if i in ids}

    def ids_with_activity(self, ids):
        return {i for i in ids if self.prospects.get(i, {}).get("activity")}

    def hard_delete_prospects(self, ids):
        self.hard_deleted = sorted(ids)

    def membership(self, list_id, prospect_id):
        return getattr(self, "member_row", None) if prospect_id in self.members.get(list_id, ()) else None

    def prospect_stats(self, tenant_id):
        return 10, 4, 6, 1


class FakeUow:
    def __init__(self, repo):
        self.lists, self.session = repo, None
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


def user(role="ADMIN", **kw):
    base = dict(role=role, user_id="u1", tenant_id="t1", custom_permissions=None, sales_level=None,
                first_name="Ann", last_name="Lee")
    base.update(kw)
    return SimpleNamespace(**base)


def plist(list_id, name, tenant_id="t1", list_type="STATIC"):
    return SimpleNamespace(list_id=list_id, tenant_id=tenant_id, list_name=name, list_type=list_type,
                           source_type="MANUAL", filters=None, description=None, uploaded_by="u1",
                           uploaded_at=None)


@pytest.fixture
def repo():
    return FakeLists()


@pytest.fixture
def uow(repo):
    return FakeUow(repo)


def test_create_checks_permission_name_type_and_uniqueness(repo, uow):
    svc = ListService(uow)
    with pytest.raises(PermissionDenied, match="manage_prospects"):
        svc.create(user("AGENT", custom_permissions=[]), "X", None, None, None)
    with pytest.raises(BadRequestError, match="Give the list a name"):
        svc.create(user(), "  ", None, None, None)
    with pytest.raises(BadRequestError, match="STATIC or ACTIVE"):
        svc.create(user(), "X", "smart", None, None)
    with pytest.raises(BadRequestError, match="at least one filter"):
        svc.create(user(), "X", "active", {"op": "AND", "conditions": []}, None)
    out = svc.create(user(), "  Hot leads ", None, {"ignored": True}, " notes ")
    assert out["list_name"] == "Hot leads" and out["list_type"] == "STATIC" and out["filters"] is None
    assert out["description"] == "notes" and out["member_count"] == 0 and uow.commits == 1
    with pytest.raises(ConflictError) as exc:
        svc.create(user(), "HOT LEADS", None, None, None)
    assert exc.value.status_code == 409 and exc.value.detail["field"] == "list_name"
    assert uow.commits == 1


def test_rename_to_a_taken_name_is_409_but_keeping_its_own_name_is_fine(repo, uow):
    repo.lists = {"L1": plist("L1", "A"), "L2": plist("L2", "B"), "X": plist("X", "B", tenant_id="t2")}
    svc = ListService(uow)
    with pytest.raises(ConflictError, match="already exists"):
        svc.update(user(), "L1", {"list_name": "b"})
    assert svc.update(user(), "L1", {"list_name": " a ", "description": "d"})["list_name"] == "a"
    with pytest.raises(BadRequestError, match="Only active lists have filters"):
        svc.update(user(), "L1", {"filters": {"conditions": [1]}})
    with pytest.raises(NotFoundError):
        svc.update(user(), "X", {"description": "other tenant"})


def test_list_without_paging_keeps_the_old_shape_with_paging_adds_totals(repo, uow):
    repo.lists = {f"L{i}": plist(f"L{i}", f"N{i}") for i in range(3)}
    svc = ListService(uow)
    old = svc.list(user(), ListFilter(list_type="STATIC"))
    assert set(old) == {"items"} and len(old["items"]) == 3
    assert repo.page_calls[-1] == ("search", ListFilter(list_type="STATIC"), 500)
    paged = svc.list(user(), ListFilter(), Paging(2, 2))
    assert paged["total"] == 3 and paged["page"] == 2 and paged["page_size"] == 2 and len(paged["items"]) == 1
    assert paged["items"][0]["created_by_name"] == "Ann Lee"


def test_members_static_only_and_counts(repo, uow):
    repo.lists = {"L1": plist("L1", "S"), "A1": plist("A1", "Act", list_type="ACTIVE")}
    repo.prospects = {"p1": {}, "p2": {}, "p3": {}}
    repo.members = {"L1": {"p1"}}
    svc = ListService(uow)
    out = svc.add_members(user(), "L1", ["p1", "p2", "p2", "nope"])
    assert out == {"added": 1, "already_in_list": 1, "not_found": 1}
    assert [m.prospect_id for m in repo.staged] == ["p2"] and uow.commits == 1
    with pytest.raises(BadRequestError, match="update automatically"):
        svc.add_members(user(), "A1", ["p1"])
    assert svc.remove_members(user(), "L1", ["p1", "p3"]) == {"removed": 1}
    assert svc.delete(user(), "L1") == {"status": "deleted", "list_id": "L1"} and "L1" not in repo.lists


def test_prospect_list_delete_keeps_contacts_in_other_lists_or_with_history(repo, uow):
    repo.lists = {"U": plist("U", "Upload"), "O": plist("O", "Other")}
    repo.members = {"U": {"a", "b", "c"}, "O": {"b"}}
    repo.prospects = {"c": {"activity": True}}
    out = ProspectListService(uow).delete(user(), "U")
    assert out == {"status": "deleted", "list_id": "U", "deleted_prospects": 1}
    assert repo.hard_deleted == ["a"] and uow.commits == 1


def test_prospect_list_page_and_stats(repo, uow):
    repo.lists = {"U": plist("U", "Upload")}
    repo.members = {"U": {"a", "b"}}
    svc = ProspectListService(uow)
    out = svc.page(user(), ProspectListFilter(search="up"), Paging(1, 20))
    assert out["items"][0]["prospect_count"] == 2 and out["total"] == 1
    assert repo.page_calls[-1][1].search == "up"
    assert svc.stats(user()) == {"total": 10, "available": 4, "active": 6, "opted_out": 1}


def test_update_note_needs_membership(repo, uow):
    repo.lists = {"U": plist("U", "Upload")}
    repo.members = {"U": {"a"}}
    repo.member_row = SimpleNamespace(notes=None)
    svc = ProspectListService(uow)
    with pytest.raises(NotFoundError, match="not found in this list"):
        svc.update_note(user(), "U", "zzz", "hi")
    assert svc.update_note(user(), "U", "a", "hi")["status"] == "updated" and repo.member_row.notes == "hi"


def p(**kw):
    base = dict(prospect_id="p", email="ann@acme.com", email_type=None, email_provider=None, poc_state=None,
                timezone=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_revalidate_prospect_rules():
    outcome, updated = revalidate_prospect(p(email="bad"), {}, set())
    assert outcome.status == "invalid" and "Invalid email format" in outcome.issues and not updated
    outcome, updated = revalidate_prospect(p(email="ann@gmail.com", poc_state="Texas"), {}, set())
    assert outcome.status == "warning" and "Personal email domain detected" in outcome.issues and updated
    outcome, _ = revalidate_prospect(p(email="Ann@acme.com"), {"ann@acme.com": "bounce"}, {"ann@acme.com"})
    assert outcome.status == "invalid"
    assert outcome.issues[-3:] == ["Globally unsubscribed (bounce)", "Duplicate email in list", "Missing POC State"]
