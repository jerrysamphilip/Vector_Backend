"""
Lists aggregate repository (ADR 0009, chunk A1): every SQL statement behind contact lists (/lists),
upload lists (/prospect-lists), adding / removing members, enrolling a list into a campaign, list
re-validation and the prospect KPI cards.

Services depend on ``ListRepository`` (a Protocol, so unit tests hand them a fake);
``SqlAlchemyListRepository`` is the production one. Entry points that start from a workspace take
``tenant_id``; member queries are keyed by a list the service already loaded through a tenant-scoped
call. Nothing here commits, checks permissions or decides rules: the caller passes the
``VisibilityScope`` / ``ListScope`` it computed, and the service's unit of work commits.
"""

from __future__ import annotations

from collections.abc import Collection
from contextlib import AbstractContextManager
from typing import Any, Optional, Protocol

from sqlalchemy import distinct, func, literal
from sqlalchemy.orm import Query, Session

from app.domain.contacts import FilterNode, ListScope, VisibilityScope
from app.domain.lists import ListFilter, ProspectListFilter
from app.domain.paging import Page, Paging
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.user import User
from app.repositories.base import SqlAlchemyRepository, SqlAlchemyUnitOfWork, TenantId
from app.repositories.contacts import apply_scope, compile_filter, list_member_query
from app.utils.sql_like import ilike

__all__ = ["ListRepository", "ListsUnitOfWork", "ProspectStats", "SqlAlchemyListRepository",
           "SqlAlchemyListsUnitOfWork", "free_list_name", "lists_uow"]

ProspectStats = tuple[int, int, int, int]  # total, available, active, opted_out


class ListRepository(Protocol):
    # lists
    def get(self, tenant_id: TenantId, entity_id: str) -> Optional[ProspectList]: ...
    def add(self, tenant_id: TenantId, entity: Any) -> Any: ...
    def stage(self, entity: Any) -> Any: ...
    def remove(self, entity: Any) -> None: ...
    def flush(self) -> None: ...
    def name_taken(self, tenant_id: TenantId, name: str, except_id: Optional[str] = None) -> bool: ...
    def free_name(self, tenant_id: TenantId, name: str) -> str: ...
    def search(self, tenant_id: TenantId, f: ListFilter, limit: int) -> list[ProspectList]: ...
    def page(self, tenant_id: TenantId, f: ListFilter, paging: Paging) -> Page[ProspectList]: ...
    def user_names(self, ids: Collection[str]) -> dict[str, str]: ...
    def visible_member_count(self, scope: ListScope, visibility: VisibilityScope) -> int: ...
    def preview(self, tenant_id: TenantId, visibility: VisibilityScope, tree: Optional[FilterNode],
                sample: int) -> tuple[int, list[Prospect]]: ...

    # members
    def live_prospect_ids(self, tenant_id: TenantId, ids: Collection[str]) -> set[str]: ...
    def member_ids(self, list_id: str, ids: Collection[str]) -> set[str]: ...
    def remove_members(self, list_id: str, ids: Collection[str]) -> int: ...
    def delete_members(self, list_id: str) -> None: ...
    def membership(self, list_id: str, prospect_id: str) -> Optional[ProspectListMember]: ...

    # upload lists (/prospect-lists)
    def upload_page(self, tenant_id: TenantId, f: ProspectListFilter, paging: Paging) -> Page[ProspectList]: ...
    def active_count(self, scope: ListScope) -> int: ...
    def static_live_count(self, list_id: str) -> int: ...
    def list_prospects(self, scope: ListScope, search: Optional[str],
                       paging: Paging) -> Page[tuple[Prospect, Optional[str]]]: ...
    def all_member_ids(self, list_id: str) -> list[str]: ...
    def ids_in_other_lists(self, ids: Collection[str], list_id: str) -> set[str]: ...
    def ids_with_activity(self, ids: Collection[str]) -> set[str]: ...
    def hard_delete_prospects(self, ids: Collection[str]) -> None: ...
    def revalidation_rows(self, tenant_id: TenantId, list_id: str) -> list[Prospect]: ...
    def unsubscribe_reasons(self, tenant_id: TenantId) -> dict[str, Optional[str]]: ...
    def prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]: ...
    def prospect_stats(self, tenant_id: TenantId) -> ProspectStats: ...

    # campaign enrollment from lists
    def tenant_list_ids(self, tenant_id: TenantId, ids: Collection[str]) -> set[str]: ...
    def campaign(self, tenant_id: TenantId, campaign_id: str) -> Optional[Campaign]: ...
    def first_step(self, campaign_id: str) -> Optional[EmailSequence]: ...


