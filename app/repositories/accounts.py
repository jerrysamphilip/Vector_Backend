"""
Accounts (companies) aggregate repository (ADR 0009, chunk A1): every SQL statement about companies, their
contact counts, the company filter tree (C-16), field propagation to contacts (C-20) and the backfill run
ledger (C-14).

Services depend on ``AccountRepository`` (a Protocol, so unit tests hand them an in-memory fake);
``SqlAlchemyAccountRepository`` is the production implementation. Every read that starts from a tenant takes
``tenant_id``; reads keyed by a company or a contact take one the service loaded through a tenant-scoped call.
Nothing here commits, decides permissions or applies business rules: the caller passes the ``VisibilityScope``
it computed, and the service's unit of work commits.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import ColumnElement, Numeric, and_, cast, func, or_
from sqlalchemy.orm import Query, Session

from app.domain.accounts import AccountCriteria
from app.domain.contacts import FilterCondition, FilterGroup, FilterNode, VisibilityScope
from app.domain.paging import Page, Paging, Sort
from app.models.account import Account
from app.models.account_backfill import AccountBackfillItem, AccountBackfillRun
from app.models.crm import ContactCompany, CrmTask, PropertyChange
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity
from app.models.user import User
from app.repositories.base import SqlAlchemyRepository, SqlAlchemyUnitOfWork, TenantId
from app.repositories.contacts import _compare as compare_values
from app.repositories.contacts import apply_scope, company_condition
from app.repositories.system_notices import reply_message
from app.utils import sql_like

__all__ = [
    "AccountRepository", "AccountsAccess", "AccountsUnitOfWork", "Session", "SqlAlchemyAccountRepository",
    "SqlAlchemyAccountsUnitOfWork", "accounts_uow", "borrow",
]

COMPANY_COLUMNS = {
    "name": Account.name, "domain": Account.domain, "industry": Account.industry, "emp_band": Account.emp_band,
    "country": Account.country, "city": Account.city, "lifecycle_stage": Account.lifecycle_stage,
    "annual_revenue": Account.annual_revenue, "owner_id": Account.owner_id,
}
# The contact column each propagated company field is copied to (C-20; app/domain/accounts.py)
CONTACT_COLUMNS = {"company_name": Prospect.company_name, "industry": Prospect.industry,
                   "emp_band": Prospect.emp_band}


# ── Filter tree ─────────────────────────────────────────────────────────────

def _custom_clause(cond: FilterCondition) -> ColumnElement[bool]:
    path = f'$."{cond.name}"'
    raw: Any = func.json_unquote(func.json_extract(Account.custom_fields, path))
    if cond.operator in ("contains", "is") and isinstance(cond.value, str):  # multi-checkbox values are arrays
        arr = func.json_contains(func.json_extract(Account.custom_fields, path), func.json_quote(cond.value)) == 1
        if cond.operator == "is":
            return or_(compare_values(raw, cond.operator, cond.value), arr)
        return or_(sql_like.ilike(raw, cond.value), arr)
    if cond.numeric:
        raw = cast(raw, Numeric(18, 4))
    return compare_values(raw, cond.operator, cond.value)


def compile_company_filter(node: Optional[FilterNode]) -> Optional[ColumnElement[bool]]:
    """SQL condition over accounts for a parsed company filter tree (None = everything)."""
    if node is None:
        return None
    if isinstance(node, FilterGroup):
        parts = [c for c in (compile_company_filter(child) for child in node.children) if c is not None]
        return or_(*parts) if node.op == "OR" else and_(*parts)
    if node.kind == "custom":
        return _custom_clause(node)
    return compare_values(COMPANY_COLUMNS[node.name], node.operator, node.value, is_text=node.is_text)


# ── Protocol ────────────────────────────────────────────────────────────────

class AccountRepository(Protocol):
    """What the account services need from storage. ORM objects returned are staged in the unit of work."""

    def add(self, tenant_id: TenantId, entity: Any) -> Any: ...
    def flush(self) -> None: ...
    def remove(self, entity: Any) -> None: ...

    # companies
    def live(self, tenant_id: TenantId, account_id: str) -> Optional[Account]: ...
    def deleted(self, tenant_id: TenantId, account_id: str) -> Optional[Account]: ...
    def search(self, criteria: AccountCriteria, sort: Sort, paging: Paging) -> Page[tuple[Account, int]]: ...
    def count(self, criteria: AccountCriteria) -> int: ...
    def export_chunk(self, criteria: AccountCriteria, offset: int, limit: int) -> list[tuple[Account, int]]: ...
    def deleted_since(self, tenant_id: TenantId, cutoff: datetime) -> list[Account]: ...
    def name_taken(self, tenant_id: TenantId, name: str, except_id: Optional[str] = None) -> bool: ...
    def domain_taken(self, tenant_id: TenantId, domain: str, except_id: Optional[str] = None,
                     live_only: bool = False) -> bool: ...
    def live_by(self, tenant_id: TenantId, *, domain: Optional[str] = None,
                name: Optional[str] = None) -> Optional[Account]: ...
    def user_in_tenant(self, tenant_id: TenantId, user_id: str) -> bool: ...
    def users_by_ids(self, ids: Collection[str]) -> dict[str, User]: ...

    # record page
    def company_contacts(self, tenant_id: TenantId, account_id: str, scope: VisibilityScope,
                         limit: int) -> list[Prospect]: ...
    def email_counts(self, prospect_ids: Collection[str]) -> tuple[int, int]: ...
    def open_tasks(self, account_id: str) -> int: ...
    def history(self, account_id: str, limit: int) -> list[PropertyChange]: ...

    # propagation (C-20)
    def primary_contacts(self, tenant_id: TenantId, account_id: str) -> list[Prospect]: ...

    # backfill (C-14)
    def unlinked_contacts(self, tenant_id: TenantId, scope: VisibilityScope) -> list[tuple[Any, ...]]: ...
    def company_index_rows(self, tenant_id: TenantId) -> tuple[list[tuple[str, str, Optional[str]]],
                                                               list[tuple[str, Optional[str]]]]: ...
    def unlinked_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]: ...
    def contacts_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> dict[str, Prospect]: ...
    def accounts_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> dict[str, Account]: ...
    def running_run(self, tenant_id: TenantId, since: datetime) -> Optional[AccountBackfillRun]: ...
    def run(self, tenant_id: TenantId, run_id: str) -> Optional[AccountBackfillRun]: ...
    def runs(self, tenant_id: TenantId, started_by: Optional[Collection[str]], limit: int) -> list[AccountBackfillRun]: ...
    def run_items(self, run_id: str, kinds: Collection[str], offset: int, limit: int) -> list[AccountBackfillItem]: ...
    def account_in_use(self, account_id: str) -> bool: ...
    def account_edited(self, account_id: str) -> bool: ...
    def purge_created(self, account: Account) -> None: ...


# ── SQLAlchemy implementation ───────────────────────────────────────────────

class SqlAlchemyAccountRepository(SqlAlchemyRepository[Account, str]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Account)

    @property
    def session(self) -> Session:
        return self._session

    def flush(self) -> None:
        self._session.flush()

    def remove(self, entity: Any) -> None:
        self._session.delete(entity)

    # companies ---------------------------------------------------------------
    def live(self, tenant_id: TenantId, account_id: str) -> Optional[Account]:
        return self._session.query(Account).filter(
            Account.account_id == account_id, Account.tenant_id == tenant_id, Account.deleted_at.is_(None)).first()

    def deleted(self, tenant_id: TenantId, account_id: str) -> Optional[Account]:
        return self._session.query(Account).filter(
            Account.account_id == account_id, Account.tenant_id == tenant_id, Account.deleted_at.isnot(None)).first()

    def _visible_contacts(self, tenant_id: TenantId, scope: VisibilityScope) -> Query[Any]:
        query = self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None))
        return apply_scope(query, Prospect.owner_id, scope)

    def _counts(self, c: AccountCriteria) -> Any:
        """Subquery account_id -> visible contacts: primary company plus other associations (CM-014)."""
        visible = self._visible_contacts(c.tenant_id, c.scope)
        primary = visible.with_entities(Prospect.prospect_id.label("pid"), Prospect.account_id.label("aid")) \
            .filter(Prospect.account_id.isnot(None))
        other = visible.join(ContactCompany, ContactCompany.prospect_id == Prospect.prospect_id).filter(
            ContactCompany.is_primary.is_(False)).with_entities(Prospect.prospect_id.label("pid"),
                                                              ContactCompany.account_id.label("aid"))
        pairs = primary.union(other).subquery()
        return self._session.query(pairs.c.aid.label("account_id"), func.count(pairs.c.pid).label("n")) \
            .group_by(pairs.c.aid).subquery()

    def _criteria_query(self, c: AccountCriteria) -> tuple[Query[Any], Any]:
        counts = self._counts(c)
        query = self._session.query(Account, func.coalesce(counts.c.n, 0)).outerjoin(
            counts, counts.c.account_id == Account.account_id).filter(Account.tenant_id == c.tenant_id)
        if c.archived:
            query = query.filter(Account.deleted_at.isnot(None))
            if c.cutoff is not None:
                query = query.filter(Account.deleted_at > c.cutoff)
        else:
            query = query.filter(Account.deleted_at.is_(None))
        if c.scope.owner_ids is not None:
            # Companies owned by you or your team, or holding at least one contact you can see
            query = query.filter(Account.owner_id.in_(c.scope.owner_ids) | (counts.c.n > 0))
        return self._property_filters(query, c), counts

    @staticmethod
    def _property_filters(query: Query[Any], c: AccountCriteria) -> Query[Any]:
        if c.unassigned:
            query = query.filter(Account.owner_id.is_(None))
        elif c.owner_id:
            query = query.filter(Account.owner_id == c.owner_id)
        if c.lifecycle_stage:
            query = query.filter(Account.lifecycle_stage == c.lifecycle_stage)
        if c.industry:
            query = query.filter(Account.industry == c.industry)
        if c.q:
            query = query.filter(or_(sql_like.ilike(Account.name, c.q), sql_like.ilike(Account.domain, c.q),
                                     sql_like.ilike(Account.industry, c.q)))
        clause = compile_company_filter(c.tree)
        return query if clause is None else query.filter(clause)

    def search(self, criteria: AccountCriteria, sort: Sort, paging: Paging) -> Page[tuple[Account, int]]:
        query, counts = self._criteria_query(criteria)
        total = query.count()
        column = {"name": Account.name, "contacts": func.coalesce(counts.c.n, 0),
                  "annual_revenue": Account.annual_revenue, "created_at": Account.created_at,
                  "updated_at": Account.updated_at}[sort.field]
        rows = query.order_by(column.desc() if sort.descending else column.asc(), Account.account_id) \
            .offset(paging.offset).limit(paging.limit).all()
        return Page([(a, int(n)) for a, n in rows], total, paging)

    def count(self, criteria: AccountCriteria) -> int:
        return self._criteria_query(criteria)[0].count()

    def export_chunk(self, criteria: AccountCriteria, offset: int, limit: int) -> list[tuple[Account, int]]:
        query, _ = self._criteria_query(criteria)
        return [(a, int(n)) for a, n in query.order_by(Account.name, Account.account_id).offset(offset).limit(limit)]

    def deleted_since(self, tenant_id: TenantId, cutoff: datetime) -> list[Account]:
        return self._session.query(Account).filter(Account.tenant_id == tenant_id, Account.deleted_at.isnot(None),
                                                   Account.deleted_at > cutoff).order_by(Account.deleted_at.desc()).all()

    def name_taken(self, tenant_id: TenantId, name: str, except_id: Optional[str] = None) -> bool:
        query = self._session.query(Account.account_id).filter(Account.tenant_id == tenant_id, Account.name == name)
        if except_id:
            query = query.filter(Account.account_id != except_id)
        return query.first() is not None

    def domain_taken(self, tenant_id: TenantId, domain: str, except_id: Optional[str] = None,
                     live_only: bool = False) -> bool:
        query = self._session.query(Account.account_id).filter(Account.tenant_id == tenant_id,
                                                               Account.domain == domain)
        if except_id:
            query = query.filter(Account.account_id != except_id)
        if live_only:
            query = query.filter(Account.deleted_at.is_(None))
        return query.first() is not None

    def live_by(self, tenant_id: TenantId, *, domain: Optional[str] = None,
                name: Optional[str] = None) -> Optional[Account]:
        query = self._session.query(Account).filter(Account.tenant_id == tenant_id, Account.deleted_at.is_(None))
        if domain is not None:
            query = query.filter(Account.domain == domain)
        if name is not None:
            query = query.filter(Account.name == name)
        return query.first()

    def user_in_tenant(self, tenant_id: TenantId, user_id: str) -> bool:
        return self._session.query(User.user_id).filter(
            User.user_id == user_id, User.tenant_id == tenant_id).first() is not None

    def users_by_ids(self, ids: Collection[str]) -> dict[str, User]:
        if not ids:
            return {}
        return {u.user_id: u for u in self._session.query(User).filter(User.user_id.in_(ids))}

    # record page -------------------------------------------------------------
    def company_contacts(self, tenant_id: TenantId, account_id: str, scope: VisibilityScope,
                         limit: int) -> list[Prospect]:
        return self._visible_contacts(tenant_id, scope).filter(company_condition(self._session, account_id)) \
            .order_by(Prospect.first_name, Prospect.last_name).limit(limit).all()

    def email_counts(self, prospect_ids: Collection[str]) -> tuple[int, int]:
        if not prospect_ids:
            return 0, 0
        sent = self._session.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.prospect_id.in_(prospect_ids), EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.isnot(None)).scalar()
        replies = self._session.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.prospect_id.in_(prospect_ids), reply_message()).scalar()
        return int(sent or 0), int(replies or 0)

    def open_tasks(self, account_id: str) -> int:
        return int(self._session.query(func.count(CrmTask.task_id)).filter(
            CrmTask.account_id == account_id, CrmTask.status == "OPEN").scalar() or 0)

    def history(self, account_id: str, limit: int) -> list[PropertyChange]:
        return self._session.query(PropertyChange).filter(
            PropertyChange.object_type == "COMPANY", PropertyChange.object_id == account_id,
        ).order_by(PropertyChange.changed_at.desc()).limit(limit).all()

    # propagation ---------------------------------------------------------------
    def primary_contacts(self, tenant_id: TenantId, account_id: str) -> list[Prospect]:
        """Every contact (live or in the recycle bin) whose primary company this is."""
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                    Prospect.account_id == account_id).all()

    # backfill ------------------------------------------------------------------
    def unlinked_contacts(self, tenant_id: TenantId, scope: VisibilityScope) -> list[tuple[Any, ...]]:
        """(prospect_id, email, company_name, industry, emp_band) of live contacts without a company."""
        query = self._session.query(Prospect.prospect_id, Prospect.email, Prospect.company_name, Prospect.industry,
                                    Prospect.emp_band).filter(Prospect.tenant_id == tenant_id,
                                                              Prospect.deleted_at.is_(None),
                                                              Prospect.account_id.is_(None))
        return [tuple(r) for r in apply_scope(query, Prospect.owner_id, scope).order_by(Prospect.prospect_id)]

    def company_index_rows(self, tenant_id: TenantId) -> tuple[list[tuple[str, str, Optional[str]]],
                                                               list[tuple[str, Optional[str]]]]:
        rows = self._session.query(Account.account_id, Account.name, Account.domain, Account.deleted_at).filter(
            Account.tenant_id == tenant_id).order_by(Account.created_at, Account.account_id).all()
        live = [(r[0], r[1], r[2]) for r in rows if r[3] is None]
        deleted = [(r[1], r[2]) for r in rows if r[3] is not None]
        return live, deleted

    def unlinked_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> list[Prospect]:
        if not ids:
            return []
        return self._session.query(Prospect).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids), Prospect.deleted_at.is_(None),
            Prospect.account_id.is_(None)).all()

    def contacts_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> dict[str, Prospect]:
        if not ids:
            return {}
        return {p.prospect_id: p for p in self._session.query(Prospect).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids))}

    def accounts_by_ids(self, tenant_id: TenantId, ids: Collection[str]) -> dict[str, Account]:
        if not ids:
            return {}
        return {a.account_id: a for a in self._session.query(Account).filter(
            Account.tenant_id == tenant_id, Account.account_id.in_(ids))}

    def running_run(self, tenant_id: TenantId, since: datetime) -> Optional[AccountBackfillRun]:
        return self._session.query(AccountBackfillRun).filter(
            AccountBackfillRun.tenant_id == tenant_id, AccountBackfillRun.status == "RUNNING",
            AccountBackfillRun.started_at > since).first()

    def run(self, tenant_id: TenantId, run_id: str) -> Optional[AccountBackfillRun]:
        return self._session.query(AccountBackfillRun).filter(
            AccountBackfillRun.tenant_id == tenant_id, AccountBackfillRun.run_id == run_id).first()

    def runs(self, tenant_id: TenantId, started_by: Optional[Collection[str]], limit: int) -> list[AccountBackfillRun]:
        query = self._session.query(AccountBackfillRun).filter(AccountBackfillRun.tenant_id == tenant_id)
        if started_by is not None:
            query = query.filter(AccountBackfillRun.started_by.in_(started_by or {"-"}))
        return query.order_by(AccountBackfillRun.started_at.desc(), AccountBackfillRun.run_id).limit(limit).all()

    def run_items(self, run_id: str, kinds: Collection[str], offset: int, limit: int) -> list[AccountBackfillItem]:
        return self._session.query(AccountBackfillItem).filter(
            AccountBackfillItem.run_id == run_id, AccountBackfillItem.kind.in_(kinds)
        ).order_by(AccountBackfillItem.item_id).offset(offset).limit(limit).all()

    def account_in_use(self, account_id: str) -> bool:
        """Anything still points at the company: a contact, an association, a task, a lead or a deal."""
        checks: Sequence[Any] = (
            self._session.query(Prospect.prospect_id).filter(Prospect.account_id == account_id),
            self._session.query(ContactCompany.link_id).filter(ContactCompany.account_id == account_id),
            self._session.query(CrmTask.task_id).filter(CrmTask.account_id == account_id),
            self._session.query(Lead.lead_id).filter(Lead.account_id == account_id),
            self._session.query(Opportunity.opportunity_id).filter(Opportunity.account_id == account_id),
        )
        return any(q.first() is not None for q in checks)

    def account_edited(self, account_id: str) -> bool:
        """Someone changed the company outside a backfill (its history has a row from another source)."""
        return self._session.query(PropertyChange.change_id).filter(
            PropertyChange.object_type == "COMPANY", PropertyChange.object_id == account_id,
            PropertyChange.source != "BACKFILL").first() is not None

    def purge_created(self, account: Account) -> None:
        self._session.query(PropertyChange).filter(PropertyChange.object_type == "COMPANY",
                                                   PropertyChange.object_id == account.account_id
                                                   ).delete(synchronize_session=False)
        self._session.delete(account)


# ── Unit of work ────────────────────────────────────────────────────────────

class AccountsAccess(Protocol):
    """The account repository plus the session it runs on (the bridge to services that still take a ``db``)."""

    @property
    def accounts(self) -> AccountRepository: ...

    @property
    def session(self) -> Session: ...


@dataclass(frozen=True)
class _Borrowed:
    session: Session

    @property
    def accounts(self) -> SqlAlchemyAccountRepository:
        return SqlAlchemyAccountRepository(self.session)


def borrow(session: Session) -> AccountsAccess:
    """Repository access on a caller's session; the caller owns the transaction."""
    return _Borrowed(session)


class AccountsUnitOfWork(Protocol):
    """The transaction an account service call runs in. ``session`` bridges to helpers that take ``db``
    (contact visibility, property history, amounts masking)."""

    @property
    def accounts(self) -> AccountRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> AccountsUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def savepoint(self) -> AbstractContextManager[Any]: ...


class SqlAlchemyAccountsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyAccountsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyAccountsUnitOfWork:
        super().__enter__()
        return self

    @property
    def accounts(self) -> SqlAlchemyAccountRepository:
        return SqlAlchemyAccountRepository(self.session)

    def savepoint(self) -> AbstractContextManager[Any]:
        return self.session.begin_nested()


def accounts_uow(session: Session) -> SqlAlchemyAccountsUnitOfWork:
    """The unit of work over a request's session (``get_db``)."""
    return SqlAlchemyAccountsUnitOfWork.for_session(session)
