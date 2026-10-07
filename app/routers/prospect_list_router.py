# app/routers/prospect_list_router.py
"""
Prospect List Management API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from app.core.database import get_db
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.campaign import CampaignProspect, Campaign
from app.models.user import User


# Kartik has changed this: Removed prefix to support explicit REST boundaries
router = APIRouter(tags=["Prospect Lists"])

# Secondary router for /prospects prefix (upload & stats endpoints)
prospect_upload_router = APIRouter(tags=["Prospects"])


from app.core.auth import require_role, require_permission
from app.utils.error_utils import handle_route_error
import logging

logger = logging.getLogger(__name__)

# =============================
# SCHEMAS
# =============================

class ProspectListResponse(BaseModel):
    list_id: str
    list_name: str
    source_type: str
    prospect_count: int
    uploaded_at: datetime

    class Config:
        from_attributes = True


class ProspectListPaginatedResponse(BaseModel):
    items: List[ProspectListResponse]
    total: int
    page: int
    page_size: int


class EnrollmentRequest(BaseModel):
    campaign_id: str
    list_ids: List[str]


class EnrollmentResult(BaseModel):
    total_checked: int
    enrolled: int
    rejected: dict


# =============================
# ENDPOINTS
# =============================

# Kartik has changed this: Explicit collection path
@router.get("/prospect-lists", response_model=ProspectListPaginatedResponse)
async def list_prospect_lists(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("uploaded_at", pattern="^(list_name|prospect_count|uploaded_at)$"),
    sort_order: Optional[str] = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get all prospect lists with member counts, pagination, sorting, and search.
    """
    try:
        query = db.query(ProspectList).filter(ProspectList.tenant_id == current_user.tenant_id)
        if search:
            query = query.filter(ProspectList.list_name.ilike(f"%{search}%"))
        total = query.count()
        if sort_by == "list_name":
            order_col = ProspectList.list_name
        else:
            order_col = ProspectList.uploaded_at
        query = query.order_by(order_col.asc() if sort_order == "asc" else order_col.desc())
        offset = (page - 1) * page_size
        lists = query.offset(offset).limit(page_size).all()
        items = []
        for lst in lists:
            count = db.query(func.count(ProspectListMember.id)).filter(
                ProspectListMember.list_id == lst.list_id
            ).scalar() or 0
            items.append(ProspectListResponse(
                list_id=lst.list_id,
                list_name=lst.list_name,
                source_type=lst.source_type,
                prospect_count=count,
                uploaded_at=lst.uploaded_at,
            ))
        return ProspectListPaginatedResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )
    except Exception as e:
        handle_route_error(e, context="list_prospect_lists")


class ProspectInListResponse(BaseModel):
    prospect_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str
    company_name: Optional[str] = None
    designation: Optional[str] = None
    poc_city: Optional[str] = None
    poc_state: Optional[str] = None
    poc_country: Optional[str] = None
    consent_status: str
    notes: Optional[str] = None
    
    class Config:
        from_attributes = True


class ProspectListDetailResponse(BaseModel):
    list_id: str
    list_name: str
    prospects: List[ProspectInListResponse]
    total: int
    page: int
    page_size: int


