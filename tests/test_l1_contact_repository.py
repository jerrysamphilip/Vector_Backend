"""
L1 repository layer (ADR 0009) against MySQL: SqlAlchemyContactRepository's filters (incl. the
AND/OR tree and lists), visibility scope, paging, sort, tenant isolation and merge.
"""
import uuid

import pytest

from app.core.database import SessionLocal
from app.domain.contacts import ContactCriteria, ListScope, VisibilityScope, parse_filter_tree, search_terms
from app.domain.paging import Paging, Sort
from app.models.account import Account
from app.models.contact_activity import ContactActivity
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.repositories.contacts import SqlAlchemyContactRepository, SqlAlchemyContactsUnitOfWork
from app.services.contact_service import merge_into
from tests.conftest import register_tenant, sql

TAG = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def world(client):
    one, two = register_tenant(client, "l1r"), register_tenant(client, "l1s")
    owner = sql("SELECT user_id FROM users WHERE email = :e", e=one["email"])
    db = SessionLocal()
    acme = Account(tenant_id=one["tenant_id"], name=f"Acme {TAG}", domain=f"acme{TAG}.io", annual_revenue=500)
    db.add(acme)
    db.flush()

    def add(tenant, n, **kw):
        p = Prospect(tenant_id=tenant, email=f"{n}-{TAG}@example.com", **kw)
        db.add(p)
        return p
    t1 = one["tenant_id"]
    ann = add(t1, "ann", first_name="Ann", last_name="Archer", owner_id=owner, tags=["vip"], score=90,
              score_grade="A", phone="+1 737 555 0188", custom_fields={"seats": 12}, account_id=acme.account_id)
    bob = add(t1, "bob", first_name="Bob", last_name="Baker", owner_id=owner, score=40, score_grade="C",
              lifecycle_stage="CUSTOMER")
    cy = add(t1, "cy", first_name="Cy", last_name="Cole", owner_id=None, tags=["vip", "new"], score=70, score_grade="B")
    gone = add(t1, "gone", first_name="Gone", owner_id=owner)
    other = add(two["tenant_id"], "ann", first_name="Ann", last_name="Other", tags=["vip"])
    db.flush()
    from app.core.clock import utcnow
    gone.deleted_at = utcnow()
    static = ProspectList(tenant_id=t1, list_name=f"Static {TAG}", source_type="MANUAL", list_type="STATIC",
                          uploaded_by=owner)
    db.add(static)
    db.flush()
    db.add(ProspectListMember(list_id=static.list_id, prospect_id=bob.prospect_id, is_new_prospect=False))
    db.commit()
    ids = {k: p.prospect_id for k, p in dict(ann=ann, bob=bob, cy=cy, gone=gone, other=other).items()}
    yield SimpleWorld(db, t1, two["tenant_id"], owner, ids, static.list_id, acme.account_id)
    db.close()


class SimpleWorld:
    def __init__(self, db, t1, t2, owner, ids, list_id, account_id):
        self.db, self.t1, self.t2, self.owner, self.ids = db, t1, t2, owner, ids
        self.list_id, self.account_id = list_id, account_id

    def repo(self):
        return SqlAlchemyContactRepository(self.db)

    def names(self, criteria, sort=Sort.parse("name"), paging=Paging(1, 50)):
        page = self.repo().search(criteria, sort, paging)
        return [p.first_name for p in page.items], page.total


def tree(spec, me="me"):
    return parse_filter_tree(spec, me, lambda: True)


def test_live_contacts_of_the_tenant_only_sorted_and_paged(world):
    c = ContactCriteria(tenant_id=world.t1)
    assert world.names(c) == (["Ann", "Bob", "Cy"], 3)
    assert world.names(c, Sort.parse("-score")) == (["Ann", "Cy", "Bob"], 3)
    assert world.names(c, Sort.parse("score"), Paging(2, 2)) == (["Ann"], 3)
    assert world.repo().get_live(world.t2, world.ids["ann"]) is None
    assert world.repo().get_live(world.t1, world.ids["gone"]) is None
    assert world.repo().get_live(world.t1, world.ids["ann"]).first_name == "Ann"


def test_visibility_scope(world):
    own = VisibilityScope(frozenset({world.owner}))
    assert world.names(ContactCriteria(tenant_id=world.t1, visibility=own)) == (["Ann", "Bob"], 2)
    picks_up = VisibilityScope(frozenset({world.owner}), include_unassigned=True)
    assert world.names(ContactCriteria(tenant_id=world.t1, visibility=picks_up))[1] == 3
    assert world.names(ContactCriteria(tenant_id=world.t1, visibility=VisibilityScope(frozenset({"nobody"})))) == ([], 0)
    assert world.names(ContactCriteria(tenant_id=world.t1, unassigned=True)) == (["Cy"], 1)
    assert world.names(ContactCriteria(tenant_id=world.t1, owner_id=world.owner))[1] == 2


def test_search_uses_the_window_count(world):
    assert world.names(ContactCriteria(tenant_id=world.t1, search=search_terms("archer"))) == (["Ann"], 1)
    assert world.names(ContactCriteria(tenant_id=world.t1, search=search_terms("(737) 555-0188"))) == (["Ann"], 1)
    assert world.names(ContactCriteria(tenant_id=world.t1, search=search_terms("zzz-none"))) == ([], 0)


