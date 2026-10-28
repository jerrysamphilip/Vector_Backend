# app/routers/contacts_router.py
"""
Contact management API (BRD v2.0, section 5.2): every contact in the workspace in one
place, with views and AND/OR filters, a record page with a unified timeline, lifecycle
stage and lead status, custom properties, property history, bulk actions, dedupe and
merge, export, and soft delete with restore.

Users with the manage_prospects permission (Admin) see every contact. Everyone else
(User) sees and edits only the contacts they own, and can't reassign, bulk edit, merge,
export or delete.

Layered (ADR 0009, the L1 reference): each handler parses the request into domain objects
(app/domain/contacts.py, app/domain/paging.py) and makes one call to a service --
ContactQueryService (reads), ContactWriteService (writes), ContactFieldService (custom properties)
or ContactActivityService (activities) -- injected through the providers below, so tests swap them
with ``app.dependency_overrides``. Handlers carry return type hints with ``response_model=None`` so
the hints document the code without turning into response models (the OpenAPI schema and
serialisation stay as they were).
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import require_role
from app.core.concurrency import if_match_version
from app.core.database import get_db
from app.domain.contacts import ContactFilter, contact_sort
from app.domain.paging import Paging
from app.schemas.contact_schema import (
    ActivityUpdate,
    ActivityWrite,
    BulkAction,
    CompanyLinkCreate,
    CompanyLinkUpdate,
    ContactCreate,
    ContactUpdate,
    FieldWrite,
    MergeRequest,
    RestoreRequest,
)
from app.services.contact_activity_service import ContactActivityService
from app.services.contact_field_service import ContactFieldService
from app.services.contact_query_service import ContactQueryService
from app.services.contact_write_service import ContactWriteService
from app.services.task_service import task_dict
from app.utils.http_files import content_disposition

router = APIRouter(prefix="/contacts", tags=["Contacts"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


# Service providers: one unit of work on the request's session (get_db)
def query_service(db=Depends(get_db)) -> ContactQueryService:
    return ContactQueryService.for_session(db)


def write_service(db=Depends(get_db)) -> ContactWriteService:
    return ContactWriteService.for_session(db)


def field_service(db=Depends(get_db)) -> ContactFieldService:
    return ContactFieldService.for_session(db)


def activity_service(db=Depends(get_db)) -> ContactActivityService:
    return ContactActivityService.for_session(db)


Reads = Depends(query_service)
Writes = Depends(write_service)
Fields = Depends(field_service)
Activities = Depends(activity_service)


# =============================
# COLLECTION
# =============================

@router.get("/meta", response_model=None)
def contact_meta(current_user: Any = Depends(tenant_user), service: ContactQueryService = Reads) -> Dict[str, Any]:
    """Picklist values and table columns for the UI."""
    return service.meta(current_user)


@router.get("", response_model=None)
def list_contacts(
    q: Optional[str] = Query(None, description="Search name, email, company or phone"),
    owner: Optional[str] = Query(None, description="'me', 'unassigned' or a user id"),
    account_id: Optional[str] = None,
    tag: Optional[str] = None,
    list_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    consent_status: Optional[str] = None,
    email_valid: Optional[bool] = None,
    has_phone: Optional[bool] = None,
    country: Optional[str] = None,
    industry: Optional[str] = None,
    lifecycle_stage: Optional[str] = None,
    lead_status: Optional[str] = None,
    filters: Optional[str] = Query(None, description="JSON AND/OR filter, see app/services/crm.py"),
    sort_by: str = Query("created_at", pattern="^(name|email|company|lifecycle_stage|lead_status|created_at|updated_at|score)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    grade: Optional[str] = Query(None, max_length=20, description="Score grade(s): A, B, C, D, comma-separated"),
    score_min: Optional[int] = Query(None, ge=0, le=100),
    score_max: Optional[int] = Query(None, ge=0, le=100),
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    f = ContactFilter(
        q=q, owner=owner, account_id=account_id, tag=tag, list_id=list_id, campaign_id=campaign_id,
        consent_status=consent_status, email_valid=email_valid, has_phone=has_phone, country=country,
        industry=industry, lifecycle_stage=lifecycle_stage, lead_status=lead_status, filters=filters,
        grade=grade, score_min=score_min, score_max=score_max)
    return service.list_page(current_user, f, contact_sort(sort_by, sort_order), Paging(page, page_size))


@router.get("/board", response_model=None)
def contacts_board(
    group_by: str = Query("lifecycle_stage", pattern="^(lifecycle_stage|lead_status)$"),
    per_column: int = Query(50, ge=1, le=200),
    q: Optional[str] = None,
    owner: Optional[str] = None,
    filters: Optional[str] = None,
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    """Board view: contacts grouped by lifecycle stage or lead status (BR-CM-20)."""
    return service.board(current_user, group_by, per_column, ContactFilter(q=q, owner=owner, filters=filters))


@router.post("", status_code=201, response_model=None)
def create_contact(
    payload: ContactCreate,
    service: ContactWriteService = Writes,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    return service.create(current_user, payload)


@router.get("/facets", response_model=None)
def contact_facets(service: ContactQueryService = Reads, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Values for the filter dropdowns: tags, countries, industries, lists and campaigns."""
    return service.facets(current_user)


