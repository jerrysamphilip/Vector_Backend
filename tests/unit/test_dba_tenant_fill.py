"""
D3 / C-22 unit: app/models/tenant_fill.py fills a new child row's tenant_id from its parent and orders a
primary-company switch; the models and alembic/versions/0028_child_tenant_ids.py agree. Fakes only, no database.
"""
import importlib.util
from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

import app.models  # noqa: F401  (registers every table and the listener)
from app.models import tenant_fill as tf
from app.models.base import Base
from app.models.campaign import Campaign, CampaignProspect, CampaignStateEvent
from app.models.crm import PRIMARY_KEY_SQL, ContactCompany
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0028_child_tenant_ids.py"


class Lookup:
    """Fake parent lookup: {table: {id: tenant}}; records each call."""

    def __init__(self, data=None):
        self.data, self.calls = data or {}, []

    def __call__(self, table, ids):
        self.calls.append((table, list(ids)))
        return {i: self.data.get(table, {}).get(i) for i in ids}


def _msg(**kw):
    return EmailMessage(to_email="x@example.com", **kw)


def test_dba_depth_orders_parents_first():
    assert tf.depth("campaigns") == 0
    assert tf.depth("email_messages") == 1 and tf.depth("email_template_versions") == 2
    assert tf.depth("email_events") == 2


def test_dba_parent_object_set_through_the_relationship():
    campaign = Campaign(tenant_id="t1")
    row = CampaignStateEvent(campaign=campaign, to_state="ACTIVE")
    lookup = Lookup()
    assert tf.fill_tenant_ids([row], lookup) == []
    assert row.tenant_id == "t1" and lookup.calls == []


def test_dba_message_and_its_event_in_one_flush():
    msg = _msg(prospect_id="p1")
    ev = EmailEvent(message=msg, event_type=2)
    lookup = Lookup({"prospects": {"p1": "t1"}})
    assert tf.fill_tenant_ids([ev, msg], lookup) == []
    assert (msg.tenant_id, ev.tenant_id) == ("t1", "t1")
    assert lookup.calls == [("prospects", ["p1"])]


def test_dba_parent_by_key_among_known_objects():
    known = Campaign(campaign_id="c1", tenant_id="t2")
    row = CampaignProspect(campaign_id="c1", prospect_id="p1")
    pending_msg = _msg(message_id="m1", prospect_id="p1", tenant_id="t3")
    ev = EmailEvent(message_id="m1", event_type=1)
    lookup = Lookup()
    assert tf.fill_tenant_ids([row, ev], lookup, known=[known, pending_msg, Prospect(prospect_id="p1")]) == []
    assert row.tenant_id == "t2" and ev.tenant_id == "t3" and lookup.calls == []


def test_dba_one_lookup_per_parent_table_and_missing_rows():
    rows = [_msg(prospect_id="p2"), _msg(prospect_id="p1"), _msg(prospect_id="p1"), _msg(prospect_id="gone")]
    lookup = Lookup({"prospects": {"p1": "t1", "p2": "t2"}})
    missing = tf.fill_tenant_ids(rows, lookup)
    assert lookup.calls == [("prospects", ["gone", "p1", "p2"])]
    assert [r.tenant_id for r in rows] == ["t2", "t1", "t1", None] and missing == [rows[3]]


def test_dba_child_without_a_relationship_uses_its_key():
    from app.models.sales_extra import ProposalLine

    line = ProposalLine(proposal_id="q1", description="Seats")
    assert tf.fill_tenant_ids([line], Lookup({"proposals": {"q1": "t4"}})) == [] and line.tenant_id == "t4"


def test_dba_set_tenant_and_other_rows_are_left_alone():
    set_row = _msg(prospect_id="p1", tenant_id="mine")
    no_fk = _msg()
    other = Campaign(tenant_id=None)
    lookup = Lookup({"prospects": {"p1": "t1"}})
    assert tf.fill_tenant_ids([set_row, other], lookup) == []
    assert set_row.tenant_id == "mine" and other.tenant_id is None and lookup.calls == []
    assert tf.fill_tenant_ids([no_fk], lookup) == [no_fk] and lookup.calls == []


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeSession:
    """What fill_for_session / _before_flush / _demote_first use of a Session."""

    def __init__(self, new=(), loaded=(), rows=(), dirty=(), deleted=()):
        self.new, self.dirty, self.deleted = list(new), list(dirty), list(deleted)
        self.identity_map = {i: o for i, o in enumerate(loaded)}
        self.rows, self.statements = list(rows), []
        self.no_autoflush = nullcontext()

    def execute(self, statement):
        self.statements.append(statement)
        return FakeResult(self.rows)


def test_dba_fill_for_session_queries_the_database_once(caplog):
    rows = [CampaignProspect(campaign_id="c1", prospect_id="p"), CampaignProspect(campaign_id="c9", prospect_id="p")]
    session = FakeSession(new=rows, rows=[("c1", "t1")])
    missing = tf.fill_for_session(session, rows)
    assert rows[0].tenant_id == "t1" and missing == [rows[1]]
    assert len(session.statements) == 1 and "campaigns" in str(session.statements[0])
    assert "No parent tenant for campaign_prospects (campaigns 'c9')" in caplog.text


def test_dba_fill_for_session_without_children_does_nothing():
    session = FakeSession(new=[Campaign(tenant_id="t")])
    assert tf.fill_for_session(session, session.new) == [] and session.statements == []


