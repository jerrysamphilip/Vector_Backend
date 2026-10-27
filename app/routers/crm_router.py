# app/routers/crm_router.py
"""Saved views (BR-CM-19) and global search across contacts and companies (BR-CM-21).

Layered like contacts_router (ADR 0009): parse, one call to SavedViewService / ContactQueryService."""

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.schemas.saved_view_schema import ViewWrite
from app.services.contact_query_service import ContactQueryService
from app.services.saved_view_service import SavedViewService

__all__ = ["ViewWrite", "router"]

router = APIRouter(tags=["CRM"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def view_service(db=Depends(get_db)) -> SavedViewService:
    return SavedViewService.for_session(db)


def search_service(db=Depends(get_db)) -> ContactQueryService:
    return ContactQueryService.for_session(db)


@router.get("/views")
def list_views(object_type: str = Query("CONTACT", pattern="^(CONTACT|COMPANY)$"),
               service: SavedViewService = Depends(view_service), current_user: Any = Depends(tenant_user)):
    return service.list(current_user, object_type)


@router.post("/views", status_code=201)
def create_view(payload: ViewWrite, service: SavedViewService = Depends(view_service),
                current_user: Any = Depends(tenant_user)):
    return service.create(current_user, payload)


@router.patch("/views/{view_id}")
def update_view(view_id: str, payload: ViewWrite, service: SavedViewService = Depends(view_service),
                current_user: Any = Depends(tenant_user)):
    return service.update(current_user, view_id, payload)


@router.delete("/views/{view_id}")
def delete_view(view_id: str, service: SavedViewService = Depends(view_service),
                current_user: Any = Depends(tenant_user)):
    return service.delete(current_user, view_id)


@router.get("/search")
def global_search(q: str = Query(..., min_length=2, max_length=100), limit: int = Query(8, ge=1, le=25),
                  service: ContactQueryService = Depends(search_service), current_user: Any = Depends(tenant_user)):
    """Contacts by name, email, phone, company or domain, and companies by name or domain."""
    return service.global_search(current_user, q, limit)
