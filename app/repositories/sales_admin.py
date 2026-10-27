"""
Sales settings and admin repositories (ADR 0009, chunk A3): the per-workspace settings document,
revenue targets, workflow rules, in-app notifications and the email template library
(BR-SF-02/03/05/07/08/10/12/13). Tenant- or user-scoped; never commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import or_

from app.models.sales_extra import MessageTemplate, Notification, SalesSetting, SalesTarget, WorkflowRule
from app.models.user import User
from app.repositories.base import TenantId
from app.repositories.sales_common import SessionRepository
from app.utils.sql_like import ilike


class SalesSettingsRepository(Protocol):
    def stored(self, tenant_id: TenantId) -> Optional[dict[str, Any]]: ...
    def save(self, tenant_id: TenantId, settings: dict[str, Any]) -> None: ...


class SqlAlchemySalesSettingsRepository(SessionRepository):
    def _row(self, tenant_id: TenantId) -> Optional[SalesSetting]:
        return self._session.query(SalesSetting).filter(SalesSetting.tenant_id == tenant_id).first()

    def stored(self, tenant_id: TenantId) -> Optional[dict[str, Any]]:
        """The stored settings document (None when the workspace never saved one)."""
        row = self._row(tenant_id)
        return row.settings if row else None

    def save(self, tenant_id: TenantId, settings: dict[str, Any]) -> None:
        row = self._row(tenant_id)
        if row:
            row.settings = settings
        else:
            self._session.add(SalesSetting(tenant_id=tenant_id, settings=settings))
        self._session.flush()


class SalesAdminRepository(Protocol):
    def count_users(self, tenant_id: TenantId, user_ids: Iterable[str]) -> int: ...
    def user_exists(self, tenant_id: TenantId, user_id: str) -> bool: ...
    def target_users(self, tenant_id: TenantId, roles: Sequence[str], visible: Optional[Iterable[str]]) -> list[User]: ...
    def targets(self, tenant_id: TenantId, fy: int) -> list[SalesTarget]: ...
    def target(self, tenant_id: TenantId, user_id: str, fy: int, quarter: int) -> Optional[SalesTarget]: ...
    def rules(self, tenant_id: TenantId) -> list[WorkflowRule]: ...
    def rule(self, tenant_id: TenantId, rule_id: str) -> Optional[WorkflowRule]: ...
    def delete_rule(self, tenant_id: TenantId, rule_id: str) -> int: ...
    def unread_count(self, user_id: str) -> int: ...
    def notifications(self, user_id: str, unread_only: bool, limit: int) -> list[Notification]: ...
    def mark_read(self, user_id: str, ids: Optional[Sequence[str]], now: datetime) -> int: ...
    def template(self, tenant_id: TenantId, template_id: str) -> Optional[MessageTemplate]: ...
    def templates(self, tenant_id: TenantId, user_id: str, category: Optional[str], search: Optional[str],
                  limit: int) -> list[MessageTemplate]: ...
    def template_categories(self, tenant_id: TenantId, user_id: str) -> list[str]: ...
    def add(self, entity: Any) -> Any: ...
    def delete(self, entity: Any) -> None: ...
    def flush(self) -> None: ...


class SqlAlchemySalesAdminRepository(SessionRepository):
    def delete(self, entity: Any) -> None:
        self._session.delete(entity)

    # users
    def count_users(self, tenant_id: TenantId, user_ids: Iterable[str]) -> int:
        return self._session.query(User.user_id).filter(User.user_id.in_(set(user_ids)),
                                                        User.tenant_id == tenant_id).count()

    def user_exists(self, tenant_id: TenantId, user_id: str) -> bool:
        return self._session.query(User.user_id).filter(User.user_id == user_id,
                                                        User.tenant_id == tenant_id).first() is not None

    # targets (BR-SF-08)
    def target_users(self, tenant_id: TenantId, roles: Sequence[str], visible: Optional[Iterable[str]]) -> list[User]:
        users = self._session.query(User).filter(User.tenant_id == tenant_id, User.status == "ACTIVE",
                                                 User.role.in_(tuple(roles)))
        if visible is not None:
            users = users.filter(User.user_id.in_(set(visible)))
        return users.order_by(User.sales_level.is_(None), User.sales_level, User.first_name).all()

    def targets(self, tenant_id: TenantId, fy: int) -> list[SalesTarget]:
        return self._session.query(SalesTarget).filter(SalesTarget.tenant_id == tenant_id, SalesTarget.fy == fy).all()

    def target(self, tenant_id: TenantId, user_id: str, fy: int, quarter: int) -> Optional[SalesTarget]:
        return self._session.query(SalesTarget).filter(SalesTarget.tenant_id == tenant_id,
                                                       SalesTarget.user_id == user_id, SalesTarget.fy == fy,
                                                       SalesTarget.quarter == quarter).first()

    # workflow rules (BR-SF-13)
    def rules(self, tenant_id: TenantId) -> list[WorkflowRule]:
        return self._session.query(WorkflowRule).filter(WorkflowRule.tenant_id == tenant_id) \
            .order_by(WorkflowRule.created_at.desc()).all()

    def rule(self, tenant_id: TenantId, rule_id: str) -> Optional[WorkflowRule]:
        return self._session.query(WorkflowRule).filter(WorkflowRule.rule_id == rule_id,
                                                        WorkflowRule.tenant_id == tenant_id).first()

    def delete_rule(self, tenant_id: TenantId, rule_id: str) -> int:
        return self._session.query(WorkflowRule).filter(WorkflowRule.rule_id == rule_id,
                                                        WorkflowRule.tenant_id == tenant_id).delete()

    # notifications (BR-SF-07): a user's own
    def unread_count(self, user_id: str) -> int:
        return self._session.query(Notification).filter(Notification.user_id == user_id,
                                                        Notification.read_at.is_(None)).count()

    def notifications(self, user_id: str, unread_only: bool, limit: int) -> list[Notification]:
        q = self._session.query(Notification).filter(Notification.user_id == user_id)
        if unread_only:
            q = q.filter(Notification.read_at.is_(None))
        return q.order_by(Notification.created_at.desc()).limit(limit).all()

    def mark_read(self, user_id: str, ids: Optional[Sequence[str]], now: datetime) -> int:
        q = self._session.query(Notification).filter(Notification.user_id == user_id, Notification.read_at.is_(None))
        if ids:
            q = q.filter(Notification.notification_id.in_(list(ids)))
        return q.update({Notification.read_at: now}, synchronize_session=False)

    # template library (BR-SF-10)
    def template(self, tenant_id: TenantId, template_id: str) -> Optional[MessageTemplate]:
        return self._session.query(MessageTemplate).filter(MessageTemplate.template_id == template_id,
                                                           MessageTemplate.tenant_id == tenant_id).first()

    def templates(self, tenant_id: TenantId, user_id: str, category: Optional[str], search: Optional[str],
                  limit: int) -> list[MessageTemplate]:
        query = self._session.query(MessageTemplate).filter(
            MessageTemplate.tenant_id == tenant_id,
            or_(MessageTemplate.shared.is_(True), MessageTemplate.owner_id == user_id))
        if category:
            query = query.filter(MessageTemplate.category == category)
        if search:
            query = query.filter(or_(ilike(MessageTemplate.name, search), ilike(MessageTemplate.subject, search)))
        return query.order_by(MessageTemplate.usage_count.desc(), MessageTemplate.name).limit(limit).all()

    def template_categories(self, tenant_id: TenantId, user_id: str) -> list[str]:
        """Categories of the templates this user can see only (not of other users' private templates)."""
        return sorted({c for (c,) in self._session.query(MessageTemplate.category).filter(
            MessageTemplate.tenant_id == tenant_id, MessageTemplate.category.isnot(None),
            MessageTemplate.category != "",
            or_(MessageTemplate.shared.is_(True), MessageTemplate.owner_id == user_id)).distinct()})
