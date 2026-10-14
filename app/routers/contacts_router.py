# app/routers/contacts_router.py
"""
Contact management API (BRD v2.0, section 5.2): every contact in the workspace in one
place, with views and AND/OR filters, a record page with a unified timeline, lifecycle
stage and lead status, custom properties, property history, bulk actions, dedupe and
merge, export, and soft delete with restore.

Users with the manage_prospects permission (Admin) see every contact. Everyone else
(User) sees and edits only the contacts they own, and can't reassign, bulk edit, merge,
export or delete.

This module only parses requests and shapes responses; the logic lives in
app/services/contact_query_service.py (reads), contact_write_service.py (writes),
contact_field_service.py (custom properties) and contact_activity_service.py (activities).
Handlers carry return type hints with ``response_model=None`` so the hints document the code
without turning into response models (the OpenAPI schema and serialisation stay as they were).
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.user import User
from app.routers.tasks_router import task_dict
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
from app.services import contact_activity_service as activities
from app.services import contact_field_service as fields
from app.services import contact_query_service as contacts
from app.services import contact_write_service as writes
from app.services.crm import RESTORE_WINDOW_DAYS

router = APIRouter(prefix="/contacts", tags=["Contacts"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


# =============================
# COLLECTION
# =============================

@router.get("/meta", response_model=None)
def contact_meta(current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Picklist values and table columns for the UI."""
    return contacts.contact_meta(current_user)


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
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    f = contacts.ContactFilters(
        q=q, owner=owner, account_id=account_id, tag=tag, list_id=list_id, campaign_id=campaign_id,
        consent_status=consent_status, email_valid=email_valid, has_phone=has_phone, country=country,
        industry=industry, lifecycle_stage=lifecycle_stage, lead_status=lead_status, filters=filters,
        grade=grade, score_min=score_min, score_max=score_max)
    return contacts.list_page(db, current_user, f, sort_by, sort_order, page, page_size)


