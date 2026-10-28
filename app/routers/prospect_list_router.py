# app/routers/prospect_list_router.py
"""
Prospect List Management API endpoints: upload lists (/prospect-lists), enrolling lists into a campaign,
single-prospect edit / delete, list re-validation, the upload flow and the prospect KPI cards.

Layered (ADR 0009, chunk A1): each handler makes one ProspectListService call (the upload steps call
app/services/prospect_upload_service.py); request / response models live in app/schemas/list_schema.py.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.core.auth import require_permission, require_role
from app.core.concurrency import if_match_version
from app.core.database import get_db
from app.core.errors import DomainError
from app.domain.lists import ProspectListFilter, prospect_list_sort
from app.domain.paging import Paging
from app.schemas.list_schema import (
    EnrollmentRequest,
    EnrollmentResult,
    ProspectInListResponse,
    ProspectListDetailResponse,
    ProspectListPaginatedResponse,
    ProspectListResponse,
    ProspectNoteUpdate,
    ProspectStatsResponse,
    ProspectUpdateRequest,
    RevalidationResponse,
    UploadConfirmRequest,
    UploadRevalidateRequest,
)
from app.services.prospect_list_service import ProspectListService
from app.services.prospect_upload_service import confirm_upload, dry_run_validate, revalidate_prospect_records
from app.utils.error_utils import handle_route_error
from app.utils.excel_reader import read_upload_file

logger = logging.getLogger(__name__)

# Re-exported: callers and tests import the response models from here
__all__ = ["ProspectInListResponse", "ProspectListResponse", "prospect_upload_router", "router"]

# No router prefix: every route spells out its full path
router = APIRouter(tags=["Prospect Lists"])

# Secondary router for /prospects prefix (upload & stats endpoints)
prospect_upload_router = APIRouter(tags=["Prospects"])

any_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
manage_prospects = [Depends(require_permission("manage_prospects"))]


def prospect_list_service(db=Depends(get_db)) -> ProspectListService:
    return ProspectListService.for_session(db)


Lists = Depends(prospect_list_service)


# =============================
# ENDPOINTS
# =============================

@router.get("/prospect-lists", response_model=ProspectListPaginatedResponse)
def list_prospect_lists(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("uploaded_at", pattern="^(list_name|prospect_count|uploaded_at)$"),
    sort_order: Optional[str] = Query("desc", pattern="^(asc|desc)$"),
    service: ProspectListService = Lists,
    current_user: Any = Depends(any_user),
):
    """
    Get all prospect lists with member counts, pagination, sorting, and search.
    """
    try:
        f = ProspectListFilter(search=search or None, sort=prospect_list_sort(sort_by, sort_order))
        return service.page(current_user, f, Paging(page, page_size))
    except Exception as e:
        handle_route_error(e, context="list_prospect_lists")


@router.get("/prospect-lists/{list_id}/prospects", response_model=ProspectListDetailResponse)
def get_list_prospects(
    list_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    service: ProspectListService = Lists,
    current_user: Any = Depends(any_user),
):
    """
    Get prospects in a specific list with pagination and search.
    """
    try:
        return service.prospects(current_user, list_id, search or None, Paging(page, page_size))
    except Exception as e:
        handle_route_error(e, context="get_list_prospects")


@router.post("/campaigns/{campaign_id}/enrollments/bulk", response_model=EnrollmentResult, dependencies=manage_prospects)
def enroll_prospects(
    campaign_id: str,
    request: EnrollmentRequest,
    service: ProspectListService = Lists,
    current_user: Any = Depends(tenant_user),
):
    """
    Enroll prospects from selected lists into a campaign.

    Performs safety checks:
    - Global unsubscribe check
    - Consent status check
    - Already enrolled check
    - Email validity check
    """
    return service.enroll(current_user, campaign_id, request.list_ids)


@router.delete("/prospect-lists/{list_id}", dependencies=manage_prospects)
def delete_prospect_list(
    list_id: str,
    service: ProspectListService = Lists,
    current_user: Any = Depends(tenant_user),
):
    """
    Delete a prospect list and its member associations.
    Note: Does not delete the prospects themselves, only the list and memberships.
    """
    try:
        return service.delete(current_user, list_id)
    except Exception as e:
        handle_route_error(e, context="delete_prospect_list")


# =============================
# PROSPECT UPDATE ENDPOINT
# =============================

@router.patch("/prospect-lists/{list_id}/prospects/{prospect_id}/notes", dependencies=manage_prospects)
def update_prospect_note(
    list_id: str,
    prospect_id: str,
    payload: ProspectNoteUpdate,
    service: ProspectListService = Lists,
    current_user: Any = Depends(tenant_user),
):
    """
    Update the note for a prospect within a specific list.
    """
    try:
        return service.update_note(current_user, list_id, prospect_id, payload.notes)
    except Exception as e:
        handle_route_error(e, context="update_prospect_note")


@router.patch("/prospects/{prospect_id}", dependencies=manage_prospects)
def update_prospect(
    prospect_id: str,
    updates: ProspectUpdateRequest,
    service: ProspectListService = Lists,
    current_user: Any = Depends(tenant_user),
    if_match: Optional[int] = Depends(if_match_version),
):
    """
    Update individual prospect fields: the same rules as PATCH /contacts/{id} (owner/team
    visibility, unique and unsuppressed email, property history, timezone from state).

    Send the contact's row_version as expected_version or If-Match (T-3); stale -> 409.
    """
    data = updates.model_dump(exclude_unset=True)
    if data.get("email") is None:
        data.pop("email", None)
    try:
        return service.update_prospect(current_user, prospect_id, data, if_match)
    except Exception as e:
        handle_route_error(e, context="update_prospect")


@router.delete("/prospects/{prospect_id}", dependencies=manage_prospects)
def delete_prospect(
    prospect_id: str,
    service: ProspectListService = Lists,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Delete an individual prospect and all related records.
    """
    return service.delete_prospect(current_user, prospect_id)