def test_simple_filters(world):
    t1 = world.t1
    assert world.names(ContactCriteria(tenant_id=t1, tag="new"))[0] == ["Cy"]
    assert world.names(ContactCriteria(tenant_id=t1, has_phone=True))[0] == ["Ann"]
    assert world.names(ContactCriteria(tenant_id=t1, has_phone=False))[0] == ["Bob", "Cy"]
    assert world.names(ContactCriteria(tenant_id=t1, grades=("A", "B")))[0] == ["Ann", "Cy"]
    assert world.names(ContactCriteria(tenant_id=t1, score_min=50, score_max=80))[0] == ["Cy"]
    assert world.names(ContactCriteria(tenant_id=t1, lifecycle_stage="CUSTOMER"))[0] == ["Bob"]
    assert world.names(ContactCriteria(tenant_id=t1, account_id=world.account_id))[0] == ["Ann"]


def test_lists_static_and_active(world):
    t1 = world.t1
    static = ListScope(world.list_id, t1)
    assert world.names(ContactCriteria(tenant_id=t1, in_list=static))[0] == ["Bob"]
    active = ListScope("any", t1, active=True, tree=tree({"field": "tags", "operator": "is", "value": "vip"}))
    assert world.names(ContactCriteria(tenant_id=t1, in_list=active))[0] == ["Ann", "Cy"]
    assert world.repo().in_list(static, world.ids["bob"]) and not world.repo().in_list(static, world.ids["ann"])


@pytest.mark.parametrize("spec,expected", [
    ({"field": "tags", "operator": "is_not", "value": "vip"}, ["Bob"]),
    ({"field": "custom.seats", "operator": "gte", "value": 10}, ["Ann"]),
    ({"field": "company.annual_revenue", "operator": "gt", "value": 100}, ["Ann"]),
    ({"field": "company.name", "operator": "contains", "value": "acme"}, ["Ann"]),
    ({"field": "first_name", "operator": "starts_with", "value": "b"}, ["Bob"]),
    ({"field": "lifecycle_stage", "operator": "is_empty"}, ["Ann", "Cy"]),
    ({"field": "created_at", "operator": "in_last_days", "value": 1}, ["Ann", "Bob", "Cy"]),
    ({"op": "OR", "conditions": [{"field": "first_name", "operator": "is", "value": "ann"},
                                 {"field": "last_name", "operator": "in", "value": ["Cole"]}]}, ["Ann", "Cy"]),
    ({"op": "AND", "conditions": [{"field": "tags", "operator": "contains", "value": "vip"},
                                  {"field": "owner_id", "operator": "is_not_empty"}]}, ["Ann"]),
])
def test_filter_tree(world, spec, expected):
    assert world.names(ContactCriteria(tenant_id=world.t1, tree=tree(spec)))[0] == expected


def test_filter_tree_me_and_list(world):
    mine = tree({"field": "owner_id", "operator": "is", "value": "me"}, me=world.owner)
    assert world.names(ContactCriteria(tenant_id=world.t1, tree=mine))[0] == ["Ann", "Bob"]
    in_static = tree({"field": "list", "operator": "not_in", "value": [world.list_id]})
    assert world.names(ContactCriteria(tenant_id=world.t1, tree=in_static))[0] == ["Ann", "Cy"]


def test_board_counts_and_lanes(world):
    c = ContactCriteria(tenant_id=world.t1)
    assert world.repo().count_by(c, "lifecycle_stage") == {"CUSTOMER": 1, None: 2}
    assert sorted(p.first_name for p in world.repo().lane(c, "lifecycle_stage", None, 10)) == ["Ann", "Cy"]
    assert [p.first_name for p in world.repo().lane(c, "lifecycle_stage", "CUSTOMER", 10)] == ["Bob"]


def test_merge_moves_records_and_deletes_the_duplicate(world):
    db = SessionLocal()
    try:
        t1 = world.t1
        keep = Prospect(tenant_id=t1, email=f"keep-{TAG}@example.com", first_name="Keep", owner_id=world.owner)
        dup = Prospect(tenant_id=t1, email=f"dup-{TAG}@example.com", last_name="Dup", tags=["x"],
                       consent_status="UNSUBSCRIBED")
        db.add_all([keep, dup])
        db.flush()
        db.add(ProspectListMember(list_id=world.list_id, prospect_id=dup.prospect_id, is_new_prospect=False))
        db.add(ContactActivity(tenant_id=t1, prospect_id=dup.prospect_id, activity_type="CALL", subject="hi"))
        db.commit()
        dup_id = dup.prospect_id
        with SqlAlchemyContactsUnitOfWork.for_session(db) as uow:
            moved = merge_into(uow.contacts, keep, [dup], world.owner, {"email": dup_id})
            uow.commit()
        assert moved == {"lists": 1, "campaigns": 0, "emails": 0, "activities": 1}
        db.refresh(keep)
        assert keep.email == f"dup-{TAG}@example.com" and keep.last_name == "Dup" and keep.tags == ["x"]
        assert keep.consent_status == "UNSUBSCRIBED"
        assert sql("SELECT COUNT(*) FROM prospects WHERE prospect_id = :p", p=dup_id) == 0
        assert sql("SELECT COUNT(*) FROM prospect_list_members WHERE prospect_id = :p", p=keep.prospect_id) == 1
        assert sql("SELECT COUNT(*) FROM contact_activities WHERE prospect_id = :p", p=keep.prospect_id) == 2
    finally:
        db.close()