# Kartik has changed this: Explicit collection path
@router.get("/prospect-lists/{list_id}/prospects", response_model=ProspectListDetailResponse)
async def get_list_prospects(
    list_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get prospects in a specific list with pagination and search.
    """
    try:
        prospect_list = db.query(ProspectList).filter(
            ProspectList.list_id == list_id,
            ProspectList.tenant_id == current_user.tenant_id,
        ).first()
        if not prospect_list:
            raise HTTPException(status_code=404, detail="List not found")
        query = db.query(Prospect, ProspectListMember.notes).join(
            ProspectListMember, Prospect.prospect_id == ProspectListMember.prospect_id
        ).filter(ProspectListMember.list_id == list_id).order_by(
            ProspectListMember.added_at.asc(),
            ProspectListMember.id.asc(),
        )
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                (Prospect.first_name.ilike(search_term)) |
                (Prospect.last_name.ilike(search_term)) |
                (Prospect.email.ilike(search_term)) |
                (Prospect.company_name.ilike(search_term))
            )
        total = query.count()
        offset = (page - 1) * page_size
        rows = query.offset(offset).limit(page_size).all()
        return ProspectListDetailResponse(
            list_id=list_id,
            list_name=prospect_list.list_name,
            prospects=[ProspectInListResponse(
                prospect_id=p.prospect_id,
                first_name=p.first_name,
                last_name=p.last_name,
                email=p.email,
                company_name=p.company_name,
                designation=p.designation,
                poc_city=p.poc_city,
                poc_state=p.poc_state,
                poc_country=p.poc_country,
                consent_status=p.consent_status,
                notes=membership_notes,
            ) for p, membership_notes in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    except Exception as e:
        handle_route_error(e, context="get_list_prospects")


# Kartik has changed this: Use RESTful campaigns sub-resource for enrollment
@router.post("/campaigns/{campaign_id}/enrollments/bulk", response_model=EnrollmentResult, dependencies=[Depends(require_permission("manage_prospects"))])
async def enroll_prospects(
    campaign_id: str,
    request: EnrollmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Enroll prospects from selected lists into a campaign.
    
    Performs safety checks:
    - Global unsubscribe check
    - Consent status check
    - Already enrolled check
    - Email validity check
    """
    rejected = {
        "global_unsubscribe": 0,
        "no_consent": 0,
        "already_enrolled": 0,
        "invalid_email": 0,
    }
    enrolled = 0
    total_checked = 0

    campaign = db.query(Campaign).filter(
        Campaign.campaign_id == campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    valid_list_ids = {
        row[0]
        for row in db.query(ProspectList.list_id).filter(
            ProspectList.tenant_id == current_user.tenant_id,
            ProspectList.list_id.in_(request.list_ids),
        ).all()
    }
    invalid_lists = [lid for lid in request.list_ids if lid not in valid_list_ids]
    if invalid_lists:
        raise HTTPException(
            status_code=404,
            detail=f"Prospect list(s) not found in your workspace: {', '.join(invalid_lists)}",
        )

    # Get all global unsubscribes for this tenant
    tenant_id = current_user.tenant_id

    unsubscribed_emails = set(
        email for (email,) in db.query(GlobalUnsubscribe.email).filter(
            GlobalUnsubscribe.tenant_id == tenant_id
        ).all()
    )

    # Get already enrolled prospect IDs for this campaign
    already_enrolled_ids = set(
        pid for (pid,) in db.query(CampaignProspect.prospect_id).filter(
            CampaignProspect.campaign_id == campaign_id
        ).all()
    )

    # Process each list
    for list_id in valid_list_ids:
        # Get prospects from this list
        members = db.query(ProspectListMember).filter(
            ProspectListMember.list_id == list_id
        ).all()

        for member in members:
            prospect = db.query(Prospect).filter(
                Prospect.prospect_id == member.prospect_id,
                Prospect.tenant_id == current_user.tenant_id,
            ).first()

            if not prospect:
                continue

            total_checked += 1

            # Check 1: Global unsubscribe
            if prospect.email.lower() in unsubscribed_emails:
                rejected["global_unsubscribe"] += 1
                continue

            # Check 2: Already enrolled in this campaign
            if prospect.prospect_id in already_enrolled_ids:
                rejected["already_enrolled"] += 1
                continue

            # Check 3: Consent status
            if prospect.consent_status == "UNSUBSCRIBED":
                rejected["no_consent"] += 1
                continue

            # Check 4: Email validity
            if not prospect.is_valid_email:
                rejected["invalid_email"] += 1
                continue

            # All checks passed - enroll prospect
            import uuid
            enrollment = CampaignProspect(
                id=str(uuid.uuid4()),
                campaign_id=campaign_id,
                prospect_id=prospect.prospect_id,
                enrollment_status="PENDING",
            )
            db.add(enrollment)
            already_enrolled_ids.add(prospect.prospect_id)  # Prevent duplicates within batch
            enrolled += 1

    db.commit()

    return EnrollmentResult(
        total_checked=total_checked,
        enrolled=enrolled,
        rejected=rejected,
    )


# Kartik has changed this: Explicit REST path
@router.delete("/prospect-lists/{list_id}", dependencies=[Depends(require_permission("manage_prospects"))])
async def delete_prospect_list(
    list_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Delete a prospect list and its member associations.
    Note: Does not delete the prospects themselves, only the list and memberships.
    """
    try:
        prospect_list = db.query(ProspectList).filter(
            ProspectList.list_id == list_id,
            ProspectList.tenant_id == current_user.tenant_id,
        ).first()
        if not prospect_list:
            raise HTTPException(status_code=404, detail="List not found")
        orphaned_ids = []
        prospects_in_list = [r[0] for r in db.query(ProspectListMember.prospect_id).filter(
            ProspectListMember.list_id == list_id
        ).all()]
        if prospects_in_list:
            prospects_in_other_lists = {r[0] for r in db.query(ProspectListMember.prospect_id).filter(
                ProspectListMember.prospect_id.in_(prospects_in_list),
                ProspectListMember.list_id != list_id
            ).all()}
            orphaned_ids = [pid for pid in prospects_in_list if pid not in prospects_in_other_lists]
            db.query(ProspectListMember).filter(
                ProspectListMember.list_id == list_id
            ).delete(synchronize_session=False)
            if orphaned_ids:
                from app.models.campaign import CampaignProspect
                from app.models.prospect import Prospect
                from app.models.prospect_persona import ProspectPersona
                from app.models.email_message import EmailMessage, EmailEvent
                from app.models.conversation import Conversation
                message_ids = [m[0] for m in db.query(EmailMessage.message_id).filter(
                    EmailMessage.prospect_id.in_(orphaned_ids)
                ).all()]
                if message_ids:
                    db.query(EmailEvent).filter(
                        EmailEvent.message_id.in_(message_ids)
                    ).delete(synchronize_session=False)
                db.query(EmailMessage).filter(
                    EmailMessage.prospect_id.in_(orphaned_ids)
                ).delete(synchronize_session=False)
                db.query(Conversation).filter(
                    Conversation.prospect_id.in_(orphaned_ids)
                ).delete(synchronize_session=False)
                db.query(ProspectPersona).filter(
                    ProspectPersona.prospect_id.in_(orphaned_ids)
                ).delete(synchronize_session=False)
                db.query(CampaignProspect).filter(
                    CampaignProspect.prospect_id.in_(orphaned_ids)
                ).delete(synchronize_session=False)
                db.query(Prospect).filter(
                    Prospect.prospect_id.in_(orphaned_ids)
                ).delete(synchronize_session=False)
        else:
            db.query(ProspectListMember).filter(
                ProspectListMember.list_id == list_id
            ).delete(synchronize_session=False)
        db.delete(prospect_list)
        db.commit()
        return {"status": "deleted", "list_id": list_id, "deleted_prospects": len(orphaned_ids)}
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="delete_prospect_list")


# =============================
# PROSPECT UPDATE ENDPOINT
# =============================

class ProspectNoteUpdate(BaseModel):
    notes: Optional[str] = None


class ProspectUpdateRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    company_name: Optional[str] = None
    designation: Optional[str] = None
    poc_city: Optional[str] = None
    poc_state: Optional[str] = None
    poc_country: Optional[str] = None


# Kartik has changed this: Explicit REST path
@router.patch("/prospect-lists/{list_id}/prospects/{prospect_id}/notes", dependencies=[Depends(require_permission("manage_prospects"))])
async def update_prospect_note(
    list_id: str,
    prospect_id: str,
    payload: ProspectNoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Update the note for a prospect within a specific list.
    """
    try:
        scoped_list = db.query(ProspectList).filter(
            ProspectList.list_id == list_id,
            ProspectList.tenant_id == current_user.tenant_id,
        ).first()
        if not scoped_list:
            raise HTTPException(status_code=404, detail="List not found")

        membership = db.query(ProspectListMember).filter(
            ProspectListMember.list_id == list_id,
            ProspectListMember.prospect_id == prospect_id,
        ).first()
        if not membership:
            raise HTTPException(status_code=404, detail="Prospect not found in this list")
        membership.notes = payload.notes
        db.commit()
        return {"status": "updated", "prospect_id": prospect_id, "list_id": list_id}
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="update_prospect_note")


# Kartik has changed this: Proper explicit top-level REST path
@router.patch("/prospects/{prospect_id}", dependencies=[Depends(require_permission("manage_prospects"))])
async def update_prospect(
    prospect_id: str,
    updates: ProspectUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Update individual prospect fields.
    """
    try:
        prospect = db.query(Prospect).filter(
            Prospect.prospect_id == prospect_id,
            Prospect.tenant_id == current_user.tenant_id,
        ).first()
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect not found")
        update_data = updates.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(prospect, field, value)
        if "email" in update_data and update_data["email"]:
            new_email = update_data["email"]
            # Check for duplicate email
            existing_email = db.query(Prospect).filter(
                Prospect.tenant_id == prospect.tenant_id,
                Prospect.email == new_email,
                Prospect.prospect_id != prospect_id
            ).first()
            if existing_email:
                raise HTTPException(status_code=400, detail="A prospect with this email already exists.")
                
            from app.utils.email_utils import parse_email
            email_info = parse_email(new_email)
            if email_info:
                prospect.email_type = email_info.get("email_type")
                prospect.email_provider = email_info.get("email_provider")
            else:
                prospect.email_type = "PERSONAL"
                prospect.email_provider = new_email.split("@")[-1] if "@" in new_email else None
        if "poc_state" in update_data and update_data["poc_state"]:
            from app.utils.business_calendar import get_timezone_for_state
            prospect.timezone = get_timezone_for_state(update_data["poc_state"])
        db.commit()
        return {"status": "updated", "prospect_id": prospect_id}
    except Exception as e:
        db.rollback()
        handle_route_error(e, context="update_prospect")


# Kartik has changed this: Proper explicit top-level REST path
@router.delete("/prospects/{prospect_id}", dependencies=[Depends(require_permission("manage_prospects"))])
async def delete_prospect(
    prospect_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """
    Delete an individual prospect and all related records.
    """
    from app.models.campaign import CampaignProspect
    from app.models.prospect_persona import ProspectPersona
    from app.models.email_message import EmailMessage, EmailEvent
    from app.models.conversation import Conversation
    
    # Check prospect exists
    prospect = db.query(Prospect).filter(
        Prospect.prospect_id == prospect_id,
        Prospect.tenant_id == current_user.tenant_id,
    ).first()
    
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")
    
    # Get all message IDs for this prospect first (needed for EmailEvent deletion)
    message_ids = [m[0] for m in db.query(EmailMessage.message_id).filter(
        EmailMessage.prospect_id == prospect_id
    ).all()]
    
    # Delete in order to respect FK constraints:
    # 0. Email events (references email_messages)
    if message_ids:
        db.query(EmailEvent).filter(
            EmailEvent.message_id.in_(message_ids)
        ).delete(synchronize_session=False)
    
    # 1. Email messages
    db.query(EmailMessage).filter(
        EmailMessage.prospect_id == prospect_id
    ).delete(synchronize_session=False)
    
    # 2. Conversations
    db.query(Conversation).filter(
        Conversation.prospect_id == prospect_id
    ).delete(synchronize_session=False)
    
    # 3. Prospect personas
    db.query(ProspectPersona).filter(
        ProspectPersona.prospect_id == prospect_id
    ).delete(synchronize_session=False)
    
    # 4. Campaign enrollments
    db.query(CampaignProspect).filter(
        CampaignProspect.prospect_id == prospect_id
    ).delete(synchronize_session=False)
    
    # 5. List memberships
    db.query(ProspectListMember).filter(
        ProspectListMember.prospect_id == prospect_id
    ).delete(synchronize_session=False)
    
    # 6. Delete the prospect
    db.delete(prospect)
    db.commit()
    
    return {"status": "deleted", "prospect_id": prospect_id}


# =============================
# REVALIDATE LIST ENDPOINT
# =============================

from app.utils.email_utils import parse_email, PERSONAL_EMAIL_DOMAINS

class RevalidationResultItem(BaseModel):
    prospect_id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    status: str  # "valid", "invalid", "warning"
    issues: List[str] = []

class RevalidationResponse(BaseModel):
    list_id: str
    list_name: str
    total_checked: int
    valid_count: int
    invalid_count: int
    warning_count: int
    updated_count: int = 0  # Number of prospects with metadata updated
    results: List[RevalidationResultItem]


# Kartik has changed this: Replaced verb with noun-based lifecycle resource
@router.post("/prospect-lists/{list_id}/revalidations", dependencies=[Depends(require_permission("manage_prospects"))])
async def revalidate_list(
    list_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Re-validate all prospects in a list.
    Checks:
    - Email format validity
    - Personal email domains (gmail, yahoo, etc.)
    - Globally unsubscribed status
    - Missing required fields (POC State)
    """
    # Get the list
    prospect_list = db.query(ProspectList).filter(
        ProspectList.list_id == list_id,
        ProspectList.tenant_id == current_user.tenant_id,
    ).first()
    
    if not prospect_list:
        raise HTTPException(status_code=404, detail="List not found")
    
    # Get all prospects in this list
    prospects = db.query(Prospect).join(
        ProspectListMember,
        Prospect.prospect_id == ProspectListMember.prospect_id
    ).filter(
        ProspectListMember.list_id == list_id,
        Prospect.tenant_id == current_user.tenant_id,
    ).all()
    
    # Get globally unsubscribed emails, with the reason each was suppressed
    unsubscribed = {
        email.lower(): reason
        for email, reason in db.query(GlobalUnsubscribe.email, GlobalUnsubscribe.reason)
        .filter(GlobalUnsubscribe.tenant_id == current_user.tenant_id)
        .all()
    }
    
    # Import timezone helper
    from app.utils.business_calendar import get_timezone_for_state
    
    results = []
    valid_count = 0
    invalid_count = 0
    warning_count = 0
    updated_count = 0
    
    # Track seen emails for duplicate detection
    seen_emails = set()
    duplicate_emails = set()
    
    # First pass: identify duplicate emails
    for prospect in prospects:
        if prospect.email:
            email_lower = prospect.email.lower()
            if email_lower in seen_emails:
                duplicate_emails.add(email_lower)
            seen_emails.add(email_lower)
    
    for prospect in prospects:
        issues = []
        status = "valid"
        was_updated = False
        
        # Check email validity
        email_info = parse_email(prospect.email)
        if not email_info:
            # parse_email returns None for invalid OR personal emails
            # Check if it's specifically a personal email domain
            if prospect.email and "@" in prospect.email:
                domain = prospect.email.split("@")[-1].lower()
                if domain in PERSONAL_EMAIL_DOMAINS:
                    issues.append("Personal email domain detected")
                    status = "warning" if status != "invalid" else status
                    # Update email_type to PERSONAL if not already
                    if prospect.email_type != "PERSONAL":
                        prospect.email_type = "PERSONAL"
                        prospect.email_provider = domain
                        was_updated = True
                else:
                    issues.append("Invalid email format")
                    status = "invalid"
            else:
                issues.append("Invalid email format")
                status = "invalid"
        else:
            # Valid business email - ensure metadata is correct
            if prospect.email_type != email_info.get("email_type") or prospect.email_provider != email_info.get("email_provider"):
                prospect.email_type = email_info.get("email_type")
                prospect.email_provider = email_info.get("email_provider")
                was_updated = True
        
        # Check globally unsubscribed
        if prospect.email and prospect.email.lower() in unsubscribed:
            reason = unsubscribed.get(prospect.email.lower())
            issues.append(f"Globally unsubscribed ({reason})" if reason else "Globally unsubscribed")
            status = "invalid"
        
        # Check for duplicate emails in list
        if prospect.email and prospect.email.lower() in duplicate_emails:
            issues.append("Duplicate email in list")
            status = "warning" if status != "invalid" else status
        
        # Check missing POC State and derive timezone
        if not prospect.poc_state or not str(prospect.poc_state).strip():
            issues.append("Missing POC State")
            status = "warning" if status != "invalid" else status
        else:
            # Derive timezone from POC State if missing or outdated
            expected_timezone = get_timezone_for_state(prospect.poc_state)
            if prospect.timezone != expected_timezone:
                prospect.timezone = expected_timezone
                was_updated = True
        
        if was_updated:
            updated_count += 1
        
        # Count by status
        if status == "valid":
            valid_count += 1
        elif status == "invalid":
            invalid_count += 1
        else:
            warning_count += 1
        
        results.append(RevalidationResultItem(
            prospect_id=prospect.prospect_id,
            email=prospect.email or "",
            first_name=prospect.first_name,
            last_name=prospect.last_name,
            company_name=prospect.company_name,
            status=status,
            issues=issues,
        ))
    
    # Commit any updates made during revalidation
    if updated_count > 0:
        db.commit()
    
    return RevalidationResponse(
        list_id=list_id,
        list_name=prospect_list.list_name,
        total_checked=len(prospects),
        valid_count=valid_count,
        invalid_count=invalid_count,
        warning_count=warning_count,
        updated_count=updated_count,
        results=results,
    )


# =============================
# PROSPECT UPLOAD & STATS (prefix: /prospects)
# =============================

from app.services.prospect_upload_service import (
    dry_run_validate,
    confirm_upload,
    revalidate_prospect_records,
)
from app.utils.excel_reader import read_upload_file


class ProspectStatsResponse(BaseModel):
    total: int
    available: int
    active: int
    opted_out: int

# Kartik has changed this: Added strict Pydantic models for request payloads
class UploadRevalidateRequest(BaseModel):
    records: list[dict]

class UploadConfirmRequest(BaseModel):
    upload_id: str
    # upload_id: Optional[str] = None
    # validation_id: Optional[str] = None
    title: str
    records: list[dict]


# Kartik has changed this: Replaced verb with noun-based action
@prospect_upload_router.post("/uploads/validations", dependencies=[Depends(require_permission("manage_prospects"))])
def upload_dry_run(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
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
            # "validation_id": upload_id,
            **summary,
        }

        if "accepted" in summary:
            response["valid"] = summary["accepted"]

        return response
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


# Kartik has changed this: Modeled upload as a root resource rather than a verb
@prospect_upload_router.post("/uploads/{upload_id}/revalidations", dependencies=[Depends(require_permission("manage_prospects"))])
def upload_revalidate(
    upload_id: str,
    payload: UploadRevalidateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Step 1.5: Re-validate edited records.
    """
    try:
        return revalidate_prospect_records(
            records=payload.records,
            db=db,
            tenant_id=current_user.tenant_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


# Kartik has changed this: Converted action endpoint into RESTful bulk creation
@prospect_upload_router.post("/uploads/{upload_id}/confirmations", dependencies=[Depends(require_permission("manage_prospects"))])
def upload_confirm(
    upload_id: str,
    payload: UploadConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """
    Step 2: Confirm upload and persist prospects.
    """
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Upload title is required")

    # effective_upload_id = payload.upload_id or payload.validation_id or upload_id

    try:
        return confirm_upload(
            upload_id=payload.upload_id,
            # upload_id=effective_upload_id,
            title=payload.title,
            records=payload.records,
            db=db,
            tenant_id=current_user.tenant_id,
            uploaded_by=current_user.user_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


# Kartik has changed this: Use proper top-level collection endpoint
@prospect_upload_router.get("/prospects/stats")
def get_prospect_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
) -> ProspectStatsResponse:
    """
    Get prospect statistics for KPI cards.
    """
    tenant_id = current_user.tenant_id

    total = db.query(func.count(Prospect.prospect_id)).filter(
        Prospect.tenant_id == tenant_id
    ).scalar() or 0

    opted_out = db.query(func.count(Prospect.prospect_id)).filter(
        Prospect.tenant_id == tenant_id,
        Prospect.consent_status == "UNSUBSCRIBED"
    ).scalar() or 0

    tenant_prospect_ids = [
        r[0] for r in db.query(Prospect.prospect_id).filter(
            Prospect.tenant_id == tenant_id
        ).all()
    ]

    if tenant_prospect_ids:
        available = db.query(func.count(distinct(CampaignProspect.prospect_id))).filter(
            CampaignProspect.prospect_id.in_(tenant_prospect_ids)
        ).scalar() or 0

        enrolled_ids = {
            r[0] for r in db.query(CampaignProspect.prospect_id).filter(
                CampaignProspect.prospect_id.in_(tenant_prospect_ids)
            ).distinct().all()
        }
        active = len([pid for pid in tenant_prospect_ids if pid not in enrolled_ids])
    else:
        available = 0
        active = 0

    return ProspectStatsResponse(
        total=total,
        available=available,
        active=active,
        opted_out=opted_out,
    )