# =============================
# REVALIDATE LIST ENDPOINT
# =============================

@router.post("/prospect-lists/{list_id}/revalidations", dependencies=manage_prospects)
def revalidate_list(
    list_id: str,
    service: ProspectListService = Lists,
    current_user: Any = Depends(tenant_user),
):
    """
    Re-validate all prospects in a list.
    Checks:
    - Email format validity
    - Personal email domains (gmail, yahoo, etc.)
    - Globally unsubscribed status
    - Missing required fields (POC State)
    """
    return RevalidationResponse(**service.revalidate(current_user, list_id))


# =============================
# PROSPECT UPLOAD & STATS (prefix: /prospects)
# =============================

@prospect_upload_router.post("/uploads/validations", dependencies=manage_prospects)
def upload_dry_run(
    file: UploadFile = File(...),
    db=Depends(get_db),
    current_user: Any = Depends(tenant_user),
):
    """
    Step 1: Dry-run validation of uploaded Excel file.
    NO database writes happen here.
    """
    try:
        df = read_upload_file(file)

        upload_id, summary = dry_run_validate(
            df=df,
            db=db,
            tenant_id=current_user.tenant_id,
            uploaded_by=current_user.user_id,
            file_name=file.filename or "uploaded_file",
        )

        response = {
            "upload_id": upload_id,
            **summary,
        }

        if "accepted" in summary:
            response["valid"] = summary["accepted"]

        return response
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="upload")


@prospect_upload_router.post("/uploads/{upload_id}/revalidations", dependencies=manage_prospects)
def upload_revalidate(
    upload_id: str,
    payload: UploadRevalidateRequest,
    db=Depends(get_db),
    current_user: Any = Depends(tenant_user),
):
    """
    Step 1.5: Re-validate edited records of the upload in the path (404 for an id that is not an
    upload id or that another workspace holds).
    """
    try:
        return revalidate_prospect_records(
            records=payload.records,
            db=db,
            tenant_id=current_user.tenant_id,
            upload_id=upload_id,
        )
    except (HTTPException, DomainError):
        raise
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="upload")


@prospect_upload_router.post("/uploads/{upload_id}/confirmations", dependencies=manage_prospects)
def upload_confirm(
    upload_id: str,
    payload: UploadConfirmRequest,
    db=Depends(get_db),
    current_user: Any = Depends(tenant_user),
):
    """
    Step 2: Confirm upload and persist prospects.

    Every row is validated again on the server; rows that fail (or that the database refuses)
    are returned in `not_added` with a reason and the rest are saved. Confirming the same
    upload_id again returns the stored result (`already_confirmed`).

    The path id is the upload: a body `upload_id` that differs is a 400; an id that is not an upload id
    or that another workspace holds is a 404.
    """
    if payload.upload_id != upload_id:
        raise HTTPException(status_code=400, detail="upload_id in the body does not match the upload in the URL")
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Upload title is required")

    try:
        return confirm_upload(
            upload_id=upload_id,
            title=payload.title,
            records=payload.records,
            db=db,
            tenant_id=current_user.tenant_id,
            uploaded_by=current_user.user_id,
        )
    except (HTTPException, DomainError):
        raise
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="upload")


@prospect_upload_router.get("/prospects/stats")
def get_prospect_stats(
    service: ProspectListService = Lists,
    current_user: Any = Depends(any_user),
) -> ProspectStatsResponse:
    """
    Get prospect statistics for KPI cards.
    """
    return ProspectStatsResponse(**service.stats(current_user))
