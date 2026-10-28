# app/models/tenant_fill.py
"""
Child rows carry their parent's tenant_id without every writer setting it (chunk D3, alembic 0028_child_tenant_ids).

One ``before_flush`` listener on every ``Session`` fills ``tenant_id`` on a NEW row of a table in
:data:`TENANT_PARENTS` when it is still empty, from the parent the row points at: the parent object when the
relationship was set (``EmailEvent(message=msg)``), else the parent with that key in the session (new or
loaded), else one query per parent table for all rows of the flush. Parents that are children themselves
(an e-mail message for its events, a template for its versions) are filled first, so a message and its events
written in one flush both get the tenant. A tenant_id the writer did set is left alone.

A row whose parent cannot be found keeps ``tenant_id`` NULL and the database refuses it (NOT NULL), as it
refuses a row whose foreign key points nowhere. Writes that bypass the unit of work (Core ``insert()``,
``bulk_save_objects``, raw SQL) are not seen here: they set tenant_id themselves (:func:`fill_for_session` serves
``bulk_save_objects`` callers).

The same listener orders one thing in the flush for the one-primary-company key (C-22,
ux_contact_companies_one_primary): when a flush makes a contact_companies row primary and also demotes or
deletes the contact's previous primary row, the demotion is written first. The unit of work would otherwise
write the new primary before it removes the old one (inserts and updates go before deletes, updates in key
order) and the unique key would refuse the switch. Nothing the writer did not stage is changed.

Lives in app.models (not app.repositories) because app.models imports it at start-up and a models ->
repositories import would close a package cycle (.importlinter no-cycles contract).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Mapper, Session

from app.models.base import Base
from app.models.crm import ContactCompany

logger = logging.getLogger(__name__)

# child table -> (foreign key column, parent table): where the child's tenant comes from. Literal copy in
# alembic/versions/0028_child_tenant_ids.py (the migration's backfill follows the same paths).
TENANT_PARENTS: dict[str, tuple[str, str]] = {
    "email_messages": ("prospect_id", "prospects"),
    "email_events": ("message_id", "email_messages"),
    "campaign_prospects": ("campaign_id", "campaigns"),
    "campaign_state_events": ("campaign_id", "campaigns"),
    "email_sequences": ("campaign_id", "campaigns"),
    "email_templates": ("campaign_id", "campaigns"),
    "email_template_versions": ("template_id", "email_templates"),
    "email_attachments": ("template_id", "email_templates"),
    "compliance_checks": ("template_id", "email_templates"),
    "automation_rules": ("campaign_id", "campaigns"),
    "prospect_list_members": ("list_id", "prospect_lists"),
    "proposal_lines": ("proposal_id", "proposals"),
    "campaign_group_members": ("group_id", "campaign_groups"),
    "prospect_personas": ("prospect_id", "prospects"),
}

# (parent table, parent ids) -> {parent id: tenant_id} for the parents not in the session
Lookup = Callable[[str, Sequence[str]], Mapping[str, Optional[str]]]


def depth(table: str) -> int:
    """How many child links lie between the table and a table that owns its tenant_id (email_events: 2)."""
    parent = TENANT_PARENTS.get(table)
    return 0 if parent is None else 1 + depth(parent[1])


def _relationship_key(mapper: Mapper[Any], fk_column: str) -> Optional[str]:
    """The many-to-one relationship of the child that the foreign key column belongs to."""
    for rel in mapper.relationships:
        if rel.direction.name == "MANYTOONE" and {c.key for c in rel.local_columns} == {fk_column}:
            return rel.key
    return None


def _table(obj: Any) -> Optional[str]:
    table = getattr(type(obj), "__table__", None)
    return getattr(table, "name", None)


def _pk(obj: Any) -> Optional[str]:
    """The single-column primary key value an object already has (None before its default runs)."""
    table = type(obj).__table__
    cols = list(table.primary_key.columns)
    return obj.__dict__.get(cols[0].key) if len(cols) == 1 else None


def _parent_object(obj: Any, fk_column: str) -> Any:
    """The parent set through the relationship (without loading anything)."""
    key = _relationship_key(inspect(type(obj)), fk_column)
    return obj.__dict__.get(key) if key else None


def fill_tenant_ids(objects: Iterable[Any], lookup: Lookup, known: Iterable[Any] = ()) -> list[Any]:
    """
    Set tenant_id on every object of a TENANT_PARENTS table that has none. ``known`` are other objects
    (the session's new and loaded ones) that may be the parents; ``lookup`` answers for the rest.
    Returns the objects left without a tenant (no parent found).
    """
    todo = [o for o in objects if _table(o) in TENANT_PARENTS and getattr(o, "tenant_id", None) is None]
    if not todo:
        return []
    parents = {TENANT_PARENTS[_table(o) or ""][1] for o in todo}
    by_key: dict[tuple[str, str], Any] = {}
    for o in [*known, *todo]:
        table = _table(o)
        pk = _pk(o) if table in parents else None
        if table and pk:
            by_key[(table, pk)] = o
    missing: list[Any] = []
    for level in sorted({depth(_table(o) or "") for o in todo}):
        missing += _fill_level([o for o in todo if depth(_table(o) or "") == level], by_key, lookup)
    return missing


def _from_session(obj: Any, by_key: Mapping[tuple[str, str], Any]) -> Optional[str]:
    fk_column, parent_table = TENANT_PARENTS[_table(obj) or ""]
    parent = _parent_object(obj, fk_column)
    if parent is None:
        fk = obj.__dict__.get(fk_column)
        parent = by_key.get((parent_table, fk)) if fk else None
    return getattr(parent, "tenant_id", None) if parent is not None else None


def _fill_level(objs: Sequence[Any], by_key: Mapping[tuple[str, str], Any], lookup: Lookup) -> list[Any]:
    wanted: dict[str, set[str]] = defaultdict(set)
    for o in objs:
        o.tenant_id = _from_session(o, by_key)
        fk_column, parent_table = TENANT_PARENTS[_table(o) or ""]
        if o.tenant_id is None and o.__dict__.get(fk_column):
            wanted[parent_table].add(o.__dict__[fk_column])
    found = {table: lookup(table, sorted(ids)) for table, ids in wanted.items()}
    missing = []
    for o in objs:
        if o.tenant_id is None:
            fk_column, parent_table = TENANT_PARENTS[_table(o) or ""]
            o.tenant_id = found.get(parent_table, {}).get(o.__dict__.get(fk_column) or "")
        if o.tenant_id is None:
            missing.append(o)
    return missing


def session_lookup(session: Session) -> Lookup:
    """Parents' tenant_id straight from the database (one query per parent table, no autoflush)."""
    def lookup(table: str, ids: Sequence[str]) -> Mapping[str, Optional[str]]:
        t = Base.metadata.tables[table]
        pk = list(t.primary_key.columns)[0]
        with session.no_autoflush:
            rows = session.execute(select(pk, t.c.tenant_id).where(pk.in_(list(ids)))).all()
        return {str(k): v for k, v in rows}
    return lookup


def fill_for_session(session: Session, objects: Iterable[Any]) -> list[Any]:
    """Fill the objects' tenant_id from the session and the database; returns the ones left empty.
    Also for ``bulk_save_objects`` callers, whose rows never reach the flush listener."""
    objects = list(objects)
    if not any(_table(o) in TENANT_PARENTS for o in objects):
        return []
    known = [*session.new, *session.identity_map.values()]
    missing = fill_tenant_ids(objects, session_lookup(session), known)
    for o in missing:
        logger.warning("[TENANT_FILL] No parent tenant for %s (%s); the insert will be refused",
                       _table(o), _parent_ref(o))
    return missing


def _parent_ref(obj: Any) -> str:
    fk_column, parent_table = TENANT_PARENTS[_table(obj) or ""]
    return f"{parent_table} {obj.__dict__.get(fk_column)!r}"


# ── One primary company per contact (C-22) ─────────────────────────

def _old(obj: Any, attr: str) -> Any:
    """The value the database holds for the attribute (before this flush)."""
    hist = inspect(obj).attrs[attr].history
    if hist.deleted:
        return hist.deleted[0]
    return hist.unchanged[0] if hist.unchanged else None


def primary_switch_demotions(new: Iterable[Any], dirty: Iterable[Any], deleted: Iterable[Any]) -> list[str]:
    """link_ids of stored primary rows this flush demotes or deletes, for contacts that get a new primary row."""
    links = [o for o in [*new, *dirty] if isinstance(o, ContactCompany)]
    promoted = {o.prospect_id for o in links if o.is_primary and (o in new or not _old(o, "is_primary")
                                                                    or _old(o, "prospect_id") != o.prospect_id)}
    if not promoted:
        return []
    leaving = [o for o in links if o not in new and _old(o, "is_primary") and not o.is_primary]
    leaving += [o for o in deleted if isinstance(o, ContactCompany) and _old(o, "is_primary")]
    return sorted(o.link_id for o in leaving if _old(o, "prospect_id") in promoted)


def _demote_first(session: Session) -> None:
    ids = primary_switch_demotions(list(session.new), list(session.dirty), list(session.deleted))
    if ids:
        with session.no_autoflush:
            session.execute(update(ContactCompany.__table__).where(
                ContactCompany.__table__.c.link_id.in_(ids)).values(is_primary=False))


def _before_flush(session: Session, _context: Any, _instances: Any) -> None:
    fill_for_session(session, session.new)
    _demote_first(session)


def register() -> None:
    """Listen on every Session (idempotent). Runs on import: app/models/__init__.py imports this module."""
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)


register()
