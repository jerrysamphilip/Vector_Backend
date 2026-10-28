# app/routers/lists_router.py
"""
Contact lists for the CRM (BR-CM-22/23/24).

- STATIC lists hold a fixed set of contacts, added by hand, by bulk action or by import.
- ACTIVE lists are a saved filter: membership is evaluated whenever the list is read or
  used as a campaign audience, so it is always current.

Upload lists from the Prospects page are static lists too. Deleting a list here never
deletes contacts (unlike the Prospects page's upload-list delete). List names are unique per
workspace (409 on a clash, C-21). GET /lists pages when ``page`` / ``page_size`` are sent.

Layered (ADR 0009, chunk A1): each handler builds domain objects and makes one ListService call.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.lists import ListFilter
from app.domain.paging import Paging
from app.schemas.list_schema import FilterPreview, ListWrite, Members
from app.services.list_service import ListService
from app.utils.http_files import content_disposition

router = APIRouter(prefix="/lists", tags=["Lists"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def list_service(db=Depends(get_db)) -> ListService:
    return ListService.for_session(db)


Lists = Depends(list_service)


@router.get("", response_model=None)
def list_lists(
    list_type: Optional[str] = Query(None, pattern="^(STATIC|ACTIVE)$"),
    q: Optional[str] = None,
    page: Optional[int] = Query(None, ge=1, description="Send page / page_size for a paged response"),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    service: ListService = Lists,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    paging = Paging(page or 1, page_size or 50) if page is not None or page_size is not None else None
    return service.list(current_user, ListFilter(list_type=list_type, q=q), paging)


@router.post("/preview", response_model=None)
def preview_filters(payload: FilterPreview, service: ListService = Lists,
                    current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """How many contacts a filter matches right now (for building active lists)."""
    return service.preview(current_user, payload.filters)


@router.post("", status_code=201, response_model=None)
def create_list(payload: ListWrite, service: ListService = Lists,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.create(current_user, payload.list_name, payload.list_type, payload.filters, payload.description)


@router.get("/{list_id}", response_model=None)
def get_list(list_id: str, service: ListService = Lists, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.get(current_user, list_id)


@router.patch("/{list_id}", response_model=None)
def update_list(list_id: str, payload: ListWrite, service: ListService = Lists,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.update(current_user, list_id, payload.model_dump(exclude_unset=True))


@router.delete("/{list_id}", response_model=None)
def delete_list(list_id: str, service: ListService = Lists,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Delete the list only; its contacts are kept."""
    return service.delete(current_user, list_id)


@router.post("/{list_id}/members", response_model=None)
def add_members(list_id: str, payload: Members, service: ListService = Lists,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.add_members(current_user, list_id, payload.prospect_ids)


@router.post("/{list_id}/members/remove", response_model=None)
def remove_members(list_id: str, payload: Members, service: ListService = Lists,
                   current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.remove_members(current_user, list_id, payload.prospect_ids)


@router.get("/{list_id}/export")
def export_list(
    list_id: str,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    columns: Optional[str] = Query(None, description="Comma-separated column keys"),
    service: ListService = Lists,
    current_user: Any = Depends(tenant_user),
) -> StreamingResponse:
    """The list's contacts as CSV / XLSX (admins and managers, like the contacts export); logged (C-07)."""
    export = service.export(current_user, list_id, format, columns)
    return StreamingResponse(iter([export.content]), media_type=export.media_type,
                             headers={"Content-Disposition": content_disposition(export.filename)})