class SqlAlchemyListRepository(SqlAlchemyRepository[ProspectList, str]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ProspectList)

    @property
    def session(self) -> Session:
        return self._session

    # lists ---------------------------------------------------------------------
    def get(self, tenant_id: TenantId, entity_id: str) -> Optional[ProspectList]:
        return self._session.query(ProspectList).filter(ProspectList.list_id == entity_id,
                                                        ProspectList.tenant_id == tenant_id).first()

    def stage(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def remove(self, entity: Any) -> None:
        self._session.delete(entity)

    def flush(self) -> None:
        self._session.flush()

    def name_taken(self, tenant_id: TenantId, name: str, except_id: Optional[str] = None) -> bool:
        """Another list of the workspace already has this name (case-insensitive, C-21)."""
        query = self._session.query(ProspectList.list_id).filter(
            ProspectList.tenant_id == tenant_id, func.lower(ProspectList.list_name) == name.lower())
        if except_id:
            query = query.filter(ProspectList.list_id != except_id)
        return query.first() is not None

    def free_name(self, tenant_id: TenantId, name: str) -> str:
        """``name``, or ``name (2)``, ``name (3)``, ... when the workspace already has a list called that:
        for flows that name a list without asking (upload title, import, bulk "new list")."""
        return free_list_name(self._session, tenant_id, name)

    def _filtered(self, tenant_id: TenantId, f: ListFilter) -> Query[Any]:
        query = self._session.query(ProspectList).filter(ProspectList.tenant_id == tenant_id)
        if f.list_type:
            query = query.filter(ProspectList.list_type == f.list_type)
        if f.search:
            query = query.filter(ilike(ProspectList.list_name, f.search))
        return query

    def search(self, tenant_id: TenantId, f: ListFilter, limit: int) -> list[ProspectList]:
        return self._filtered(tenant_id, f).order_by(ProspectList.uploaded_at.desc()).limit(limit).all()

    def page(self, tenant_id: TenantId, f: ListFilter, paging: Paging) -> Page[ProspectList]:
        query = self._filtered(tenant_id, f)
        total = query.with_entities(func.count(ProspectList.list_id)).scalar() or 0
        rows = query.order_by(ProspectList.uploaded_at.desc(), ProspectList.list_id.desc()).offset(
            paging.offset).limit(paging.limit).all()
        return Page(items=rows, total=total, paging=paging)

    def user_names(self, ids: Collection[str]) -> dict[str, str]:
        if not ids:
            return {}
        return {u.user_id: f"{u.first_name} {u.last_name}".strip()
                for u in self._session.query(User).filter(User.user_id.in_(ids))}

    def visible_member_count(self, scope: ListScope, visibility: VisibilityScope) -> int:
        """Live members the caller may see (active lists evaluated from their filters)."""
        query = self._session.query(func.count(Prospect.prospect_id)).filter(
            Prospect.prospect_id.in_(list_member_query(self._session, scope)), Prospect.deleted_at.is_(None))
        return apply_scope(query, Prospect.owner_id, visibility).scalar() or 0

    def preview(self, tenant_id: TenantId, visibility: VisibilityScope, tree: Optional[FilterNode],
                sample: int) -> tuple[int, list[Prospect]]:
        query = self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None))
        query = apply_scope(query, Prospect.owner_id, visibility)
        clause = compile_filter(self._session, tree)
        if clause is not None:
            query = query.filter(clause)
        rows = query.order_by(Prospect.created_at.desc()).limit(sample).all()
        return query.count(), rows

    # members -------------------------------------------------------------------
    def live_prospect_ids(self, tenant_id: TenantId, ids: Collection[str]) -> set[str]:
        return {r[0] for r in self._session.query(Prospect.prospect_id).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids), Prospect.deleted_at.is_(None))}

    def member_ids(self, list_id: str, ids: Collection[str]) -> set[str]:
        return {r[0] for r in self._session.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(ids))}

    def remove_members(self, list_id: str, ids: Collection[str]) -> int:
        return self._session.query(ProspectListMember).filter(
            ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(ids)
        ).delete(synchronize_session=False)

    def delete_members(self, list_id: str) -> None:
        self._session.query(ProspectListMember).filter(ProspectListMember.list_id == list_id).delete(
            synchronize_session=False)

    def membership(self, list_id: str, prospect_id: str) -> Optional[ProspectListMember]:
        return self._session.query(ProspectListMember).filter(
            ProspectListMember.list_id == list_id, ProspectListMember.prospect_id == prospect_id).first()

    # upload lists --------------------------------------------------------------
    def upload_page(self, tenant_id: TenantId, f: ProspectListFilter, paging: Paging) -> Page[ProspectList]:
        query = self._session.query(ProspectList).filter(ProspectList.tenant_id == tenant_id)
        if f.search:
            query = query.filter(ilike(ProspectList.list_name, f.search))
        total = query.count()
        column = ProspectList.list_name if f.sort.field == "list_name" else ProspectList.uploaded_at
        query = query.order_by(column.desc() if f.sort.descending else column.asc())
        return Page(items=query.offset(paging.offset).limit(paging.limit).all(), total=total, paging=paging)

    def active_count(self, scope: ListScope) -> int:
        return list_member_query(self._session, scope).count()

    def static_live_count(self, list_id: str) -> int:
        return self._session.query(func.count(ProspectListMember.id)).join(
            Prospect, Prospect.prospect_id == ProspectListMember.prospect_id
        ).filter(ProspectListMember.list_id == list_id, Prospect.deleted_at.is_(None)).scalar() or 0

    def list_prospects(self, scope: ListScope, search: Optional[str],
                       paging: Paging) -> Page[tuple[Prospect, Optional[str]]]:
        query: Query[Any]
        if scope.active:
            query = self._session.query(Prospect, literal(None).label("notes")).filter(
                Prospect.prospect_id.in_(list_member_query(self._session, scope))
            ).order_by(Prospect.created_at.asc(), Prospect.prospect_id.asc())
        else:
            query = self._session.query(Prospect, ProspectListMember.notes).join(
                ProspectListMember, Prospect.prospect_id == ProspectListMember.prospect_id
            ).filter(ProspectListMember.list_id == scope.list_id, Prospect.deleted_at.is_(None)).order_by(
                ProspectListMember.added_at.asc(), ProspectListMember.id.asc())
        if search:
            query = query.filter(ilike(Prospect.first_name, search) | ilike(Prospect.last_name, search)
                                 | ilike(Prospect.email, search) | ilike(Prospect.company_name, search))
        total = query.count()
        rows = [(p, notes) for p, notes in query.offset(paging.offset).limit(paging.limit).all()]
        return Page(items=rows, total=total, paging=paging)

    def all_member_ids(self, list_id: str) -> list[str]:
        return [r[0] for r in self._session.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.list_id == list_id).all()]

    def ids_in_other_lists(self, ids: Collection[str], list_id: str) -> set[str]:
        return {r[0] for r in self._session.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.prospect_id.in_(ids), ProspectListMember.list_id != list_id).all()}

    def ids_with_activity(self, ids: Collection[str]) -> set[str]:
        return {r[0] for r in self._session.query(ContactActivity.prospect_id).filter(
            ContactActivity.prospect_id.in_(ids)).distinct()}

    def hard_delete_prospects(self, ids: Collection[str]) -> None:
        """Remove contacts that only existed for an upload list, with their emails and enrollments."""
        db = self._session
        message_ids = [m[0] for m in db.query(EmailMessage.message_id).filter(EmailMessage.prospect_id.in_(ids)).all()]
        if message_ids:
            db.query(EmailEvent).filter(EmailEvent.message_id.in_(message_ids)).delete(synchronize_session=False)
        for model in (EmailMessage, Conversation, ProspectPersona, CampaignProspect, Prospect):
            db.query(model).filter(model.prospect_id.in_(ids)).delete(synchronize_session=False)

    def revalidation_rows(self, tenant_id: TenantId, list_id: str) -> list[Prospect]:
        return self._session.query(Prospect).join(
            ProspectListMember, Prospect.prospect_id == ProspectListMember.prospect_id
        ).filter(ProspectListMember.list_id == list_id, Prospect.tenant_id == tenant_id).all()

    def unsubscribe_reasons(self, tenant_id: TenantId) -> dict[str, Optional[str]]:
        return {email.lower(): reason for email, reason in self._session.query(
            GlobalUnsubscribe.email, GlobalUnsubscribe.reason).filter(GlobalUnsubscribe.tenant_id == tenant_id).all()}

    def prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def prospect_stats(self, tenant_id: TenantId) -> ProspectStats:
        """KPI cards: live contacts, enrolled somewhere ("available"), never enrolled ("active"), opted out."""
        db = self._session
        live = db.query(Prospect.prospect_id).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None))
        total = live.with_entities(func.count(Prospect.prospect_id)).scalar() or 0
        opted_out = live.filter(Prospect.consent_status == "UNSUBSCRIBED").with_entities(
            func.count(Prospect.prospect_id)).scalar() or 0
        enrolled = db.query(func.count(distinct(CampaignProspect.prospect_id))).filter(
            CampaignProspect.prospect_id.in_(live)).scalar() or 0
        return total, enrolled, total - enrolled, opted_out

    # campaign enrollment -------------------------------------------------------
    def tenant_list_ids(self, tenant_id: TenantId, ids: Collection[str]) -> set[str]:
        return {r[0] for r in self._session.query(ProspectList.list_id).filter(
            ProspectList.tenant_id == tenant_id, ProspectList.list_id.in_(ids)).all()}

    def campaign(self, tenant_id: TenantId, campaign_id: str) -> Optional[Campaign]:
        return self._session.query(Campaign).filter(Campaign.campaign_id == campaign_id,
                                                    Campaign.tenant_id == tenant_id).first()

    def first_step(self, campaign_id: str) -> Optional[EmailSequence]:
        return self._session.query(EmailSequence).filter(EmailSequence.campaign_id == campaign_id,
                                                         EmailSequence.step_number == 1).first()