@router.get("/board", response_model=None)
def contacts_board(
    group_by: str = Query("lifecycle_stage", pattern="^(lifecycle_stage|lead_status)$"),
    per_column: int = Query(50, ge=1, le=200),
    q: Optional[str] = None,
    owner: Optional[str] = None,
    filters: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    """Board view: contacts grouped by lifecycle stage or lead status (BR-CM-20)."""
    return contacts.board(db, current_user, group_by, per_column,
                          contacts.ContactFilters(q=q, owner=owner, filters=filters))


@router.post("", status_code=201, response_model=None)
def create_contact(
    payload: ContactCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    prospect = writes.create_contact(db, current_user, payload)
    return contacts.contact_detail(db, current_user, prospect)


@router.get("/facets", response_model=None)
def contact_facets(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Values for the filter dropdowns: tags, countries, industries, lists and campaigns."""
    return contacts.facets(db, current_user)


@router.get("/owners", response_model=None)
def contact_owners(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)) -> List[dict]:
    """Active users in the workspace, for owner pickers."""
    return contacts.owners(db, current_user)


@router.get("/mentionable-users", response_model=None)
def mentionable_users(q: Optional[str] = Query(None, max_length=100), limit: int = Query(20, ge=1, le=50),
                      prospect_id: Optional[str] = Query(None, max_length=36),
                      opportunity_id: Optional[str] = Query(None, max_length=36),
                      db: Session = Depends(get_db), current_user: User = Depends(tenant_user)) -> List[dict]:
    """
    Colleagues who can be @mentioned in a note: any active user of the workspace (BR-CM-16).
    With prospect_id (contact notes) or opportunity_id (deal notes), only those who can see that
    record, matching the check made when the note is saved.
    """
    return contacts.mentionable_users(db, current_user, q, limit, prospect_id, opportunity_id)


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
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> StreamingResponse:
    """Export a view to CSV / XLSX. Admins and managers only (BR-CM-31)."""
    f = contacts.ContactFilters(q=q, owner=owner, list_id=list_id, lifecycle_stage=lifecycle_stage,
                                lead_status=lead_status, tag=tag, filters=filters)
    export = contacts.export_contacts(db, current_user, format, columns, f)
    return StreamingResponse(iter([export.content]), media_type=export.media_type,
                             headers={"Content-Disposition": f'attachment; filename="{export.filename}"'})


@router.post("/bulk", response_model=None)
def bulk_update(
    payload: BulkAction,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    """Bulk edit, assign, tag, add to list, enroll or delete (BR-CM-36/37)."""
    return writes.bulk_update(db, current_user, payload)


# =============================
# RECENTLY DELETED (BR-CM-35)
# =============================

@router.get("/deleted", response_model=None)
def deleted_contacts(
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    writes.require_manager(current_user)
    return contacts.deleted_page(db, current_user, q, page, page_size)


@router.post("/restore", response_model=None)
def restore(payload: RestoreRequest, db: Session = Depends(get_db),
            current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    count = writes.restore_contacts(db, current_user, payload.prospect_ids)
    return {"status": "restored", "restored": count}


@router.post("/purge", response_model=None)
def purge_deleted(payload: RestoreRequest, db: Session = Depends(get_db),
                  current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Delete forever: permanently remove contacts that are in Recently deleted (same visibility
    as the list and restore). Uses the erase path; each contact leaves an audit row."""
    purged = writes.purge_contacts(db, current_user, payload.prospect_ids)
    return {"status": "purged", "purged": purged}


# =============================
# DUPLICATES & MERGE
# =============================

@router.get("/duplicates", response_model=None)
def find_duplicates(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    """
    Groups of likely duplicates (BR-CM-32): the same email apart from case or dots in
    Gmail-style addresses, a similar name at the same company, or the same phone number.
    """
    writes.require_manager(current_user)
    return contacts.find_duplicates(db, current_user, limit)


@router.post("/merge", response_model=None)
def merge(
    payload: MergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    primary, merged, moved = writes.merge(db, current_user, payload)
    return {"status": "merged", "merged": merged, "moved": moved,
            "contact": contacts.contact_detail(db, current_user, primary)}


# =============================
# CUSTOM PROPERTIES (BR-CM-09/10)
# =============================

@router.get("/fields", response_model=None)
def list_fields(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)) -> List[dict]:
    return fields.list_fields(db, current_user.tenant_id)


@router.post("/fields", status_code=201, response_model=None)
def create_field(payload: FieldWrite, db: Session = Depends(get_db),
                 current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    writes.require_manager(current_user)
    return fields.create_field(db, current_user.tenant_id, payload)


@router.patch("/fields/{field_id}", response_model=None)
def update_field(field_id: str, payload: FieldWrite, db: Session = Depends(get_db),
                 current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    writes.require_manager(current_user)
    return fields.update_field(db, current_user.tenant_id, field_id, payload)


@router.delete("/fields/{field_id}", response_model=None)
def delete_field(field_id: str, db: Session = Depends(get_db),
                 current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Removes the property definition. Stored values stay on contacts but are no longer shown."""
    writes.require_manager(current_user)
    fields.delete_field(db, current_user.tenant_id, field_id)
    return {"status": "deleted", "field_id": field_id}


# =============================
# ACTIVITIES (by id)
# =============================

@router.patch("/activities/{activity_id}", response_model=None)
def update_activity(activity_id: str, payload: ActivityUpdate, db: Session = Depends(get_db),
                    current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    return activities.update_activity(db, current_user, activity_id, payload)


@router.delete("/activities/{activity_id}", response_model=None)
def delete_activity(activity_id: str, db: Session = Depends(get_db),
                    current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    activities.delete_activity(db, current_user, activity_id)
    return {"status": "deleted", "activity_id": activity_id}


# =============================
# SINGLE CONTACT
# =============================

@router.get("/{prospect_id}", response_model=None)
def get_contact(prospect_id: str, db: Session = Depends(get_db),
                current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    return contacts.contact_detail(db, current_user, contacts.get_contact(db, current_user, prospect_id))


@router.patch("/{prospect_id}", response_model=None)
def update_contact(
    prospect_id: str,
    payload: ContactUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    prospect = writes.update_contact(db, current_user, prospect_id, payload)
    return contacts.contact_detail(db, current_user, prospect)


@router.delete("/{prospect_id}", response_model=None)
def delete_contact(prospect_id: str, db: Session = Depends(get_db),
                   current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Soft delete: hidden everywhere and restorable for 90 days (BR-CM-35)."""
    writes.delete_contact(db, current_user, prospect_id)
    return {"status": "deleted", "prospect_id": prospect_id, "restorable_days": RESTORE_WINDOW_DAYS}


# =============================
# ASSOCIATED COMPANIES (CM-014)
# =============================

@router.get("/{prospect_id}/companies", response_model=None)
def list_contact_companies(prospect_id: str, db: Session = Depends(get_db),
                           current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Every company the contact is associated with, with its label; the primary one first."""
    return contacts.companies_response(db, contacts.get_contact(db, current_user, prospect_id))


@router.post("/{prospect_id}/companies", status_code=201, response_model=None)
def add_contact_company(prospect_id: str, payload: CompanyLinkCreate, db: Session = Depends(get_db),
                        current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    return contacts.companies_response(db, writes.add_company(db, current_user, prospect_id, payload))


@router.patch("/{prospect_id}/companies/{account_id}", response_model=None)
def update_contact_company(prospect_id: str, account_id: str, payload: CompanyLinkUpdate,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    return contacts.companies_response(db, writes.update_company(db, current_user, prospect_id, account_id, payload))


@router.delete("/{prospect_id}/companies/{account_id}", response_model=None)
def remove_contact_company(prospect_id: str, account_id: str,
                           new_primary_id: Optional[str] = Query(None, max_length=36,
                                                                 description="Required when removing the primary company"),
                           db: Session = Depends(get_db),
                           current_user: User = Depends(tenant_user)) -> Dict[str, Any]:
    """Remove an association. The primary company can only go once another one is chosen as primary."""
    prospect = writes.remove_company(db, current_user, prospect_id, account_id, new_primary_id)
    return contacts.companies_response(db, prospect)


@router.get("/{prospect_id}/history", response_model=None)
def property_history(
    prospect_id: str,
    field: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    """Every property change with old and new value, user, time and source (BR-CM-11)."""
    return contacts.property_history(db, current_user, prospect_id, field)


@router.get("/{prospect_id}/timeline", response_model=None)
def contact_timeline(
    prospect_id: str,
    types: Optional[str] = Query(None, description="Comma-separated: NOTE,CALL,MEETING,EMAIL,TASK,PROPERTY,CAMPAIGN,LIST"),
    user_id: Optional[str] = Query(None, description="Only items by this user"),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    """Everything that happened with this contact, newest first (BR-CM-14)."""
    return contacts.timeline(db, current_user, prospect_id, types, user_id, limit, task_serializer=task_dict)


@router.post("/{prospect_id}/activities", status_code=201, response_model=None)
def log_activity(
    prospect_id: str,
    payload: ActivityWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
) -> Dict[str, Any]:
    """Add a note, or log a call, email or meeting (BR-CM-13)."""
    return activities.log_activity(db, current_user, prospect_id, payload)