def test_dba_before_flush_fills_new_rows_from_loaded_parents():
    row = CampaignProspect(campaign_id="c1", prospect_id="p")
    session = FakeSession(new=[row], loaded=[Campaign(campaign_id="c1", tenant_id="t5")])
    tf._before_flush(session, None, None)
    assert row.tenant_id == "t5" and session.statements == []


def test_dba_listener_is_registered_once():
    assert event.contains(Session, "before_flush", tf._before_flush)
    tf.register()
    assert event.contains(Session, "before_flush", tf._before_flush)


def _stored(link_id, prospect_id, primary):
    """A contact_companies row as loaded from the database."""
    row = ContactCompany()
    for key, value in (("link_id", link_id), ("prospect_id", prospect_id), ("is_primary", primary),
                       ("tenant_id", "t"), ("account_id", "a-" + link_id)):
        set_committed_value(row, key, value)
    return row


def test_dba_primary_switch_writes_the_demotion_first():
    old, new_primary = _stored("l1", "p1", True), _stored("l2", "p1", False)
    old.is_primary, new_primary.is_primary = False, True
    assert tf.primary_switch_demotions([], [old, new_primary], []) == ["l1"]
    replaced = _stored("l3", "p2", True)  # deleted while a new primary row is inserted
    added = ContactCompany(prospect_id="p2", account_id="b", tenant_id="t", is_primary=True)
    assert tf.primary_switch_demotions([added], [], [replaced]) == ["l3"]
    session = FakeSession(new=[added], deleted=[replaced])
    tf._demote_first(session)
    assert len(session.statements) == 1 and "UPDATE contact_companies" in str(session.statements[0])


def test_dba_no_primary_switch_no_statement():
    secondary = _stored("l4", "p3", False)
    secondary.label = "Billing"
    gone = _stored("l5", "p4", True)  # deleted, but nothing becomes primary for p4
    other = _stored("l6", "p9", True)
    other.is_primary = False
    still = _stored("l7", "p8", True)
    still.label = "unchanged primary"
    assert tf.primary_switch_demotions([], [secondary, other, still], [gone]) == []
    promoted = ContactCompany(prospect_id="p5", account_id="x", tenant_id="t", is_primary=True)
    assert tf.primary_switch_demotions([promoted], [other], []) == []  # another contact's demotion
    session = FakeSession(dirty=[secondary], deleted=[gone])
    tf._demote_first(session)
    assert session.statements == []


def test_dba_moved_primary_row_counts_as_promoted():
    moved = _stored("l8", "p6", True)
    moved.prospect_id = "p7"  # a merge moves a primary row to the survivor
    demoted = _stored("l9", "p7", True)
    demoted.is_primary = False
    assert tf.primary_switch_demotions([], [moved, demoted], []) == ["l9"]


def _migration():
    spec = importlib.util.spec_from_file_location("m0028", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dba_migration_matches_the_models():
    m = _migration()
    assert m.revision == "0028_child_tenant_ids" and m.down_revision == "0027_campaign_edit"
    assert {c: (fk, parent) for c, (_pk, fk, parent, _ppk, _ix) in m.CHILDREN.items()} == tf.TENANT_PARENTS
    order = list(m.CHILDREN)
    for child, (pk, fk, parent, parent_pk, indexes) in m.CHILDREN.items():
        table = Base.metadata.tables[child]
        assert not table.c.tenant_id.nullable, child
        assert [c.name for c in table.primary_key.columns] == [pk], child
        assert table.c[fk].references(Base.metadata.tables[parent].c[parent_pk]), child
        assert "tenant_id" in Base.metadata.tables[parent].c, parent
        if parent in m.CHILDREN:
            assert order.index(parent) < order.index(child), child
        model_ix = {ix.name: ", ".join(c.name for c in ix.columns) for ix in table.indexes}
        for name, cols in indexes.items():
            assert model_ix.get(name) == cols, (child, name)
        cols = [c.name for c in table.columns]
        assert cols[cols.index("tenant_id") - 1] == m.AFTER.get(child, fk), child
    assert m.PRIMARY_KEY_SQL == PRIMARY_KEY_SQL


def test_dba_composite_keys_match_the_models():
    m = _migration()
    for parent, (name, column) in m.PARENT_KEYS.items():
        uniques = {c.name: [col.name for col in c.columns] for c in Base.metadata.tables[parent].constraints
                   if c.name}
        assert uniques.get(name) == ["tenant_id", column], parent
    for name, (child, column, parent, on_delete) in m.COMPOSITE_FKS.items():
        fks = {fk.name: fk for fk in Base.metadata.tables[child].foreign_key_constraints}
        fk = fks[name]
        assert [c.name for c in fk.columns] == ["tenant_id", column]
        assert fk.referred_table.name == parent
        assert (fk.ondelete == "CASCADE") == ("CASCADE" in on_delete)
    indexes = {ix.name: ix for ix in Base.metadata.tables["contact_companies"].indexes}
    for name, cols in m.CHILD_INDEXES["contact_companies"].items():
        assert ", ".join(c.name for c in indexes[name].columns) == cols
    assert indexes[m.PRIMARY_INDEX].unique and [c.name for c in indexes[m.PRIMARY_INDEX].columns] == [m.PRIMARY_COLUMN]


def test_dba_list_member_relationships_join_on_the_id_alone():
    from app.models.prospect_list import ProspectList, ProspectListMember

    for rel in (ProspectListMember.prospect_list, ProspectListMember.prospect, ProspectList.members,
                Prospect.list_memberships):
        assert "tenant_id" not in str(rel.property.primaryjoin)