@router.get("/owners", response_model=None)
def contact_owners(service: ContactQueryService = Reads, current_user: Any = Depends(tenant_user)) -> List[dict]:
    """Active users in the workspace, for owner pickers."""
    return service.owners(current_user)


@router.get("/mentionable-users", response_model=None)
def mentionable_users(q: Optional[str] = Query(None, max_length=100), limit: int = Query(20, ge=1, le=50),
                      prospect_id: Optional[str] = Query(None, max_length=36),
                      opportunity_id: Optional[str] = Query(None, max_length=36),
                      service: ContactQueryService = Reads, current_user: Any = Depends(tenant_user)) -> List[dict]:
    """
    Colleagues who can be @mentioned in a note: any active user of the workspace (BR-CM-16).
    With prospect_id (contact notes) or opportunity_id (deal notes), only those who can see that
    record, matching the check made when the note is saved.
    """
    return service.mentionable_users(current_user, q, limit, prospect_id, opportunity_id)


@router.get("/export")
def export_contacts(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    columns: Optional[str] = Query(None, description="Comma-separated column keys"),
    q: Optional[str] = None,
    owner: Optional[str] = None,
    list_id: Optional[str] = None,
    lifecycle_stage: Optional[str] = None,
    lead_status: Optional[str] = None,
    tag: Optional[str] = None,
    filters: Optional[str] = None,
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> StreamingResponse:
    """Export a view to CSV / XLSX. Admins and managers only (BR-CM-31)."""
    f = ContactFilter(q=q, owner=owner, list_id=list_id, lifecycle_stage=lifecycle_stage,
                      lead_status=lead_status, tag=tag, filters=filters)
    export = service.export(current_user, format, columns, f)
    return StreamingResponse(iter([export.content]), media_type=export.media_type,
                             headers={"Content-Disposition": content_disposition(export.filename)})


@router.post("/bulk", response_model=None)
def bulk_update(
    payload: BulkAction,
    service: ContactWriteService = Writes,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    """Bulk edit, assign, tag, add to list, enroll or delete (BR-CM-36/37)."""
    return service.bulk(current_user, payload)


# =============================
# RECENTLY DELETED (BR-CM-35)
# =============================

@router.get("/deleted", response_model=None)
def deleted_contacts(
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    return service.deleted_page(current_user, q, Paging(page, page_size))


@router.post("/restore", response_model=None)
def restore(payload: RestoreRequest, service: ContactWriteService = Writes,
            current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.restore(current_user, payload.prospect_ids)


@router.post("/purge", response_model=None)
def purge_deleted(payload: RestoreRequest, service: ContactWriteService = Writes,
                  current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Delete forever: permanently remove contacts that are in Recently deleted (same visibility
    as the list and restore). Uses the erase path; each contact leaves an audit row."""
    return service.purge(current_user, payload.prospect_ids)


# =============================
# DUPLICATES & MERGE
# =============================

@router.get("/duplicates", response_model=None)
def find_duplicates(
    limit: int = Query(50, ge=1, le=200),
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    """
    Groups of likely duplicates (BR-CM-32): the same email apart from case or dots in
    Gmail-style addresses, a similar name at the same company, or the same phone number.
    """
    return service.duplicates(current_user, limit)


@router.post("/merge", response_model=None)
def merge(
    payload: MergeRequest,
    service: ContactWriteService = Writes,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    return service.merge(current_user, payload)


# =============================
# CUSTOM PROPERTIES (BR-CM-09/10)
# =============================

@router.get("/fields", response_model=None)
def list_fields(include_inactive: bool = Query(False, description="Also list deactivated properties"),
                service: ContactFieldService = Fields, current_user: Any = Depends(tenant_user)) -> List[dict]:
    return service.list(current_user, include_inactive)


@router.post("/fields", status_code=201, response_model=None)
def create_field(payload: FieldWrite, service: ContactFieldService = Fields,
                 current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.create(current_user, payload)


@router.patch("/fields/{field_id}", response_model=None)
def update_field(field_id: str, payload: FieldWrite, service: ContactFieldService = Fields,
                 current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.update(current_user, field_id, payload)


@router.delete("/fields/{field_id}", response_model=None)
def delete_field(field_id: str, service: ContactFieldService = Fields,
                 current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Deactivates the property (C-17): hidden from forms and import mapping; values stay on contacts and are
    still exported. Reactivate with PATCH ``is_active: true``. System properties cannot be deactivated."""
    return service.delete(current_user, field_id)


# =============================
# ACTIVITIES (by id)
# =============================

@router.patch("/activities/{activity_id}", response_model=None)
def update_activity(activity_id: str, payload: ActivityUpdate, service: ContactActivityService = Activities,
                    current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.update(current_user, activity_id, payload)


@router.delete("/activities/{activity_id}", response_model=None)
def delete_activity(activity_id: str, service: ContactActivityService = Activities,
                    current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.delete(current_user, activity_id)


# =============================
# SINGLE CONTACT
# =============================

@router.get("/{prospect_id}", response_model=None)
def get_contact(prospect_id: str, service: ContactQueryService = Reads,
                current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.detail(current_user, prospect_id)


@router.patch("/{prospect_id}", response_model=None)
def update_contact(
    prospect_id: str,
    payload: ContactUpdate,
    service: ContactWriteService = Writes,
    current_user: Any = Depends(tenant_user),
    if_match: Optional[int] = Depends(if_match_version),
) -> Dict[str, Any]:
    """Edit a contact; send the row_version you loaded as expected_version or If-Match (T-3)."""
    return service.update(current_user, prospect_id, payload, if_match)


@router.delete("/{prospect_id}", response_model=None)
def delete_contact(prospect_id: str, service: ContactWriteService = Writes,
                   current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Soft delete: hidden everywhere and restorable for 90 days (BR-CM-35)."""
    return service.delete(current_user, prospect_id)


# =============================
# ASSOCIATED COMPANIES (CM-014)
# =============================

@router.get("/{prospect_id}/companies", response_model=None)
def list_contact_companies(prospect_id: str, service: ContactQueryService = Reads,
                           current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Every company the contact is associated with, with its label; the primary one first."""
    return service.companies(current_user, prospect_id)


@router.post("/{prospect_id}/companies", status_code=201, response_model=None)
def add_contact_company(prospect_id: str, payload: CompanyLinkCreate, service: ContactWriteService = Writes,
                        current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.add_company(current_user, prospect_id, payload)


@router.patch("/{prospect_id}/companies/{account_id}", response_model=None)
def update_contact_company(prospect_id: str, account_id: str, payload: CompanyLinkUpdate,
                           service: ContactWriteService = Writes,
                           current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.update_company(current_user, prospect_id, account_id, payload)


@router.delete("/{prospect_id}/companies/{account_id}", response_model=None)
def remove_contact_company(prospect_id: str, account_id: str,
                           new_primary_id: Optional[str] = Query(None, max_length=36,
                                                                 description="Required when removing the primary company"),
                           service: ContactWriteService = Writes,
                           current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Remove an association. The primary company can only go once another one is chosen as primary."""
    return service.remove_company(current_user, prospect_id, account_id, new_primary_id)


@router.get("/{prospect_id}/history", response_model=None)
def property_history(
    prospect_id: str,
    field: Optional[str] = None,
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    """Every property change with old and new value, user, time and source (BR-CM-11)."""
    return service.property_history(current_user, prospect_id, field)


@router.get("/{prospect_id}/timeline", response_model=None)
def contact_timeline(
    prospect_id: str,
    types: Optional[str] = Query(None, description="Comma-separated: NOTE,CALL,MEETING,EMAIL,TASK,PROPERTY,CAMPAIGN,LIST"),
    user_id: Optional[str] = Query(None, description="Only items by this user"),
    limit: int = Query(200, ge=1, le=500),
    service: ContactQueryService = Reads,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    """Everything that happened with this contact, newest first (BR-CM-14)."""
    return service.timeline(current_user, prospect_id, types, user_id, limit, task_serializer=task_dict)


@router.post("/{prospect_id}/activities", status_code=201, response_model=None)
def log_activity(
    prospect_id: str,
    payload: ActivityWrite,
    service: ContactActivityService = Activities,
    current_user: Any = Depends(tenant_user),
) -> Dict[str, Any]:
    """Add a note, or log a call, email or meeting (BR-CM-13)."""
    return service.log(current_user, prospect_id, payload)
