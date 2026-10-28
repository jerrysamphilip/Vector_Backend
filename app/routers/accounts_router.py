# app/routers/accounts_router.py
"""
Companies (BR-CM-03/04). Each company has a unique domain per workspace; contacts are
associated automatically from their email domain, or by company name for personal email
addresses. Renaming a company updates the company name on its contacts. Deleting is a
soft delete: the company is hidden (contacts keep their link) and can be restored for
90 days.

Layered (ADR 0009, chunk A1, following contacts_router): each handler parses the request into domain
objects (app/domain/accounts.py, app/domain/paging.py) and makes one call to a service --
AccountService (companies), AccountBackfillService (backfill preview / runs / undo, C-14) or
ContactFieldService for COMPANY (company custom properties, C-16) -- injected through the providers below,
so tests swap them with ``app.dependency_overrides``. Handlers carry return type hints with
``response_model=None`` so the hints document the code without turning into response models.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import require_role
from app.core.concurrency import if_match_version
from app.core.database import get_db
from app.domain.accounts import AccountFilter, account_sort
from app.domain.paging import Paging
from app.schemas.account_schema import AccountUpdate, AccountWrite
from app.schemas.contact_schema import FieldWrite
from app.services.account_backfill_service import AccountBackfillService
from app.services.account_service import AccountService
from app.services.contact_field_service import COMPANY, ContactFieldService
from app.utils.http_files import content_disposition

__all__ = ["AccountUpdate", "AccountWrite", "router"]

router = APIRouter(prefix="/accounts", tags=["Companies"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


# Service providers: one unit of work on the request's session (get_db)
def account_service(db=Depends(get_db)) -> AccountService:
    return AccountService.for_session(db)


def backfill_service(db=Depends(get_db)) -> AccountBackfillService:
    return AccountBackfillService.for_session(db)


def company_field_service(db=Depends(get_db)) -> ContactFieldService:
    return ContactFieldService.for_session(db, COMPANY)


Accounts = Depends(account_service)
Backfill = Depends(backfill_service)
Fields = Depends(company_field_service)


@router.get("", response_model=None)
def list_accounts(
    q: Optional[str] = None,
    owner: Optional[str] = Query(None, description="'me', 'unassigned' or a user id"),
    lifecycle_stage: Optional[str] = None,
    industry: Optional[str] = None,
    sort_by: str = Query("name", pattern="^(name|contacts|annual_revenue|created_at|updated_at)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    filters: Optional[str] = Query(None, description="JSON AND/OR filter on company properties and custom.<key>"),
    status: str = Query("active", pattern="^(active|archived)$",
                        description="'archived': companies in the recycle bin (managers)"),
    service: AccountService = Accounts,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    f = AccountFilter(q=q, owner=owner, lifecycle_stage=lifecycle_stage, industry=industry, filters=filters,
                      status="archived" if status == "archived" else "active")
    return service.list_page(current_user, f, account_sort(sort_by, sort_order), Paging(page, page_size))


@router.get("/meta", response_model=None)
def account_meta(service: AccountService = Accounts, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Table / export columns (including company custom properties) for the UI."""
    return service.meta(current_user)


@router.get("/export")
def export_accounts(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    columns: Optional[str] = Query(None, description="Comma-separated column keys"),
    q: Optional[str] = None,
    owner: Optional[str] = None,
    lifecycle_stage: Optional[str] = None,
    industry: Optional[str] = None,
    filters: Optional[str] = None,
    status: str = Query("active", pattern="^(active|archived)$"),
    service: AccountService = Accounts,
    current_user: Any = Depends(tenant_user),
) -> StreamingResponse:
    """Export a company view to CSV / XLSX: same right as the contact export (C-07); refused over the row limit."""
    f = AccountFilter(q=q, owner=owner, lifecycle_stage=lifecycle_stage, industry=industry, filters=filters,
                      status="archived" if status == "archived" else "active")
    export = service.export(current_user, format, columns, f)
    return StreamingResponse(iter([export.content]), media_type=export.media_type,
                             headers={"Content-Disposition": content_disposition(export.filename)})


# Company custom properties (C-16)
@router.get("/fields", response_model=None)
def list_company_fields(include_inactive: bool = Query(False, description="Also list deactivated properties"),
                        service: ContactFieldService = Fields,
                        current_user: Any = Depends(tenant_user)) -> List[dict]:
    return service.list(current_user, include_inactive)


@router.post("/fields", status_code=201, response_model=None)
def create_company_field(payload: FieldWrite, service: ContactFieldService = Fields,
                         current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.create(current_user, payload)


@router.patch("/fields/{field_id}", response_model=None)
def update_company_field(field_id: str, payload: FieldWrite, service: ContactFieldService = Fields,
                         current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.update(current_user, field_id, payload)


@router.delete("/fields/{field_id}", response_model=None)
def delete_company_field(field_id: str, service: ContactFieldService = Fields,
                         current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Deactivates the property (C-17): hidden from forms and import mapping; values stay on companies and are
    still exported. Reactivate with PATCH ``is_active: true``. System properties cannot be deactivated."""
    return service.delete(current_user, field_id)


@router.post("", status_code=201, response_model=None)
def create_account(payload: AccountWrite, service: AccountService = Accounts,
                   current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.create(current_user, payload)


@router.post("/backfill", response_model=None)
def backfill(service: AccountBackfillService = Backfill, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """
    Associate every contact without a company (BR-CM-04): by email domain, creating the
    company if needed, or by company name for personal email addresses.
    """
    return service.legacy(current_user)


# Backfill preview / run history / undo (C-14)
@router.post("/backfill/previews", response_model=None)
def preview_backfill(service: AccountBackfillService = Backfill,
                     current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Dry run: what a backfill would create, link and refuse, with samples. Writes nothing."""
    return service.preview(current_user)


@router.post("/backfill/runs", status_code=201, response_model=None)
def run_backfill(service: AccountBackfillService = Backfill,
                 current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Run the backfill in batches and record what it did, so it can be undone."""
    return service.run(current_user)


@router.get("/backfill/runs", response_model=None)
def list_backfill_runs(limit: int = Query(20, ge=1, le=100), service: AccountBackfillService = Backfill,
                       current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.list_runs(current_user, limit)


@router.post("/backfill/runs/{run_id}/undo", response_model=None)
def undo_backfill_run(run_id: str, service: AccountBackfillService = Backfill,
                      current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Remove the links and companies the run created and restore the contact fields it filled."""
    return service.undo(current_user, run_id)


@router.get("/deleted", response_model=None)
def deleted_accounts(service: AccountService = Accounts, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.deleted(current_user)


@router.post("/{account_id}/restore", response_model=None)
def restore_account(account_id: str, service: AccountService = Accounts,
                    current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.restore(current_user, account_id)


@router.get("/{account_id}", response_model=None)
def get_account(account_id: str, service: AccountService = Accounts,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.get(current_user, account_id)


@router.patch("/{account_id}", response_model=None)
def update_account(account_id: str, payload: AccountUpdate, service: AccountService = Accounts,
                   current_user: Any = Depends(tenant_user),
                   if_match: Optional[int] = Depends(if_match_version)) -> Dict[str, Any]:
    return service.update(current_user, account_id, payload, if_match)


@router.delete("/{account_id}", response_model=None)
def delete_account(account_id: str, service: AccountService = Accounts,
                   current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Soft delete: hidden and restorable for 90 days; contacts are kept."""
    return service.delete(current_user, account_id)
