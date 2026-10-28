# app/routers/exports_router.py
"""Export audit log (gap list C-07): GET /exports/logs -- who exported what, when. Admins only."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.paging import Paging
from app.services.export_log import ExportLogService

router = APIRouter(prefix="/exports", tags=["Exports"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def export_log_service(db=Depends(get_db)) -> ExportLogService:
    return ExportLogService.for_session(db)


@router.get("/logs", response_model=None)
def export_logs(
    object_type: Optional[str] = Query(None, pattern="^(CONTACT|LIST|COMPANY)$"),
    user_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    service: ExportLogService = Depends(export_log_service),
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    return service.page(current_user, object_type, user_id, Paging(page, page_size))
