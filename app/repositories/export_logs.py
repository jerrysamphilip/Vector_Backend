"""
Export audit log storage (gap list C-07): rows are added by ``app.services.export_log.record_export`` inside
the exporting service's transaction and read by admins at GET /exports/logs. Nothing here commits.
"""

from __future__ import annotations

from typing import Optional, Protocol

from sqlalchemy.orm import Session

from app.domain.paging import Page, Paging
from app.models.export_log import ExportLog
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId

__all__ = ["ExportLogRepository", "ExportLogsUnitOfWork", "SqlAlchemyExportLogRepository"]


class ExportLogRepository(Protocol):
    def add(self, log: ExportLog) -> None: ...
    def page(self, tenant_id: TenantId, object_type: Optional[str], user_id: Optional[str],
             paging: Paging) -> Page[ExportLog]: ...
    def user_names(self, ids: set[str]) -> dict[str, str]: ...


class SqlAlchemyExportLogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, log: ExportLog) -> None:
        self._session.add(log)

    def page(self, tenant_id: TenantId, object_type: Optional[str], user_id: Optional[str],
             paging: Paging) -> Page[ExportLog]:
        query = self._session.query(ExportLog).filter(ExportLog.tenant_id == tenant_id)
        if object_type:
            query = query.filter(ExportLog.object_type == object_type)
        if user_id:
            query = query.filter(ExportLog.user_id == user_id)
        total = query.count()
        rows = query.order_by(ExportLog.created_at.desc(), ExportLog.export_id).offset(paging.offset).limit(
            paging.limit).all()
        return Page(items=rows, total=total, paging=paging)

    def user_names(self, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        return {u.user_id: f"{u.first_name} {u.last_name}".strip()
                for u in self._session.query(User).filter(User.user_id.in_(ids))}


class ExportLogsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> ExportLogsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> ExportLogsUnitOfWork:
        super().__enter__()
        return self

    @property
    def logs(self) -> SqlAlchemyExportLogRepository:
        return SqlAlchemyExportLogRepository(self.session)
