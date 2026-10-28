"""
Audit log repository (ADR 0009): append-only audit entries and their tenant-scoped reads. Never
commits (``add`` flushes so the entry gets its id inside the caller's transaction).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.repositories.base import TenantId

__all__ = ["Session", "SqlAlchemyAuditRepository"]


class SqlAlchemyAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, log: AuditLog) -> AuditLog:
        self._session.add(log)
        self._session.flush()  # the id without committing
        return log

    def trail(self, tenant_id: TenantId, entity_type: Optional[str], entity_id: Optional[str],
              user_id: Optional[str], limit: int) -> list[AuditLog]:
        """The tenant's entries, newest first, optionally narrowed by entity and user."""
        query = self._session.query(AuditLog).filter(AuditLog.tenant_id == tenant_id)
        if entity_type:
            query = query.filter(AuditLog.entity_type == entity_type)
        if entity_id:
            query = query.filter(AuditLog.entity_id == entity_id)
        if user_id:
            query = query.filter(AuditLog.user_id == user_id)
        return query.order_by(AuditLog.created_at.desc()).limit(limit).all()

    def entity_history(self, entity_type: str, entity_id: str, limit: int) -> list[AuditLog]:
        """History of one entity (ids are global UUIDs; callers check the entity's tenant first)."""
        return self._session.query(AuditLog).filter(
            AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id,
        ).order_by(AuditLog.created_at.desc()).limit(limit).all()
