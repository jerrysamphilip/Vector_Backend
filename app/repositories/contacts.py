"""
Contacts aggregate repository (ADR 0009, chunk L1): every SQL statement about contacts (prospects),
their companies, lists and campaigns membership, activities, custom property definitions, property
history, saved views and the AND/OR filter engine.

Services depend on ``ContactRepository`` (a Protocol, so unit tests hand them an in-memory fake);
``SqlAlchemyContactRepository`` is the production implementation. Reads that start from a tenant take
``tenant_id``; reads of a contact's child rows are keyed by a contact the service already loaded
through a tenant-scoped call. Nothing here commits, decides permissions or applies business rules:
the caller passes the ``VisibilityScope`` it computed, and the service's unit of work commits.

``ContactsUnitOfWork`` is the unit of work services own: ``SqlAlchemyContactsUnitOfWork.for_session(db)``
in a request, ``SqlAlchemyContactsUnitOfWork(SessionLocal)`` in the worker and scripts.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from sqlalchemy import ColumnElement, Numeric, and_, cast, func, not_, or_, text
from sqlalchemy.orm import Query, Session, object_session

from app.core.clock import utcnow
from app.domain.contacts import (
    ContactCriteria,
    FilterCondition,
    FilterError,
    FilterGroup,
    FilterNode,
    ListScope,
    VisibilityScope,
    as_list,
    search_terms,
)
from app.domain.paging import Page, Paging, Sort
from app.models.account import Account
from app.models.audit import AuditLog
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.contact_field import ContactFieldDefinition
from app.models.conversation import Conversation
from app.models.crm import ContactCompany, CrmTask, ImportJob, PropertyChange, SavedView
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.sales import Lead, Opportunity
from app.models.sales_extra import Notification
from app.models.user import User
from app.models.webhook import WebhookDelivery
from app.repositories.base import SqlAlchemyRepository, SqlAlchemyUnitOfWork, TenantId
from app.repositories.duplicates import DuplicateRepository, SqlAlchemyDuplicateRepository
from app.repositories.system_notices import not_system_notice, reply_message
from app.utils import sql_like

__all__ = [
    "ContactRepository", "ContactStats", "ContactsAccess", "ContactsUnitOfWork", "Session",
    "SqlAlchemyContactRepository", "SqlAlchemyContactsUnitOfWork", "borrow", "apply_scope", "company_condition", "compile_filter", "contacts_uow",
    "owner_condition", "search_condition", "session_of", "fulltext_query", "FULLTEXT_TOKEN_SIZE",
]

# app.repositories.duplicates registers the e-mail history flush listener (C-11) on import

SORT_COLUMNS = {
    "name": (Prospect.first_name, Prospect.last_name),
    "email": (Prospect.email,),
    "company": (Prospect.company_name,),
    "lifecycle_stage": (Prospect.lifecycle_stage,),
    "lead_status": (Prospect.lead_status,),
    "created_at": (Prospect.created_at,),
    "updated_at": (Prospect.updated_at,),
    "score": (Prospect.score,),  # BR-CM-43
}

CONTACT_COLUMNS = {
    "first_name": Prospect.first_name, "last_name": Prospect.last_name, "email": Prospect.email,
    "phone": Prospect.phone, "mobile_phone": Prospect.mobile_phone, "designation": Prospect.designation,
    "company_name": Prospect.company_name, "industry": Prospect.industry, "emp_band": Prospect.emp_band,
    "linkedin_url": Prospect.linkedin_url, "poc_city": Prospect.poc_city, "poc_state": Prospect.poc_state,
    "poc_country": Prospect.poc_country, "timezone": Prospect.timezone, "lifecycle_stage": Prospect.lifecycle_stage,
    "lead_status": Prospect.lead_status, "lead_source": Prospect.lead_source, "legal_basis": Prospect.legal_basis,
    "owner_id": Prospect.owner_id, "account_id": Prospect.account_id, "consent_status": Prospect.consent_status,
    "is_valid_email": Prospect.is_valid_email, "email_type": Prospect.email_type,
    "created_at": Prospect.created_at, "updated_at": Prospect.updated_at,
}
COMPANY_COLUMNS = {
    "name": Account.name, "domain": Account.domain, "industry": Account.industry, "emp_band": Account.emp_band,
    "country": Account.country, "city": Account.city, "lifecycle_stage": Account.lifecycle_stage,
    "annual_revenue": Account.annual_revenue, "owner_id": Account.owner_id,
}
# Columns the board groups by and the facets list
GROUP_COLUMNS = {"lifecycle_stage": Prospect.lifecycle_stage, "lead_status": Prospect.lead_status}
FACET_COLUMNS = {"poc_country": Prospect.poc_country, "industry": Prospect.industry,
                 "lead_source": Prospect.lead_source}

# Outbound statuses cancelled / paused when a contact is soft-deleted (BR-CM-35)
_CANCEL_ON_DELETE = ["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]
_PAUSE_ON_DELETE = ["ACTIVE", "OPENED", "QUEUED"]
# Rows that reference a contact and go with it on erasure
_ERASED_CHILDREN = (EmailMessage, Conversation, ProspectPersona, CampaignProspect,
                    ProspectListMember, ContactActivity, CrmTask)


@dataclass(frozen=True, slots=True)
class ContactStats:
    """Engagement counts for the record page."""

    emails_sent: int
    last_emailed_at: Optional[datetime]
    replies: int
    last_reply_at: Optional[datetime]
    emails_opened: int
    activity_counts: dict[str, int]
    open_tasks: int


# ── SQL building blocks (also used by the services' legacy ``db`` helpers) ─────────

def session_of(entity: Any) -> Session:
    """The session a loaded entity belongs to."""
    session = object_session(entity)
    if session is None:
        raise RuntimeError("entity is not attached to a session")
    return session


def owner_condition(column: Any, scope: VisibilityScope) -> Optional[ColumnElement[bool]]:
    """SQL condition limiting ``column`` (an owner id) to the scope; None = no limit."""
    if scope.owner_ids is None:
        return None
    clause: ColumnElement[bool] = column.in_(scope.owner_ids)
    if scope.include_unassigned:
        clause = or_(clause, column.is_(None))  # unassigned records can be picked up
    return clause


def apply_scope(query: Query[Any], column: Any, scope: VisibilityScope) -> Query[Any]:
    clause = owner_condition(column, scope)
    return query if clause is None else query.filter(clause)


def search_condition(q: str) -> ColumnElement[bool]:
    """Match name, email, company, title or phone (digits only, formatting ignored); see
    ``app.domain.contacts.search_terms`` for when the digits-only match applies."""
    return _search_terms_condition(search_terms(q))


# Contact search on the FULLTEXT ngram index (D7, migration 0030). MySQL's ngram parser cuts every run of
# word characters into overlapping tokens of ngram_token_size characters (server default 2, which the index
# assumes) and treats everything else (spaces, "@", ".", "-", "+", "'", ...) as a separator.
FULLTEXT_TOKEN_SIZE = 2
_WORD_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lo", "Nd"})


def _is_token_char(ch: str) -> bool:
    """A character the ngram parser certainly indexes. Anything doubtful (marks, symbols, characters
    outside the BMP) counts as a separator: splitting a term more finely only weakens the pre-filter."""
    return ch == "_" or (ord(ch) < 0x10000 and unicodedata.category(ch) in _WORD_CATEGORIES)


def _fulltext_fragments(term: str) -> list[str]:
    """The maximal runs of token characters in ``term`` that are at least one token long."""
    fragments, current = [], []
    for ch in term + " ":
        if _is_token_char(ch):
            current.append(ch)
            continue
        if len(current) >= FULLTEXT_TOKEN_SIZE:
            fragments.append("".join(current))
        current = []
    return list(dict.fromkeys(fragments))


def fulltext_query(terms: Sequence[str]) -> Optional[str]:
    """The BOOLEAN MODE query that every row matching ``LIKE '%term%'`` for one of ``terms`` also matches,
    or None when a term has no fragment of at least FULLTEXT_TOKEN_SIZE characters (the LIKE scan then
    stays the only condition).

    Each fragment becomes a required quoted phrase (``+"smith"``: its tokens next to each other). A
    substring match contains every fragment of the term inside one word of the row, so the MATCH never
    drops a row the LIKE accepts; the LIKE still decides, keeping today's results exactly. Fragments hold
    word characters only, so the boolean operators (``+ - < > ( ) ~ * " @``) typed by a user never reach
    the query: they are separators. Several terms (text and phone digits) are alternatives: ``(...) (...)``."""
    groups = []
    for term in terms:
        fragments = _fulltext_fragments(term)
        if not fragments:
            return None
        groups.append(" ".join(f'+"{f}"' for f in fragments))
    if not groups:
        return None
    return groups[0] if len(groups) == 1 else " ".join(f"({g})" for g in groups)


def _search_terms_condition(terms: Sequence[str]) -> ColumnElement[bool]:
    like = or_(*[sql_like.like(Prospect.search_text, t) for t in terms])
    query = fulltext_query(terms)
    if query is None:  # shorter than one ngram token (e.g. "a", "j s"): the LIKE scan, as before
        return like
    # MATCH narrows the rows through ft_prospects_search_text; LIKE keeps the exact substring semantics
    return and_(Prospect.search_text.match(query), like)


def company_condition(session: Session, account_id: str) -> ColumnElement[bool]:
    """Contacts whose primary company is account_id, or that are associated with it (CM-014)."""
    return or_(Prospect.account_id == account_id,
               Prospect.prospect_id.in_(secondary_contact_ids(session, account_id)))


def secondary_contact_ids(session: Session, account_id: str) -> Query[Any]:
    """Subquery: contacts associated with the company other than as their primary company."""
    return session.query(ContactCompany.prospect_id).filter(
        ContactCompany.account_id == account_id, ContactCompany.is_primary.is_(False))


def _has_tag(tag: Any) -> ColumnElement[bool]:
    return func.json_contains(Prospect.tags, func.json_quote(tag)) == 1


def _compare(column: Any, operator: str, value: Any, is_text: bool = True) -> ColumnElement[bool]:
    """One comparison; ``value`` was normalised by the domain parser (dates, day counts)."""
    if operator == "is":
        return func.lower(column) == str(value).lower() if is_text and isinstance(value, str) else column == value
    if operator == "is_not":
        return or_(column.is_(None), (func.lower(column) != str(value).lower())
                   if is_text and isinstance(value, str) else column != value)
    if operator in ("in", "not_in"):
        return column.in_(as_list(value)) if operator == "in" else or_(column.is_(None), column.notin_(as_list(value)))
    if operator in ("contains", "not_contains", "starts_with"):
        if operator == "starts_with":
            return sql_like.ilike(column, value, "prefix")
        match = sql_like.ilike(column, value)
        return match if operator == "contains" else or_(column.is_(None), not_(match))
    if operator in ("is_empty", "is_not_empty"):
        if operator == "is_empty":
            return or_(column.is_(None), column == "") if is_text else column.is_(None)
        return and_(column.isnot(None), column != "") if is_text else column.isnot(None)
    return _compare_range(column, operator, value)


def _compare_range(column: Any, operator: str, value: Any) -> ColumnElement[bool]:
    if operator in ("gt", "after"):
        return column > value
    if operator == "gte":
        return column >= value
    if operator in ("lt", "before"):
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "between":
        low, high = (as_list(value) + [None, None])[:2]
        return column.between(low, high)
    if operator == "in_last_days":
        return column >= utcnow() - timedelta(days=value)
    raise FilterError(f"Unknown operator: {operator}")


def _tags_clause(operator: str, value: Any) -> ColumnElement[bool]:
    tags = as_list(value)
    if operator in ("is", "in", "contains"):
        return or_(*[_has_tag(t) for t in tags]) if tags else Prospect.tags.isnot(None)
    if operator in ("is_not", "not_in", "not_contains"):
        return or_(Prospect.tags.is_(None), not_(or_(*[_has_tag(t) for t in tags])))
    if operator == "is_empty":
        return or_(Prospect.tags.is_(None), func.json_length(Prospect.tags) == 0)
    return func.json_length(Prospect.tags) > 0


def _custom_clause(cond: FilterCondition) -> ColumnElement[bool]:
    path = f'$."{cond.name}"'
    raw: Any = func.json_unquote(func.json_extract(Prospect.custom_fields, path))
    if cond.operator in ("contains", "is") and isinstance(cond.value, str):  # multi-checkbox values are arrays
        arr = func.json_contains(func.json_extract(Prospect.custom_fields, path), func.json_quote(cond.value)) == 1
        if cond.operator == "is":
            return or_(_compare(raw, cond.operator, cond.value), arr)
        return or_(sql_like.ilike(raw, cond.value), arr)
    if cond.numeric:
        raw = cast(raw, Numeric(18, 4))  # number properties compare numerically, dates as ISO text
    return _compare(raw, cond.operator, cond.value)


def _condition_clause(session: Session, cond: FilterCondition) -> ColumnElement[bool]:
    if cond.kind == "tags":
        return _tags_clause(cond.operator, cond.value)
    if cond.kind == "list":
        members = session.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.list_id.in_(as_list(cond.value)))
        inside = Prospect.prospect_id.in_(members)
        return not_(inside) if cond.operator in ("is_not", "not_in") else inside
    if cond.kind == "custom":
        return _custom_clause(cond)
    if cond.kind == "company":
        companies = session.query(Account.account_id).filter(
            _compare(COMPANY_COLUMNS[cond.name], cond.operator, cond.value, is_text=cond.is_text))
        return Prospect.account_id.in_(companies)
    return _compare(CONTACT_COLUMNS[cond.name], cond.operator, cond.value, is_text=cond.is_text)


def compile_filter(session: Session, node: Optional[FilterNode]) -> Optional[ColumnElement[bool]]:
    """SQL condition over prospects for a parsed AND/OR tree (None = everything)."""
    if node is None:
        return None
    if isinstance(node, FilterGroup):
        parts = [c for c in (compile_filter(session, child) for child in node.children) if c is not None]
        return or_(*parts) if node.op == "OR" else and_(*parts)
    return _condition_clause(session, node)


def list_member_query(session: Session, scope: ListScope) -> Query[Any]:
    """Subquery of contact ids in a list. Active lists are evaluated live from their filters."""
    if scope.active:
        clause = compile_filter(session, scope.tree)
        query = session.query(Prospect.prospect_id).filter(Prospect.tenant_id == scope.tenant_id,
                                                           Prospect.deleted_at.is_(None))
        return query.filter(clause) if clause is not None else query
    return session.query(ProspectListMember.prospect_id).filter(ProspectListMember.list_id == scope.list_id)


# ── Protocol ────────────────────────────────────────────────────────────────

class ContactRepository(Protocol):
    """What the contact services need from storage. ``Prospect`` and friends are ORM objects; changes
    made to them are staged in the unit of work and saved when the service commits."""

    # entities
    def get(self, tenant_id: TenantId, entity_id: str) -> Optional[Prospect]: ...
    def add(self, tenant_id: TenantId, entity: Any) -> Any: ...
    def stage(self, entity: Any) -> Any: ...
    def remove(self, entity: Any) -> None: ...
    def flush(self) -> None: ...
    def refresh(self, entity: Any) -> None: ...
    def touch(self, entity: Any) -> None: ...
    def bump_version(self, prospect: Prospect) -> None: ...

    # visibility
    def manager_pairs(self, tenant_id: TenantId) -> list[tuple[str, Optional[str]]]: ...
    def get_live(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]: ...
    def visible_by_ids(self, tenant_id: TenantId, scope: VisibilityScope, ids: Collection[str]) -> list[Prospect]: ...

    # list / board / export / facets / duplicates
    def search(self, criteria: ContactCriteria, sort: Sort, paging: Paging) -> Page[Prospect]: ...
    def count_by(self, criteria: ContactCriteria, group_by: str) -> dict[Optional[str], int]: ...
    def lane(self, criteria: ContactCriteria, group_by: str, value: Optional[str], limit: int) -> list[Prospect]: ...
    def export_chunk(self, criteria: ContactCriteria, offset: int, limit: int) -> list[Prospect]: ...
    def distinct_values(self, tenant_id: TenantId, scope: VisibilityScope, column: str) -> list[Any]: ...
    def tag_lists(self, tenant_id: TenantId, scope: VisibilityScope) -> list[Any]: ...
    def prospects_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]: ...
    def deleted_page(self, tenant_id: TenantId, scope: VisibilityScope, cutoff: datetime, q: Optional[str],
                     paging: Paging) -> Page[Prospect]: ...
    def list_rows(self, tenant_id: TenantId) -> list[tuple[str, str, str]]: ...
    def campaign_rows(self, tenant_id: TenantId) -> list[tuple[str, str, str]]: ...
    def active_users(self, tenant_id: TenantId) -> list[User]: ...
    def search_contacts(self, tenant_id: TenantId, scope: VisibilityScope, q: str, limit: int) -> list[Prospect]: ...
    def search_companies(self, tenant_id: TenantId, scope: VisibilityScope, term: str, limit: int) -> list[Account]: ...

    # batch lookups for serialisation
    def users_by_ids(self, ids: Collection[str]) -> dict[str, User]: ...
    def live_account_names(self, ids: Collection[str]) -> dict[str, tuple[str, Optional[str]]]: ...
    def account_names(self, ids: Collection[str]) -> dict[str, str]: ...
    def activity_touches(self, ids: Collection[str]) -> list[tuple[str, str, Optional[datetime]]]: ...
    def outbound_sends(self, ids: Collection[str]) -> list[tuple[str, Optional[datetime]]]: ...

    # record page and timeline
    def account_card(self, tenant_id: TenantId, account_id: Optional[str]) -> Optional[tuple[Account, int]]: ...
    def static_memberships(self, prospect_id: str, newest_first: bool) -> list[tuple[ProspectListMember, ProspectList]]: ...
    def active_lists(self, tenant_id: TenantId) -> list[ProspectList]: ...
    def in_list(self, scope: ListScope, prospect_id: str) -> bool: ...
    def enrollments(self, prospect_id: str, newest_first: bool) -> list[tuple[CampaignProspect, Campaign]]: ...
    def persona(self, prospect_id: str) -> Optional[ProspectPersona]: ...
    def stats(self, prospect_id: str) -> ContactStats: ...
    def changes(self, prospect_id: str, field: Optional[str], changed_by: Optional[str],
                limit: int) -> list[PropertyChange]: ...
    def activities_of(self, prospect_id: str, created_by: Optional[str], limit: int) -> list[ContactActivity]: ...
    def tasks_of(self, prospect_id: str, created_by: Optional[str], limit: int) -> list[CrmTask]: ...
    def emails_of(self, prospect_id: str, limit: int) -> list[tuple[EmailMessage, Optional[str]]]: ...
    def email_events_of(self, prospect_id: str, kinds: Collection[str], limit: int) -> list[tuple[EmailEvent, Optional[str]]]: ...

    # write side
    def user_in_tenant(self, tenant_id: TenantId, user_id: str) -> bool: ...
    def live_account(self, tenant_id: TenantId, account_id: str) -> Optional[Account]: ...
    def account(self, tenant_id: TenantId, account_id: str) -> Optional[Account]: ...
    def by_email(self, tenant_id: TenantId, email: str) -> Optional[Prospect]: ...
    def resolve_contact_by_email(self, tenant_id: TenantId, email: str) -> Optional[Prospect]: ...
    def email_taken(self, tenant_id: TenantId, email: str, except_id: str) -> bool: ...
    def is_unsubscribed(self, tenant_id: TenantId, email: str) -> bool: ...
    def retarget_unsent(self, prospect_id: str, email: str, statuses: Collection[str]) -> int: ...
    def list_in_tenant(self, tenant_id: TenantId, list_id: Optional[str]) -> Optional[ProspectList]: ...
    def list_member_ids(self, list_id: str, ids: Collection[str]) -> set[str]: ...
    def remove_list_members(self, list_id: str, ids: Collection[str]) -> int: ...
    def campaign_in_tenant(self, tenant_id: TenantId, campaign_id: Optional[str]) -> Optional[Campaign]: ...
    def deleted_ids(self, tenant_id: TenantId, scope: VisibilityScope, ids: Collection[str]) -> list[str]: ...
    def restorable(self, tenant_id: TenantId, ids: Collection[str], cutoff: datetime,
                   scope: Optional[VisibilityScope]) -> list[Prospect]: ...
    def soft_delete(self, ids: Collection[str], when: datetime, user_id: Optional[str]) -> None: ...

    # companies (CM-014)
    def links_of(self, prospect_id: str) -> list[ContactCompany]: ...
    def link(self, prospect_id: str, account_id: str) -> Optional[ContactCompany]: ...
    def linked_account_ids(self, prospect_id: str, except_account: Optional[str] = None) -> list[str]: ...
    def links_with_accounts(self, prospect: Prospect) -> list[tuple[ContactCompany, Account]]: ...
    def labels_for_company(self, account_id: str, ids: Collection[str]) -> dict[str, str]: ...
    def has_visible_contact_at(self, account_id: str, owner_ids: Collection[str]) -> bool: ...

    # merge and erasure
    @property
    def duplicates(self) -> DuplicateRepository: ...
    def memberships(self, prospect_id: str) -> list[ProspectListMember]: ...
    def enrollment_rows(self, prospect_id: str) -> list[CampaignProspect]: ...
    def reassign_children(self, source_id: str, target_id: str) -> tuple[int, int]: ...
    def repoint_merged(self, tenant_id: TenantId, source_id: str, target_id: str) -> None: ...
    def delete_rows(self, ids: Collection[str]) -> None: ...
    def identities(self, ids: Collection[str]) -> list[tuple[str, str, Optional[str]]]: ...
    def lead_ids(self, ids: Collection[str]) -> list[str]: ...
    def import_jobs_with_errors(self, tenant_id: TenantId) -> list[ImportJob]: ...
    def deliveries_naming(self, tenant_id: TenantId, needles: Sequence[str]) -> list[WebhookDelivery]: ...
    def delete_notifications(self, tenant_id: TenantId, links: Sequence[str], emails: Iterable[str]) -> None: ...
    def detach_messages(self, ids: Collection[str]) -> None: ...
    def erase_rows(self, tenant_id: Optional[TenantId], ids: Collection[str]) -> None: ...

    # companies resolution (BR-CM-04) and accounts
    def account_by_name(self, tenant_id: TenantId, name: str) -> Optional[Account]: ...
    def live_account_by(self, tenant_id: TenantId, *, domain: Optional[str] = None, name: Optional[str] = None,
                        domainless: bool = False) -> Optional[Account]: ...
    def deleted_account_holds(self, tenant_id: TenantId, *, domain: Optional[str] = None,
                              name: Optional[str] = None) -> bool: ...
    def account_name_taken(self, tenant_id: TenantId, name: str) -> bool: ...
    def backfill_accounts(self, tenant_id: TenantId) -> int: ...

    # recycle bin expiry
    def expired_contact_ids(self, cutoff: datetime, limit: int) -> list[str]: ...
    def expired_accounts(self, cutoff: datetime) -> list[Account]: ...
    def purge_account(self, account: Account) -> None: ...

    # custom property definitions (BR-CM-09/10)
    def field_definitions(self, tenant_id: TenantId, order: str = "none") -> list[ContactFieldDefinition]: ...
    def required_fields(self, tenant_id: TenantId) -> list[ContactFieldDefinition]: ...
    def field_key_taken(self, tenant_id: TenantId, key: str) -> bool: ...
    def field_label_taken(self, tenant_id: TenantId, label: str) -> bool: ...
    def count_fields(self, tenant_id: TenantId) -> int: ...
    def field_definition(self, tenant_id: TenantId, field_id: str) -> Optional[ContactFieldDefinition]: ...
    def delete_field_definition(self, tenant_id: TenantId, field_id: str) -> int: ...

    # activities (BR-CM-13) and saved views (BR-CM-19)
    def activity(self, tenant_id: TenantId, activity_id: str) -> Optional[ContactActivity]: ...
    def views(self, tenant_id: TenantId, owner_id: str, object_type: str) -> list[SavedView]: ...
    def view(self, tenant_id: TenantId, view_id: str) -> Optional[SavedView]: ...


# ── SQLAlchemy implementation ───────────────────────────────────────────────

class SqlAlchemyContactRepository(SqlAlchemyRepository[Prospect, str]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Prospect)

    @property
    def session(self) -> Session:
        return self._session

    # entities --------------------------------------------------------------
    def stage(self, entity: Any) -> Any:
        """Stage a row that has no tenant of its own (list members, links keyed by a checked contact)."""
        self._session.add(entity)
        return entity

    def remove(self, entity: Any) -> None:
        self._session.delete(entity)

    def flush(self) -> None:
        self._session.flush()

    def refresh(self, entity: Any) -> None:
        self._session.refresh(entity)

    def touch(self, entity: Any) -> None:
        """Bump updated_at on the server clock."""
        entity.updated_at = func.now()

    def bump_version(self, prospect: Prospect) -> None:
        """Increment row_version in SQL (set-based edits take no expected version, T-3)."""
        prospect.row_version = Prospect.row_version + 1

    # visibility ------------------------------------------------------------
    def manager_pairs(self, tenant_id: TenantId) -> list[tuple[str, Optional[str]]]:
        return [(uid, mid) for uid, mid in self._session.query(User.user_id, User.manager_id).filter(
            User.tenant_id == tenant_id)]

    def _live(self, tenant_id: TenantId) -> Query[Any]:
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None))

    def _visible(self, tenant_id: TenantId, scope: VisibilityScope) -> Query[Any]:
        return apply_scope(self._live(tenant_id), Prospect.owner_id, scope)

    def get_live(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(
            Prospect.prospect_id == prospect_id, Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None),
        ).first()

    def visible_by_ids(self, tenant_id: TenantId, scope: VisibilityScope, ids: Collection[str]) -> list[Prospect]:
        return self._visible(tenant_id, scope).filter(Prospect.prospect_id.in_(ids)).all()

    # criteria --------------------------------------------------------------
    def _criteria_query(self, c: ContactCriteria) -> Query[Any]:
        query = self._visible(c.tenant_id, c.visibility)
        # Owner filters narrow within what the user may already see
        if c.owner_id:
            query = query.filter(Prospect.owner_id == c.owner_id)
        elif c.unassigned:
            query = query.filter(Prospect.owner_id.is_(None))
        if c.search:
            query = query.filter(_search_terms_condition(c.search))
        return self._property_filters(self._membership_filters(query, c), c)

    def _membership_filters(self, query: Query[Any], c: ContactCriteria) -> Query[Any]:
        if c.account_id:
            # Primary company or any other associated company (CM-014)
            query = query.filter(company_condition(self._session, c.account_id))
        if c.tag:
            query = query.filter(_has_tag(c.tag))
        if c.in_list is not None:
            query = query.filter(Prospect.prospect_id.in_(list_member_query(self._session, c.in_list)))
        if c.campaign_id:
            query = query.filter(Prospect.prospect_id.in_(
                self._session.query(CampaignProspect.prospect_id).filter(CampaignProspect.campaign_id == c.campaign_id)))
        return query

    def _property_filters(self, query: Query[Any], c: ContactCriteria) -> Query[Any]:
        if c.consent_status:
            query = query.filter(Prospect.consent_status == c.consent_status)
        if c.email_valid is not None:
            query = query.filter(Prospect.is_valid_email == c.email_valid)
        if c.has_phone is True:
            query = query.filter(or_(func.coalesce(Prospect.phone, "") != "",
                                     func.coalesce(Prospect.mobile_phone, "") != ""))
        elif c.has_phone is False:
            query = query.filter(func.coalesce(Prospect.phone, "") == "", func.coalesce(Prospect.mobile_phone, "") == "")
        for column, value in ((Prospect.poc_country, c.country), (Prospect.industry, c.industry),
                              (Prospect.lifecycle_stage, c.lifecycle_stage), (Prospect.lead_status, c.lead_status)):
            if value:
                query = query.filter(column == value)
        # Contact score (BR-CM-43): grade(s) and a 0-100 range
        if c.grades is not None:
            query = query.filter(Prospect.score_grade.in_(c.grades))
        if c.score_min is not None:
            query = query.filter(Prospect.score >= c.score_min)
        if c.score_max is not None:
            query = query.filter(Prospect.score <= c.score_max)
        clause = compile_filter(self._session, c.tree)
        if clause is not None:
            query = query.filter(clause)
        return query

    def search(self, criteria: ContactCriteria, sort: Sort, paging: Paging) -> Page[Prospect]:
        query = self._criteria_query(criteria)
        direction: Callable[[Any], Any] = (lambda c: c.desc()) if sort.descending else (lambda c: c.asc())
        order = [direction(c) for c in SORT_COLUMNS[sort.field]] + [direction(Prospect.prospect_id)]
        if criteria.search:
            # A search usually matches few rows: scan once, counting with a window function, and
            # don't let MySQL walk the date index looking for matches (contact search < 1 s at 100k)
            paged = query.add_columns(func.count().over().label("total")).with_hint(
                Prospect, "IGNORE INDEX FOR ORDER BY (ix_prospects_tenant_live_created, ix_prospects_tenant_live_updated)",
                "mysql").order_by(*order).offset(paging.offset).limit(paging.limit).all()
            rows = [r[0] for r in paged]
            total = paged[0][1] if paged else query.with_entities(
                func.count(Prospect.prospect_id)).order_by(None).scalar()
        else:
            # COUNT on the id (not .count(), which wraps every column in a subquery); the tiebreaker
            # sorts the same way as the sort column, so MySQL walks the index instead of sorting
            total = query.with_entities(func.count(Prospect.prospect_id)).order_by(None).scalar()
            rows = query.order_by(*order).offset(paging.offset).limit(paging.limit).all()
        return Page(items=rows, total=total, paging=paging)

    def count_by(self, criteria: ContactCriteria, group_by: str) -> dict[Optional[str], int]:
        column = GROUP_COLUMNS[group_by]
        return dict(self._criteria_query(criteria).with_entities(
            column, func.count(Prospect.prospect_id)).group_by(column).all())

    def lane(self, criteria: ContactCriteria, group_by: str, value: Optional[str], limit: int) -> list[Prospect]:
        column = GROUP_COLUMNS[group_by]
        return self._criteria_query(criteria).filter(column == value if value else column.is_(None)).order_by(
            Prospect.updated_at.desc()).limit(limit).all()

    def export_chunk(self, criteria: ContactCriteria, offset: int, limit: int) -> list[Prospect]:
        return self._criteria_query(criteria).order_by(Prospect.created_at.desc()).offset(offset).limit(limit).all()

    def distinct_values(self, tenant_id: TenantId, scope: VisibilityScope, column: str) -> list[Any]:
        col = FACET_COLUMNS[column]
        return sorted(v for (v,) in self._visible(tenant_id, scope).with_entities(col).filter(
            col.isnot(None), col != "").distinct())

    def tag_lists(self, tenant_id: TenantId, scope: VisibilityScope) -> list[Any]:
        return [t for (t,) in self._visible(tenant_id, scope).with_entities(Prospect.tags).filter(
            Prospect.tags.isnot(None))]

    def prospects_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]:
        if not ids:
            return []
        return self._session.query(Prospect).filter(Prospect.prospect_id.in_(ids),
                                                    Prospect.tenant_id == tenant_id).all()

    def deleted_page(self, tenant_id: TenantId, scope: VisibilityScope, cutoff: datetime, q: Optional[str],
                     paging: Paging) -> Page[Prospect]:
        query = self._session.query(Prospect).filter(
            Prospect.tenant_id == tenant_id, Prospect.deleted_at.isnot(None),
            Prospect.deleted_at > cutoff, Prospect.merged_into_id.is_(None))
        query = apply_scope(query, Prospect.owner_id, scope)  # own team's contacts only (BR-SH-02)
        if q and q.strip():
            term = q.strip()
            query = query.filter(or_(*[sql_like.ilike(c, term) for c in (
                Prospect.email, Prospect.first_name, Prospect.last_name, Prospect.company_name)]))
        total = query.count()
        rows = query.order_by(Prospect.deleted_at.desc()).offset(paging.offset).limit(paging.limit).all()
        return Page(items=rows, total=total, paging=paging)

    def list_rows(self, tenant_id: TenantId) -> list[tuple[str, str, str]]:
        return [tuple(r) for r in self._session.query(
            ProspectList.list_id, ProspectList.list_name, ProspectList.list_type).filter(
            ProspectList.tenant_id == tenant_id).order_by(ProspectList.uploaded_at.desc()).all()]

    def campaign_rows(self, tenant_id: TenantId) -> list[tuple[str, str, str]]:
        return [tuple(r) for r in self._session.query(Campaign.campaign_id, Campaign.campaign_name, Campaign.status).filter(
            Campaign.tenant_id == tenant_id).order_by(Campaign.created_at.desc()).all()]

    def active_users(self, tenant_id: TenantId) -> list[User]:
        return self._session.query(User).filter(
            User.tenant_id == tenant_id, User.status == "ACTIVE").order_by(User.first_name, User.last_name).all()

    def search_contacts(self, tenant_id: TenantId, scope: VisibilityScope, q: str, limit: int) -> list[Prospect]:
        return self._visible(tenant_id, scope).filter(search_condition(q)).order_by(
            Prospect.updated_at.desc()).limit(limit).all()

    def search_companies(self, tenant_id: TenantId, scope: VisibilityScope, term: str, limit: int) -> list[Account]:
        companies = self._session.query(Account).filter(
            Account.tenant_id == tenant_id, Account.deleted_at.is_(None),
            or_(sql_like.ilike(Account.name, term), sql_like.ilike(Account.domain, term)))
        if scope.owner_ids is not None:  # companies of your team, or with a contact you can see (BR-SH-02)
            seen = self._visible(tenant_id, scope).with_entities(Prospect.account_id).filter(
                Prospect.account_id.isnot(None))
            companies = companies.filter(or_(Account.owner_id.in_(scope.owner_ids), Account.account_id.in_(seen)))
        return companies.order_by(Account.name).limit(limit).all()

    # batch lookups -----------------------------------------------------------
    def users_by_ids(self, ids: Collection[str]) -> dict[str, User]:
        if not ids:
            return {}
        return {u.user_id: u for u in self._session.query(User).filter(User.user_id.in_(ids))}

    def live_account_names(self, ids: Collection[str]) -> dict[str, tuple[str, Optional[str]]]:
        if not ids:
            return {}
        return {a.account_id: (a.name, a.domain) for a in self._session.query(
            Account.account_id, Account.name, Account.domain).filter(
            Account.account_id.in_(ids), Account.deleted_at.is_(None))}

    def account_names(self, ids: Collection[str]) -> dict[str, str]:
        if not ids:
            return {}
        return {a.account_id: a.name for a in self._session.query(Account).filter(Account.account_id.in_(ids))}

    def activity_touches(self, ids: Collection[str]) -> list[tuple[str, str, Optional[datetime]]]:
        return [(pid, kind, ts) for pid, kind, ts in self._session.query(
            ContactActivity.prospect_id, ContactActivity.activity_type, func.max(ContactActivity.occurred_at)).filter(
            ContactActivity.prospect_id.in_(ids)).group_by(ContactActivity.prospect_id, ContactActivity.activity_type)]

    def outbound_sends(self, ids: Collection[str]) -> list[tuple[str, Optional[datetime]]]:
        return [(pid, ts) for pid, ts in self._session.query(EmailMessage.prospect_id, func.max(EmailMessage.sent_at)).filter(
            EmailMessage.prospect_id.in_(ids), EmailMessage.sent_at.isnot(None),
            EmailMessage.direction == "OUTBOUND").group_by(EmailMessage.prospect_id)]

    # record page ---------------------------------------------------------------
    def account_card(self, tenant_id: TenantId, account_id: Optional[str]) -> Optional[tuple[Account, int]]:
        if not account_id:
            return None
        account = self._session.query(Account).filter(Account.account_id == account_id, Account.tenant_id == tenant_id,
                                                      Account.deleted_at.is_(None)).first()
        if not account:
            return None
        count = self._session.query(func.count(Prospect.prospect_id)).filter(
            Prospect.account_id == account.account_id, Prospect.deleted_at.is_(None)).scalar()
        return account, count

    def static_memberships(self, prospect_id: str, newest_first: bool) -> list[tuple[ProspectListMember, ProspectList]]:
        query = self._session.query(ProspectListMember, ProspectList).join(
            ProspectList, ProspectList.list_id == ProspectListMember.list_id
        ).filter(ProspectListMember.prospect_id == prospect_id)
        if newest_first:
            query = query.order_by(ProspectListMember.added_at.desc())
        return [(m, lst) for m, lst in query]

    def active_lists(self, tenant_id: TenantId) -> list[ProspectList]:
        return list(self._session.query(ProspectList).filter(ProspectList.tenant_id == tenant_id,
                                                             ProspectList.list_type == "ACTIVE"))

    def in_list(self, scope: ListScope, prospect_id: str) -> bool:
        member = Prospect.prospect_id if scope.active else ProspectListMember.prospect_id
        return list_member_query(self._session, scope).filter(member == prospect_id).first() is not None

    def enrollments(self, prospect_id: str, newest_first: bool) -> list[tuple[CampaignProspect, Campaign]]:
        query = self._session.query(CampaignProspect, Campaign).join(
            Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
        ).filter(CampaignProspect.prospect_id == prospect_id)
        if newest_first:
            query = query.order_by(CampaignProspect.enrolled_at.desc())
        return [(e, c) for e, c in query]

    def persona(self, prospect_id: str) -> Optional[ProspectPersona]:
        return self._session.query(ProspectPersona).filter(ProspectPersona.prospect_id == prospect_id).first()

    def stats(self, prospect_id: str) -> ContactStats:
        db = self._session
        sent = db.query(func.count(EmailMessage.message_id), func.max(EmailMessage.sent_at)).filter(
            EmailMessage.prospect_id == prospect_id, EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.isnot(None)).one()
        received = db.query(func.count(EmailMessage.message_id), func.max(EmailMessage.sent_at)).filter(
            EmailMessage.prospect_id == prospect_id, reply_message()).one()
        opens = db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(EmailMessage.prospect_id == prospect_id, EmailEvent.event_type == EmailEvent.EVENT_OPEN).scalar()
        activity_counts = dict(db.query(ContactActivity.activity_type, func.count(ContactActivity.activity_id)).filter(
            ContactActivity.prospect_id == prospect_id).group_by(ContactActivity.activity_type).all())
        open_tasks = db.query(func.count(CrmTask.task_id)).filter(
            CrmTask.prospect_id == prospect_id, CrmTask.status == "OPEN").scalar()
        return ContactStats(sent[0], sent[1], received[0], received[1], opens or 0, activity_counts, open_tasks or 0)

    def changes(self, prospect_id: str, field: Optional[str], changed_by: Optional[str],
                limit: int) -> list[PropertyChange]:
        query = self._session.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                                           PropertyChange.object_id == prospect_id)
        if field:
            query = query.filter(PropertyChange.field == field)
        if changed_by:
            query = query.filter(PropertyChange.changed_by == changed_by)
        return query.order_by(PropertyChange.changed_at.desc()).limit(limit).all()

    def activities_of(self, prospect_id: str, created_by: Optional[str], limit: int) -> list[ContactActivity]:
        query = self._session.query(ContactActivity).filter(ContactActivity.prospect_id == prospect_id)
        if created_by:
            query = query.filter(ContactActivity.created_by == created_by)
        return query.order_by(ContactActivity.occurred_at.desc()).limit(limit).all()

    def tasks_of(self, prospect_id: str, created_by: Optional[str], limit: int) -> list[CrmTask]:
        query = self._session.query(CrmTask).filter(CrmTask.prospect_id == prospect_id)
        if created_by:
            query = query.filter(CrmTask.created_by == created_by)
        return list(query.order_by(CrmTask.created_at.desc()).limit(limit))

    def emails_of(self, prospect_id: str, limit: int) -> list[tuple[EmailMessage, Optional[str]]]:
        return [(m, name) for m, name in self._session.query(EmailMessage, Campaign.campaign_name).outerjoin(
            Campaign, Campaign.campaign_id == EmailMessage.campaign_id
        ).filter(
            EmailMessage.prospect_id == prospect_id,
            or_(EmailMessage.sent_at.isnot(None), EmailMessage.direction == "INBOUND"),
            not_system_notice(),  # the bounce note: the EMAIL_BOUNCED event shows the bounce
        ).order_by(func.coalesce(EmailMessage.sent_at, EmailMessage.scheduled_at).desc()).limit(limit).all()]

    def email_events_of(self, prospect_id: str, kinds: Collection[str],
                        limit: int) -> list[tuple[EmailEvent, Optional[str]]]:
        return [(e, subject) for e, subject in self._session.query(EmailEvent, EmailMessage.subject).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.prospect_id == prospect_id,
            EmailEvent.event_type.in_(list(kinds)),
        ).order_by(EmailEvent.event_time.asc()).limit(limit).all()]

    # write side ----------------------------------------------------------------
    def user_in_tenant(self, tenant_id: TenantId, user_id: str) -> bool:
        return self._session.query(User.user_id).filter(
            User.user_id == user_id, User.tenant_id == tenant_id).first() is not None

    def live_account(self, tenant_id: TenantId, account_id: str) -> Optional[Account]:
        return self._session.query(Account).filter(
            Account.account_id == account_id, Account.tenant_id == tenant_id, Account.deleted_at.is_(None)).first()

    def account(self, tenant_id: TenantId, account_id: str) -> Optional[Account]:
        """The tenant's company, live or in the recycle bin."""
        return self._session.query(Account).filter(Account.account_id == account_id,
                                                   Account.tenant_id == tenant_id).first()

    def by_email(self, tenant_id: TenantId, email: str) -> Optional[Prospect]:
        """The contact with this address, or the one a retired / merged address now belongs to (C-11)."""
        return self.resolve_contact_by_email(tenant_id, email)

    def resolve_contact_by_email(self, tenant_id: TenantId, email: str) -> Optional[Prospect]:
        """Current address first (live or in the recycle bin), then e-mail history: a retired address
        resolves to its contact, a merged one to the surviving contact."""
        return self.duplicates.resolve_contact_by_email(tenant_id, email)

    def email_taken(self, tenant_id: TenantId, email: str, except_id: str) -> bool:
        return self._session.query(Prospect.prospect_id).filter(
            Prospect.tenant_id == tenant_id, Prospect.email == email,
            Prospect.prospect_id != except_id).first() is not None

    def is_unsubscribed(self, tenant_id: TenantId, email: str) -> bool:
        return self._session.query(GlobalUnsubscribe).filter(
            GlobalUnsubscribe.tenant_id == tenant_id, func.lower(GlobalUnsubscribe.email) == email.lower()
        ).first() is not None

    def retarget_unsent(self, prospect_id: str, email: str, statuses: Collection[str]) -> int:
        return self._session.query(EmailMessage).filter(
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.status.in_(statuses),
            EmailMessage.sent_at.is_(None),
        ).update({EmailMessage.to_email: email}, synchronize_session=False)

    def list_in_tenant(self, tenant_id: TenantId, list_id: Optional[str]) -> Optional[ProspectList]:
        return self._session.query(ProspectList).filter(ProspectList.list_id == list_id,
                                                        ProspectList.tenant_id == tenant_id).first()

    def list_member_ids(self, list_id: str, ids: Collection[str]) -> set[str]:
        return {r[0] for r in self._session.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(ids))}

    def remove_list_members(self, list_id: str, ids: Collection[str]) -> int:
        return self._session.query(ProspectListMember).filter(
            ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(ids)
        ).delete(synchronize_session=False)

    def campaign_in_tenant(self, tenant_id: TenantId, campaign_id: Optional[str]) -> Optional[Campaign]:
        return self._session.query(Campaign).filter(Campaign.campaign_id == campaign_id,
                                                    Campaign.tenant_id == tenant_id).first()

    def deleted_ids(self, tenant_id: TenantId, scope: VisibilityScope, ids: Collection[str]) -> list[str]:
        query = self._session.query(Prospect.prospect_id).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids),
            Prospect.deleted_at.isnot(None), Prospect.merged_into_id.is_(None))
        return [r[0] for r in apply_scope(query, Prospect.owner_id, scope).all()]

    def restorable(self, tenant_id: TenantId, ids: Collection[str], cutoff: datetime,
                   scope: Optional[VisibilityScope]) -> list[Prospect]:
        query = self._session.query(Prospect).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids),
            Prospect.deleted_at.isnot(None), Prospect.deleted_at > cutoff, Prospect.merged_into_id.is_(None),
        )
        if scope is not None:
            query = apply_scope(query, Prospect.owner_id, scope)
        return query.all()

    def soft_delete(self, ids: Collection[str], when: datetime, user_id: Optional[str]) -> None:
        """Hide the contacts and stop anything still queued to them."""
        db = self._session
        db.query(Prospect).filter(Prospect.prospect_id.in_(ids)).update(
            {Prospect.deleted_at: when, Prospect.deleted_by: user_id}, synchronize_session=False)
        db.query(EmailMessage).filter(
            EmailMessage.prospect_id.in_(ids), EmailMessage.status.in_(_CANCEL_ON_DELETE)
        ).update({EmailMessage.status: "CANCELLED", EmailMessage.failure_reason: "Contact deleted"},
                 synchronize_session=False)
        db.query(CampaignProspect).filter(
            CampaignProspect.prospect_id.in_(ids), CampaignProspect.status.in_(_PAUSE_ON_DELETE)
        ).update({CampaignProspect.status: "PAUSED", CampaignProspect.stopped_reason: "Contact deleted"},
                 synchronize_session=False)

    # companies -------------------------------------------------------------------
    def links_of(self, prospect_id: str) -> list[ContactCompany]:
        return self._session.query(ContactCompany).filter(ContactCompany.prospect_id == prospect_id).all()

    def link(self, prospect_id: str, account_id: str) -> Optional[ContactCompany]:
        return self._session.query(ContactCompany).filter(ContactCompany.prospect_id == prospect_id,
                                                          ContactCompany.account_id == account_id).first()

    def linked_account_ids(self, prospect_id: str, except_account: Optional[str] = None) -> list[str]:
        query = self._session.query(ContactCompany.account_id).filter(ContactCompany.prospect_id == prospect_id)
        if except_account is not None:
            query = query.filter(ContactCompany.account_id != except_account)
        return [r.account_id for r in query]

    def links_with_accounts(self, prospect: Prospect) -> list[tuple[ContactCompany, Account]]:
        return [(link, a) for link, a in self._session.query(ContactCompany, Account).join(
            Account, Account.account_id == ContactCompany.account_id).filter(
            ContactCompany.prospect_id == prospect.prospect_id, Account.tenant_id == prospect.tenant_id,
            Account.deleted_at.is_(None))]

    def labels_for_company(self, account_id: str, ids: Collection[str]) -> dict[str, str]:
        return {pid: label for pid, label in self._session.query(ContactCompany.prospect_id, ContactCompany.label).filter(
            ContactCompany.account_id == account_id, ContactCompany.prospect_id.in_(ids),
            ContactCompany.label.isnot(None))}

    def has_visible_contact_at(self, account_id: str, owner_ids: Collection[str]) -> bool:
        return self._session.query(Prospect.prospect_id).filter(
            company_condition(self._session, account_id), Prospect.deleted_at.is_(None),
            Prospect.owner_id.in_(owner_ids)).first() is not None

    # merge and erasure -------------------------------------------------------
    @property
    def duplicates(self) -> SqlAlchemyDuplicateRepository:
        """Stored duplicate pairs, merge logs and e-mail identifiers on the same session."""
        return SqlAlchemyDuplicateRepository(self._session)

    def memberships(self, prospect_id: str) -> list[ProspectListMember]:
        return self._session.query(ProspectListMember).filter(ProspectListMember.prospect_id == prospect_id).all()

    def enrollment_rows(self, prospect_id: str) -> list[CampaignProspect]:
        return self._session.query(CampaignProspect).filter(CampaignProspect.prospect_id == prospect_id).all()

    def reassign_children(self, source_id: str, target_id: str) -> tuple[int, int]:
        """Move emails, conversations, activities, tasks, leads and deals; returns (emails, activities)."""
        db = self._session
        emails = db.query(EmailMessage).filter(EmailMessage.prospect_id == source_id).update(
            {EmailMessage.prospect_id: target_id}, synchronize_session=False)
        db.query(Conversation).filter(Conversation.prospect_id == source_id).update(
            {Conversation.prospect_id: target_id}, synchronize_session=False)
        activities = db.query(ContactActivity).filter(ContactActivity.prospect_id == source_id).update(
            {ContactActivity.prospect_id: target_id}, synchronize_session=False)
        db.query(CrmTask).filter(CrmTask.prospect_id == source_id).update(
            {CrmTask.prospect_id: target_id}, synchronize_session=False)
        # Sales records follow the person (FKs would otherwise block deleting the duplicate)
        for model in (Lead, Opportunity):
            db.query(model).filter(model.prospect_id == source_id).update(
                {model.prospect_id: target_id}, synchronize_session=False)
        return emails, activities

    def _point_merged(self, tenant_id: TenantId, sources: Collection[str], target_id: Optional[str]) -> None:
        """The tenant's contacts merged into one of ``sources`` now point at ``target_id`` (None: no pointer).

        D1: the old ``UPDATE ... WHERE merged_into_id IN (...)`` had no index and no tenant filter, so it
        scanned and X-locked every contact of every tenant and deadlocked with background-import inserts.
        Now a plain (non-locking) read finds them on ix_prospects_tenant_merged and the UPDATE goes by
        unique key (tenant, primary key), which locks just those rows: no table scan and no gap locks that would make another
        workspace's inserts wait. Usually nothing points there and no UPDATE is sent. (Nothing else writes
        merged_into_id, so a pointer cannot appear between the read and the update.)"""
        pointing = [pid for (pid,) in self._session.query(Prospect.prospect_id).filter(
            Prospect.tenant_id == tenant_id, Prospect.merged_into_id.in_(list(sources)))]
        if pointing:
            self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(pointing),
                                                 Prospect.merged_into_id.in_(list(sources))).update(
                {Prospect.merged_into_id: target_id}, synchronize_session=False)

    def repoint_merged(self, tenant_id: TenantId, source_id: str, target_id: str) -> None:
        """Contacts merged into ``source_id`` now point at ``target_id``; its history follows it."""
        self._point_merged(tenant_id, [source_id], target_id)
        self._session.query(PropertyChange).filter(PropertyChange.tenant_id == tenant_id,
                                                   PropertyChange.object_type == "CONTACT",
                                                   PropertyChange.object_id == source_id).update(
            {PropertyChange.object_id: target_id}, synchronize_session=False)

    def delete_rows(self, ids: Collection[str]) -> None:
        self._session.query(Prospect).filter(Prospect.prospect_id.in_(list(ids))).delete(synchronize_session=False)

    def identities(self, ids: Collection[str]) -> list[tuple[str, str, Optional[str]]]:
        return [(pid, tid, email) for pid, tid, email in self._session.query(
            Prospect.prospect_id, Prospect.tenant_id, Prospect.email).filter(Prospect.prospect_id.in_(ids)).all()]

    def add_erasure_audit(self, ids: Collection[str], actor_id: Optional[str], reason: str) -> None:
        """One audit row per erased contact (who, when, which record id; no personal data)."""
        for pid, tenant_id, _email in self.identities(ids):
            self._session.add(AuditLog(tenant_id=tenant_id, user_id=actor_id, action=reason,
                                       entity_type="contact", entity_id=pid))

    def lead_ids(self, ids: Collection[str]) -> list[str]:
        return [lid for (lid,) in self._session.query(Lead.lead_id).filter(Lead.prospect_id.in_(ids)).all()]

    def import_jobs_with_errors(self, tenant_id: TenantId) -> list[ImportJob]:
        return list(self._session.query(ImportJob).filter(ImportJob.tenant_id == tenant_id,
                                                          ImportJob.errors.isnot(None)))

    def deliveries_naming(self, tenant_id: TenantId, needles: Sequence[str]) -> list[WebhookDelivery]:
        return list(self._session.query(WebhookDelivery).filter(
            WebhookDelivery.tenant_id == tenant_id,
            or_(*[WebhookDelivery.payload.contains(n, autoescape=True) for n in needles])))

    def delete_notifications(self, tenant_id: TenantId, links: Sequence[str], emails: Iterable[str]) -> None:
        named = [Notification.link.in_(links)]
        for e in emails:
            named += [Notification.title.contains(e, autoescape=True), Notification.body.contains(e, autoescape=True)]
        self._session.query(Notification).filter(Notification.tenant_id == tenant_id, or_(*named)).delete(
            synchronize_session=False)

    def detach_messages(self, ids: Collection[str]) -> None:
        """Delete the contacts' email events and unthread messages from their conversations (FKs)."""
        db = self._session
        message_ids = [m[0] for m in db.query(EmailMessage.message_id).filter(EmailMessage.prospect_id.in_(ids)).all()]
        if message_ids:
            db.query(EmailEvent).filter(EmailEvent.message_id.in_(message_ids)).delete(synchronize_session=False)
        conversation_ids = [c[0] for c in db.query(Conversation.id).filter(Conversation.prospect_id.in_(ids))]
        if conversation_ids:  # any message threaded into these conversations must let go first (FK)
            db.query(EmailMessage).filter(EmailMessage.conversation_id.in_(conversation_ids)).update(
                {EmailMessage.conversation_id: None}, synchronize_session=False)

    def _ids_by_tenant(self, tenant_id: Optional[TenantId], ids: Collection[str]) -> dict[str, list[str]]:
        """The existing contacts among ``ids`` grouped by their tenant (only ``tenant_id``'s when given)."""
        groups: dict[str, list[str]] = {}
        for pid, tid, _email in self.identities(ids):
            if tenant_id is None or tid == tenant_id:
                groups.setdefault(tid, []).append(pid)
        return groups

    def erase_rows(self, tenant_id: Optional[TenantId], ids: Collection[str]) -> None:
        """Deals lose the link to the person; every other row about the contacts goes, then the contacts.

        ``tenant_id`` None = the system purge across workspaces (each contact's own tenant is used); with a
        tenant, contacts of other tenants among ``ids`` are left alone."""
        db = self._session
        by_tenant = self._ids_by_tenant(tenant_id, ids)
        ids = [pid for group in by_tenant.values() for pid in group]
        if not ids:
            return
        db.query(Opportunity).filter(Opportunity.prospect_id.in_(ids)).update(
            {Opportunity.prospect_id: None}, synchronize_session=False)
        for model in _ERASED_CHILDREN:
            db.query(model).filter(model.prospect_id.in_(ids)).delete(synchronize_session=False)
        # Contacts merged into these ones keep no pointer to an erased record (per tenant, see _point_merged)
        for tenant, tenant_ids in by_tenant.items():
            self._point_merged(tenant, tenant_ids, None)
        db.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                        PropertyChange.object_id.in_(ids)).delete(synchronize_session=False)
        db.query(Prospect).filter(Prospect.prospect_id.in_(ids)).delete(synchronize_session=False)

    # company resolution and accounts -----------------------------------------------
    def account_by_name(self, tenant_id: TenantId, name: str) -> Optional[Account]:
        return self._session.query(Account).filter(Account.tenant_id == tenant_id, Account.name == name).first()

    def live_account_by(self, tenant_id: TenantId, *, domain: Optional[str] = None, name: Optional[str] = None,
                        domainless: bool = False) -> Optional[Account]:
        query = self._session.query(Account).filter(Account.tenant_id == tenant_id, Account.deleted_at.is_(None))
        if domain is not None:
            query = query.filter(Account.domain == domain)
        if name is not None:
            query = query.filter(Account.name == name)
        if domainless:
            query = query.filter(Account.domain.is_(None))
        return query.first()

    def deleted_account_holds(self, tenant_id: TenantId, *, domain: Optional[str] = None,
                              name: Optional[str] = None) -> bool:
        query = self._session.query(Account.account_id).filter(Account.tenant_id == tenant_id)
        if domain is not None:
            query = query.filter(Account.domain == domain)
        if name is not None:
            query = query.filter(Account.name == name)
        return query.filter(Account.deleted_at.isnot(None)).first() is not None

    def account_name_taken(self, tenant_id: TenantId, name: str) -> bool:
        return self._session.query(Account.account_id).filter(
            Account.tenant_id == tenant_id, Account.name == name).first() is not None

    def backfill_accounts(self, tenant_id: TenantId) -> int:
        """Create accounts from company names and link contacts that have no account."""
        self._session.execute(text("""
            INSERT IGNORE INTO accounts (account_id, tenant_id, name, industry, emp_band, created_at, updated_at)
            SELECT UUID(), p.tenant_id, LEFT(TRIM(p.company_name), 255), MAX(p.industry), MAX(p.emp_band), NOW(), NOW()
            FROM prospects p
            WHERE p.tenant_id = :tenant_id AND p.account_id IS NULL
              AND p.company_name IS NOT NULL AND TRIM(p.company_name) <> ''
            GROUP BY p.tenant_id, LEFT(TRIM(p.company_name), 255)
        """), {"tenant_id": tenant_id})
        result: Any = self._session.execute(text("""
            UPDATE prospects p
            JOIN accounts a ON a.tenant_id = p.tenant_id AND a.name = LEFT(TRIM(p.company_name), 255)
            SET p.account_id = a.account_id
            WHERE p.tenant_id = :tenant_id AND p.account_id IS NULL
              AND p.company_name IS NOT NULL AND TRIM(p.company_name) <> ''
        """), {"tenant_id": tenant_id})
        return result.rowcount or 0

    # recycle bin expiry ------------------------------------------------------------
    def expired_contact_ids(self, cutoff: datetime, limit: int) -> list[str]:
        return [r[0] for r in self._session.query(Prospect.prospect_id).filter(
            Prospect.deleted_at.isnot(None), Prospect.deleted_at <= cutoff).limit(limit)]

    def expired_accounts(self, cutoff: datetime) -> list[Account]:
        return self._session.query(Account).filter(Account.deleted_at.isnot(None), Account.deleted_at <= cutoff).all()

    def purge_account(self, account: Account) -> None:
        """Detach everything that still points at the company (FKs), then remove it."""
        db = self._session
        for model in (Prospect, Lead, Opportunity):
            db.query(model).filter(model.account_id == account.account_id).update(
                {model.account_id: None}, synchronize_session=False)
        db.query(CrmTask).filter(CrmTask.account_id == account.account_id).delete(synchronize_session=False)
        db.query(PropertyChange).filter(PropertyChange.object_type == "COMPANY",
                                        PropertyChange.object_id == account.account_id).delete(synchronize_session=False)
        db.delete(account)

    # custom property definitions -------------------------------------------------------
    def field_definitions(self, tenant_id: TenantId, order: str = "none") -> list[ContactFieldDefinition]:
        """``order``: "none", "sort" (sort_order) or "listing" (group, sort_order, created_at)."""
        query = self._session.query(ContactFieldDefinition).filter(ContactFieldDefinition.tenant_id == tenant_id)
        if order == "sort":
            query = query.order_by(ContactFieldDefinition.sort_order)
        elif order == "listing":
            query = query.order_by(ContactFieldDefinition.group_name, ContactFieldDefinition.sort_order,
                                   ContactFieldDefinition.created_at)
        return query.all()

    def required_fields(self, tenant_id: TenantId) -> list[ContactFieldDefinition]:
        return self._session.query(ContactFieldDefinition).filter(
            ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.required.is_(True)).all()

    def field_key_taken(self, tenant_id: TenantId, key: str) -> bool:
        return self._session.query(ContactFieldDefinition.field_id).filter(
            ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.field_key == key).first() is not None

    def field_label_taken(self, tenant_id: TenantId, label: str) -> bool:
        return self._session.query(ContactFieldDefinition.field_id).filter(
            ContactFieldDefinition.tenant_id == tenant_id,
            func.lower(ContactFieldDefinition.label) == label.lower()).first() is not None

    def count_fields(self, tenant_id: TenantId) -> int:
        return self._session.query(func.count(ContactFieldDefinition.field_id)).filter(
            ContactFieldDefinition.tenant_id == tenant_id).scalar()

    def field_definition(self, tenant_id: TenantId, field_id: str) -> Optional[ContactFieldDefinition]:
        return self._session.query(ContactFieldDefinition).filter(
            ContactFieldDefinition.field_id == field_id, ContactFieldDefinition.tenant_id == tenant_id).first()

    def delete_field_definition(self, tenant_id: TenantId, field_id: str) -> int:
        return self._session.query(ContactFieldDefinition).filter(
            ContactFieldDefinition.field_id == field_id, ContactFieldDefinition.tenant_id == tenant_id).delete()

    # activities and saved views ----------------------------------------------------------
    def activity(self, tenant_id: TenantId, activity_id: str) -> Optional[ContactActivity]:
        return self._session.query(ContactActivity).filter(
            ContactActivity.activity_id == activity_id, ContactActivity.tenant_id == tenant_id).first()

    def views(self, tenant_id: TenantId, owner_id: str, object_type: str) -> list[SavedView]:
        return self._session.query(SavedView).filter(
            SavedView.tenant_id == tenant_id, SavedView.object_type == object_type,
            or_(SavedView.owner_id == owner_id, SavedView.shared.is_(True)),
        ).order_by(SavedView.shared, SavedView.name).all()

    def view(self, tenant_id: TenantId, view_id: str) -> Optional[SavedView]:
        return self._session.query(SavedView).filter(SavedView.view_id == view_id,
                                                     SavedView.tenant_id == tenant_id).first()


