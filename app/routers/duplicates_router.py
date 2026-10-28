# app/routers/duplicates_router.py
"""
Duplicate review and contact identity history (BR-CM-32/33; gap list C-08, C-09, C-10, C-11, C-19, F-21).

    GET  /contacts/duplicates/pairs            stored suggestions (status OPEN / DISMISSED / MERGED), paged
    POST /contacts/duplicates/scan             rescan the workspace now
    POST /contacts/duplicates/{pair_id}/dismiss   "not the same person" (never suggested again)
    POST /contacts/duplicates/{pair_id}/reopen
    POST /contacts/merge/preview               per-property values, default choices, what moves, warnings
    GET  /contacts/{prospect_id}/merges        merge history of a contact (snapshots of merged records)
    GET  /contacts/{prospect_id}/emails        past e-mail addresses (retired / merged)

GET /contacts/duplicates (the stored OPEN pairs, grouped) and POST /contacts/merge stay in
app/routers/contacts_router.py. Each handler makes one DuplicateService call.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.paging import Paging
from app.schemas.duplicate_schema import MergePreviewRequest
from app.services.duplicate_service import DuplicateService

router = APIRouter(prefix="/contacts", tags=["Contacts"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def duplicate_service(db=Depends(get_db)) -> DuplicateService:
    return DuplicateService.for_session(db)


Service = Depends(duplicate_service)


@router.get("/duplicates/pairs", response_model=None)
def duplicate_pairs(
    status: str = Query("OPEN", pattern="^(OPEN|DISMISSED|MERGED)$"),
    confidence: Optional[str] = Query(None, pattern="^(HIGH|MEDIUM)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: DuplicateService = Service,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    return service.pairs(current_user, status, confidence, Paging(page, page_size))


@router.post("/duplicates/scan", response_model=None)
def scan_duplicates(service: DuplicateService = Service, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.scan(current_user)


@router.post("/duplicates/{pair_id}/dismiss", response_model=None)
def dismiss_pair(pair_id: str, service: DuplicateService = Service,
                 current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.dismiss(current_user, pair_id)


@router.post("/duplicates/{pair_id}/reopen", response_model=None)
def reopen_pair(pair_id: str, service: DuplicateService = Service,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.reopen(current_user, pair_id)


@router.post("/merge/preview", response_model=None)
def merge_preview(payload: MergePreviewRequest, service: DuplicateService = Service,
                  current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.merge_preview(current_user, payload.primary_id, payload.duplicate_ids)


@router.get("/{prospect_id}/merges", response_model=None)
def merge_history(prospect_id: str, limit: int = Query(50, ge=1, le=200), service: DuplicateService = Service,
                  current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.merge_history(current_user, prospect_id, limit)


@router.get("/{prospect_id}/emails", response_model=None)
def email_history(prospect_id: str, service: DuplicateService = Service,
                  current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.email_history(current_user, prospect_id)