def free_list_name(session: Session, tenant_id: TenantId, name: str) -> str:
    """A list name not used in the workspace yet (C-21), at most 255 characters."""
    base = name.strip()[:255]
    taken = {n.lower() for (n,) in session.query(ProspectList.list_name).filter(
        ProspectList.tenant_id == tenant_id, ProspectList.list_name.startswith(base[:240], autoescape=True))}
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base[:240]} ({n})".lower() in taken:
        n += 1
    return f"{base[:240]} ({n})"


# ── Unit of work ────────────────────────────────────────────────────

class ListsUnitOfWork(Protocol):
    """The transaction a list service call runs in. ``session`` bridges to services that still take
    a ``db`` (crm filters, enrollment rules, campaign email scheduling, contact edits)."""

    @property
    def lists(self) -> ListRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> ListsUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def savepoint(self) -> AbstractContextManager[Any]: ...


class SqlAlchemyListsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyListsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyListsUnitOfWork:
        super().__enter__()
        return self

    @property
    def lists(self) -> SqlAlchemyListRepository:
        return SqlAlchemyListRepository(self.session)

    def savepoint(self) -> AbstractContextManager[Any]:
        return self.session.begin_nested()


def lists_uow(session: Session) -> SqlAlchemyListsUnitOfWork:
    """The unit of work over a request's session (``get_db``)."""
    return SqlAlchemyListsUnitOfWork.for_session(session)