# ── Unit of work ────────────────────────────────────────────────────────────

class ContactsAccess(Protocol):
    """The contact repository plus the session it runs on (the bridge to services that still take a
    ``db``). An entered ``ContactsUnitOfWork`` is one; ``borrow(db)`` gives one without a transaction
    boundary, for helpers called inside a caller's own transaction."""

    @property
    def contacts(self) -> ContactRepository: ...

    @property
    def session(self) -> Session: ...


@dataclass(frozen=True)
class _Borrowed:
    session: Session

    @property
    def contacts(self) -> SqlAlchemyContactRepository:
        return SqlAlchemyContactRepository(self.session)


def borrow(session: Session) -> ContactsAccess:
    """Repository access on a caller's session; the caller owns the transaction."""
    return _Borrowed(session)


class ContactsUnitOfWork(Protocol):
    """The transaction a contact service call runs in, with the contact repository on its session.

    ``session`` is the bridge to services that still take a ``db`` (mentions, suppression, scoring):
    they work in the same transaction."""

    @property
    def contacts(self) -> ContactRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> ContactsUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def savepoint(self) -> AbstractContextManager[Any]: ...


class SqlAlchemyContactsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyContactsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyContactsUnitOfWork:
        super().__enter__()
        return self

    @property
    def contacts(self) -> SqlAlchemyContactRepository:
        return SqlAlchemyContactRepository(self.session)

    def savepoint(self) -> AbstractContextManager[Any]:
        """A nested transaction: an exception inside it undoes only its own changes."""
        return self.session.begin_nested()


def contacts_uow(session: Session) -> SqlAlchemyContactsUnitOfWork:
    """The unit of work over a request's session (``get_db``)."""
    return SqlAlchemyContactsUnitOfWork.for_session(session)
