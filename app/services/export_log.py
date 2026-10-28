"""
Export audit log (gap list C-07): every export of contacts or a list leaves a row saying who exported
which object, with which view or filter, how many rows, in which format and when. Admins read the log
at GET /exports/logs (``ExportLogService``).

``record_export`` is called by the exporting service inside its own unit of work, so the row is saved
with that service's commit. The log never holds the exported rows themselves.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.core.errors import PermissionDenied
from app.domain.paging import Paging
from app.models.export_log import ExportLog
from app.models.user import User
from app.repositories.export_logs import ExportLogRepository, ExportLogsUnitOfWork, SqlAlchemyExportLogRepository

__all__ = ["EXPORT_LOG_ROLES", "ExportLogService", "record_export"]

EXPORT_LOG_ROLES = ("SUPER_ADMIN", "ADMIN")
OBJECT_TYPES = ("CONTACT", "LIST", "COMPANY")
SUMMARY_MAX = 2000


def _repository(uow_or_session: Any) -> ExportLogRepository:
    """The log repository for a unit of work (``.session``), a session, or a fake that is one."""
    if hasattr(uow_or_session, "add") and hasattr(uow_or_session, "page"):
        return uow_or_session  # already an ExportLogRepository (tests)
    return SqlAlchemyExportLogRepository(getattr(uow_or_session, "session", uow_or_session))


def record_export(uow_or_session: Any, tenant_id: str, user_id: Optional[str], object_type: str,
                  filter_summary: Optional[str], rows: int, fmt: str) -> ExportLog:
    """Stage one export-log row in the caller's transaction (the caller commits)."""
    log = ExportLog(tenant_id=tenant_id, user_id=user_id, object_type=object_type.upper(),
                    filter_summary=filter_summary[:SUMMARY_MAX] if filter_summary else None,
                    row_count=int(rows), format=fmt.lower())
    _repository(uow_or_session).add(log)
    return log


def log_dict(log: ExportLog, names: dict[str, str]) -> dict:
    return {
        "export_id": log.export_id, "user_id": log.user_id, "user_name": names.get(log.user_id or ""),
        "object_type": log.object_type, "filter_summary": log.filter_summary, "row_count": log.row_count,
        "format": log.format, "created_at": log.created_at,
    }


class ExportLogService:
    """GET /exports/logs: the workspace's export log, newest first. Admins only."""

    def __init__(self, uow: ExportLogsUnitOfWork,
                 logs: Callable[[Any], ExportLogRepository] = SqlAlchemyExportLogRepository) -> None:
        self.uow = uow
        self._logs = logs

    @classmethod
    def for_session(cls, db: Any) -> "ExportLogService":
        return cls(ExportLogsUnitOfWork.for_session(db))

    def page(self, user: User, object_type: Optional[str], user_id: Optional[str], paging: Paging) -> dict:
        if user.role not in EXPORT_LOG_ROLES:
            raise PermissionDenied("Only admins can see the export log")
        with self.uow as uow:
            repo = self._logs(uow.session)
            page = repo.page(user.tenant_id, object_type.upper() if object_type else None, user_id, paging)
            names = repo.user_names({log.user_id for log in page.items if log.user_id})
            return {"items": [log_dict(log, names) for log in page.items], "total": page.total,
                    "page": paging.page, "page_size": paging.page_size}
