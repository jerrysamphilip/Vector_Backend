"""
Tasks on contacts, companies and deals (BR-CM-15): the SQL behind the task queue and the task cards
(ADR 0009, chunk L1). Part of the contacts aggregate; services call it through ``TaskRepository``.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from sqlalchemy import case, or_
from sqlalchemy.orm import Query, Session

from app.domain.contacts import TaskFilter
from app.domain.paging import Page, Paging
from app.models.account import Account
from app.models.crm import CrmTask
from app.models.prospect import Prospect
from app.models.sales import Opportunity
from app.models.user import User
from app.repositories.base import SqlAlchemyRepository, TenantId


class TaskRepository(Protocol):
    def get(self, tenant_id: TenantId, entity_id: str) -> Optional[CrmTask]: ...
    def add(self, tenant_id: TenantId, entity: CrmTask) -> CrmTask: ...
    def remove(self, task: CrmTask) -> None: ...
    def user_names(self, ids: Collection[str]) -> dict[str, str]: ...
    def contact(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]: ...
    def company_name(self, tenant_id: TenantId, account_id: str) -> Optional[str]: ...
    def opportunity_name(self, opportunity_id: str) -> Optional[str]: ...
    def page(self, tenant_id: TenantId, f: TaskFilter, me: str, visible: Optional[Collection[str]],
             now: datetime, paging: Paging) -> Page[CrmTask]: ...
    def my_counts(self, tenant_id: TenantId, me: str, now: datetime) -> tuple[int, int, int]: ...


class SqlAlchemyTaskRepository(SqlAlchemyRepository[CrmTask, str]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, CrmTask)

    def get(self, tenant_id: TenantId, entity_id: str) -> Optional[CrmTask]:
        return self._session.query(CrmTask).filter(CrmTask.task_id == entity_id, CrmTask.tenant_id == tenant_id).first()

    def remove(self, task: CrmTask) -> None:
        self._session.delete(task)

    def user_names(self, ids: Collection[str]) -> dict[str, str]:
        return {u.user_id: f"{u.first_name} {u.last_name}".strip()
                for u in self._session.query(User).filter(User.user_id.in_(ids))}

    def contact(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        """The task's contact, live or in the recycle bin."""
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def company_name(self, tenant_id: TenantId, account_id: str) -> Optional[str]:
        row = self._session.query(Account.account_id, Account.name).filter(
            Account.account_id == account_id, Account.tenant_id == tenant_id).first()
        return row.name if row else None

    def opportunity_name(self, opportunity_id: str) -> Optional[str]:
        return self._session.query(Opportunity.name).filter(Opportunity.opportunity_id == opportunity_id).scalar()

    def _scoped(self, query: Query[Any], f: TaskFilter, me: str, visible: Optional[Collection[str]]) -> Query[Any]:
        if f.opportunity_id:
            return query.filter(CrmTask.opportunity_id == f.opportunity_id)
        if f.prospect_id or f.account_id:
            if f.prospect_id:
                query = query.filter(CrmTask.prospect_id == f.prospect_id)
            if f.account_id:
                query = query.filter(CrmTask.account_id == f.account_id)
                # A company is shared across teams: show only the tasks of people you can see (BR-SH-02)
                if visible is not None:
                    query = query.filter(or_(CrmTask.owner_id.in_(visible), CrmTask.created_by == me))
            return query
        if f.scope in ("all", "team"):
            # Everyone's tasks you may see: the whole workspace, or you and your team (BR-SH-02)
            return query.filter(CrmTask.owner_id.in_(visible)) if visible is not None else query
        if f.scope == "created":
            return query.filter(CrmTask.created_by == me)
        return query.filter(CrmTask.owner_id == me)

    @staticmethod
    def _due(query: Query[Any], due: Optional[str], now: datetime) -> Query[Any]:
        today_end = now.replace(hour=23, minute=59, second=59)
        if due == "overdue":
            return query.filter(CrmTask.due_at < now)
        if due == "today":
            return query.filter(CrmTask.due_at <= today_end)
        if due == "week":
            return query.filter(CrmTask.due_at <= today_end + timedelta(days=7))
        if due == "none":
            return query.filter(CrmTask.due_at.is_(None))
        return query

    def page(self, tenant_id: TenantId, f: TaskFilter, me: str, visible: Optional[Collection[str]],
             now: datetime, paging: Paging) -> Page[CrmTask]:
        query = self._scoped(self._session.query(CrmTask).filter(CrmTask.tenant_id == tenant_id), f, me, visible)
        if f.status != "ALL":
            query = query.filter(CrmTask.status == f.status)
        query = self._due(query, f.due, now)
        total = query.count()
        priority_rank = case({"HIGH": 0, "MEDIUM": 1, "LOW": 2}, value=CrmTask.priority, else_=3)
        rows = query.order_by(CrmTask.status.desc(), CrmTask.due_at.is_(None), CrmTask.due_at.asc(), priority_rank,
                              CrmTask.created_at.desc()).offset(paging.offset).limit(paging.limit).all()
        return Page(items=rows, total=total, paging=paging)

    def my_counts(self, tenant_id: TenantId, me: str, now: datetime) -> tuple[int, int, int]:
        """(open, overdue, reminders due) of the user's own open tasks."""
        mine = self._session.query(CrmTask).filter(CrmTask.tenant_id == tenant_id, CrmTask.owner_id == me,
                                                   CrmTask.status == "OPEN")
        return mine.count(), mine.filter(CrmTask.due_at < now).count(), mine.filter(CrmTask.reminder_at <= now).count()
