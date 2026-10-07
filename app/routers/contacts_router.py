# app/routers/contacts_router.py
"""
Contact management API (BRD v2.0, section 5.2): every contact in the workspace in one
place, with views and AND/OR filters, a record page with a unified timeline, lifecycle
stage and lead status, custom properties, property history, bulk actions, dedupe and
merge, export, and soft delete with restore.

Users with the manage_prospects permission (Admin) see every contact. Everyone else
(User) sees and edits only the contacts they own, and can't reassign, bulk edit, merge,
export or delete.
"""

import csv
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ACTIVITY_TYPES, ContactActivity
from app.models.contact_field import FIELD_TYPES, ContactFieldDefinition
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.user import User
from app.services import crm
from app.services.contact_service import (
    MERGE_CHOOSABLE,
    can_access_contact,
    can_manage_contacts,
    can_see_owner,
    scope,
    visible_user_ids,
    clean_str,
    merge_contacts,
    normalize_tags,
    phone_digits,
)

router = APIRouter(prefix="/contacts", tags=["Contacts"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

SORT_COLUMNS = {
    "name": (Prospect.first_name, Prospect.last_name),
    "email": (Prospect.email,),
    "company": (Prospect.company_name,),
    "lifecycle_stage": (Prospect.lifecycle_stage,),
    "lead_status": (Prospect.lead_status,),
    "created_at": (Prospect.created_at,),
    "updated_at": (Prospect.updated_at,),
}

EVENT_LABELS = {
    EmailEvent.EVENT_OPEN: "EMAIL_OPENED",
    EmailEvent.EVENT_CLICK: "EMAIL_CLICKED",
    EmailEvent.EVENT_BOUNCE: "EMAIL_BOUNCED",
    EmailEvent.EVENT_UNSUBSCRIBE: "UNSUBSCRIBED",
}

# Properties tracked in history and editable through the API
TRACKED_FIELDS = (
    "first_name", "last_name", "email", "phone", "mobile_phone", "designation", "company_name",
    "account_id", "owner_id", "industry", "emp_band", "linkedin_url", "poc_city", "poc_state",
    "poc_country", "timezone", "lifecycle_stage", "lead_status", "lead_source", "legal_basis",
    "consent_status", "tags", "custom_fields",
)
TEXT_FIELDS = ("first_name", "last_name", "phone", "mobile_phone", "designation", "industry", "emp_band",
               "linkedin_url", "poc_city", "poc_country", "lead_source")

# Columns available to tables and exports (key -> label)
COLUMNS = {
    "full_name": "Name", "email": "Email", "phone": "Phone", "mobile_phone": "Mobile",
    "designation": "Job title", "company_name": "Company", "owner_name": "Owner",
    "lifecycle_stage": "Lifecycle stage", "lead_status": "Lead status", "lead_source": "Lead source",
    "tags": "Tags", "industry": "Industry", "poc_city": "City", "poc_state": "State",
    "poc_country": "Country", "timezone": "Time zone", "consent_status": "Email subscription",
    "legal_basis": "Legal basis", "linkedin_url": "LinkedIn", "last_activity_at": "Last activity",
    "last_contacted_at": "Last contacted", "created_at": "Create date", "updated_at": "Last updated",
}


# =============================
# SCHEMAS
# =============================

class ContactWrite(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=50)
    mobile_phone: Optional[str] = Field(None, max_length=50)
    designation: Optional[str] = None
    company_name: Optional[str] = None
    account_id: Optional[str] = None
    owner_id: Optional[str] = None
    industry: Optional[str] = None
    emp_band: Optional[str] = None
    linkedin_url: Optional[str] = None
    poc_city: Optional[str] = None
    poc_state: Optional[str] = None
    poc_country: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    lead_status: Optional[str] = None
    lead_source: Optional[str] = Field(None, max_length=100)
    legal_basis: Optional[str] = None
    consent_status: Optional[str] = None  # OPT_IN / UNSUBSCRIBED (synced with the global unsubscribe list)
    tags: Optional[List[str]] = None
    custom_fields: Optional[Dict[str, Any]] = None


class ContactCreate(ContactWrite):
    email: EmailStr
    list_id: Optional[str] = None


class ContactUpdate(ContactWrite):
    email: Optional[EmailStr] = None


class ActivityWrite(BaseModel):
    activity_type: str = "NOTE"
    subject: Optional[str] = Field(None, max_length=255)
    body: Optional[str] = None
    outcome: Optional[str] = Field(None, max_length=100)
    duration_minutes: Optional[int] = Field(None, ge=0, le=24 * 60)
    occurred_at: Optional[datetime] = None


class ActivityUpdate(BaseModel):
    subject: Optional[str] = Field(None, max_length=255)
    body: Optional[str] = None
    outcome: Optional[str] = Field(None, max_length=100)
    duration_minutes: Optional[int] = Field(None, ge=0, le=24 * 60)
    occurred_at: Optional[datetime] = None


class BulkAction(BaseModel):
    prospect_ids: List[str] = Field(..., min_length=1, max_length=5000)
    # assign_owner / add_tags / remove_tags / set_account / set_property /
    # add_to_list / remove_from_list / enroll / delete
    action: str
    owner_id: Optional[str] = None
    tags: Optional[List[str]] = None
    account_id: Optional[str] = None
    field: Optional[str] = None
    value: Any = None
    list_id: Optional[str] = None
    new_list_name: Optional[str] = Field(None, max_length=255)
    campaign_id: Optional[str] = None


class MergeRequest(BaseModel):
    primary_id: str
    duplicate_ids: List[str] = Field(..., min_length=1, max_length=20)
    choices: Dict[str, str] = {}  # property -> prospect_id whose value to keep


class RestoreRequest(BaseModel):
    prospect_ids: List[str] = Field(..., min_length=1, max_length=5000)


class FieldWrite(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    field_type: str = "TEXT"
    options: Optional[List[str]] = None
    sort_order: Optional[int] = None
    group_name: Optional[str] = Field(None, max_length=100)
    required: bool = False


# =============================
# HELPERS
# =============================

def _utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    """Timestamps are stored as naive UTC (like func.now() on the server)."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _require_manager(user: User):
    if not can_manage_contacts(user):
        raise HTTPException(status_code=403, detail="Your account does not have the 'manage_prospects' permission.")


def _visible(db: Session, user: User):
    """Contacts this user may see (not deleted; own contacts only without manage_prospects)."""
    query = db.query(Prospect).filter(Prospect.tenant_id == user.tenant_id, Prospect.deleted_at.is_(None))
    # Own records plus those of everyone below in the sales hierarchy (BR-SH-02)
    return scope(query, db, user, Prospect.owner_id)


def _get_contact(db: Session, user: User, prospect_id: str) -> Prospect:
    prospect = db.query(Prospect).filter(
        Prospect.prospect_id == prospect_id,
        Prospect.tenant_id == user.tenant_id,
        Prospect.deleted_at.is_(None),
    ).first()
    if not prospect or not can_access_contact(user, prospect):
        raise HTTPException(status_code=404, detail="Contact not found")
    return prospect


def _check_owner(db: Session, user: User, owner_id: Optional[str]):
    if owner_id and not db.query(User.user_id).filter(
        User.user_id == owner_id, User.tenant_id == user.tenant_id
    ).first():
        raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")
    if owner_id and not can_see_owner(db, user, owner_id):
        raise HTTPException(status_code=403, detail="You can only assign records to yourself or your team")


def _get_account(db: Session, user: User, account_id: str) -> Account:
    account = db.query(Account).filter(
        Account.account_id == account_id, Account.tenant_id == user.tenant_id, Account.deleted_at.is_(None)
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="Company not found")
    return account


def _user_name(u: Optional[User]) -> Optional[str]:
    return f"{u.first_name} {u.last_name}".strip() if u else None


def _summaries(db: Session, prospects: List[Prospect]) -> List[dict]:
    """Serialise contacts for tables, batch-loading owners, companies and activity dates."""
    ids = [p.prospect_id for p in prospects]
    owner_ids = {p.owner_id for p in prospects if p.owner_id}
    account_ids = {p.account_id for p in prospects if p.account_id}
    owners = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(owner_ids))} if owner_ids else {}
    accounts = {a.account_id: (a.name, a.domain) for a in db.query(Account.account_id, Account.name, Account.domain).filter(
        Account.account_id.in_(account_ids), Account.deleted_at.is_(None))} if account_ids else {}

    last_activity, last_contacted = {}, {}

    def _bump(store, pid, ts):
        if ts and (not store.get(pid) or ts > store[pid]):
            store[pid] = ts

    if ids:
        for pid, kind, ts in db.query(ContactActivity.prospect_id, ContactActivity.activity_type,
                                      func.max(ContactActivity.occurred_at)).filter(
                ContactActivity.prospect_id.in_(ids)).group_by(ContactActivity.prospect_id, ContactActivity.activity_type):
            _bump(last_activity, pid, ts)
            if kind in ("CALL", "EMAIL", "MEETING"):
                _bump(last_contacted, pid, ts)
        for pid, ts in db.query(EmailMessage.prospect_id, func.max(EmailMessage.sent_at)).filter(
                EmailMessage.prospect_id.in_(ids), EmailMessage.sent_at.isnot(None),
                EmailMessage.direction == "OUTBOUND").group_by(EmailMessage.prospect_id):
            _bump(last_activity, pid, ts)
            _bump(last_contacted, pid, ts)

    return [{
        "prospect_id": p.prospect_id,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "full_name": p.full_name,
        "email": p.email,
        "phone": p.phone,
        "mobile_phone": p.mobile_phone,
        "designation": p.designation,
        "company_name": p.company_name,
        "account_id": p.account_id,
        "account_name": accounts.get(p.account_id, (None, None))[0],
        "account_domain": accounts.get(p.account_id, (None, None))[1],
        "owner_id": p.owner_id,
        "owner_name": _user_name(owners.get(p.owner_id)),
        "tags": p.tags or [],
        "industry": p.industry,
        "emp_band": p.emp_band,
        "poc_city": p.poc_city,
        "poc_state": p.poc_state,
        "poc_country": p.poc_country,
        "timezone": p.timezone,
        "linkedin_url": p.linkedin_url,
        "lifecycle_stage": p.lifecycle_stage,
        "lead_status": p.lead_status,
        "lead_source": p.lead_source,
        "legal_basis": p.legal_basis,
        "consent_status": p.consent_status,
        "is_valid_email": p.is_valid_email,
        "custom_fields": p.custom_fields or {},
        "quality_flags": crm.quality_flags(p),
        "last_activity_at": last_activity.get(p.prospect_id),
        "last_contacted_at": last_contacted.get(p.prospect_id),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    } for p in prospects]


def _email_meta(email: str) -> tuple:
    from app.utils.email_utils import parse_email
    info = parse_email(email)
    if info:
        return info.get("email_type"), info.get("email_provider")
    return "PERSONAL", email.split("@")[-1] if "@" in email else None


_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,25}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_custom_fields(db: Session, tenant_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate against the definitions; blank values remove the key."""
    defs = {d.field_key: d for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id)}
    cleaned = {}
    for key, value in (values or {}).items():
        definition = defs.get(key)
        if not definition:
            raise HTTPException(status_code=400, detail=f"Unknown custom property '{key}'")
        label, ftype, options = definition.label, definition.field_type, definition.options or []
        if value is None or value == [] or (isinstance(value, str) and not value.strip()):
            cleaned[key] = None
            continue
        if ftype == "NUMBER":
            try:
                value = float(value)
                value = int(value) if value.is_integer() else value
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"'{label}' must be a number")
        elif ftype in ("SELECT", "RADIO"):
            if value not in options:
                raise HTTPException(status_code=400, detail=f"'{label}' must be one of {options}")
        elif ftype == "MULTI_CHECKBOX":
            items = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
            bad = [v for v in items if v not in options]
            if bad:
                raise HTTPException(status_code=400, detail=f"'{label}' has unknown choices {bad}; allowed {options}")
            value = [o for o in options if o in items]
        elif ftype == "DATE":
            value = str(value).strip()[:10]
            if not _DATE_RE.match(value):
                raise HTTPException(status_code=400, detail=f"'{label}' must be a date (YYYY-MM-DD)")
        elif ftype == "PHONE":
            value = str(value).strip()
            if not _PHONE_RE.match(value):
                raise HTTPException(status_code=400, detail=f"'{label}' must be a phone number")
        elif ftype == "URL":
            value = str(value).strip()
            if not re.match(r"^https?://", value):
                value = f"https://{value}"
        else:
            value = str(value).strip()[:1000]
        cleaned[key] = value
    return cleaned


def _check_enum(field, value, allowed):
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"{field} must be one of {list(allowed)}")


def _sync_subscription(db: Session, prospect: Prospect, status: str, user: User):
    """Keep the global unsubscribe list in step with the contact's subscription (BR-CM-39)."""
    from app.services.suppression import lift, suppress
    if status == "UNSUBSCRIBED":
        # Excluded from every campaign at once; pending emails are cancelled (BR-DF-08)
        suppress(db, prospect.tenant_id, prospect.email, f"Unsubscribed by {_user_name(user)}",
                 kind="UNSUBSCRIBE", source="MANUAL")
    else:
        lift(db, prospect.tenant_id, prospect.email)
    prospect.consent_timestamp = datetime.utcnow()
    prospect.consent_source = "MANUAL"


def _apply_fields(db: Session, user: User, prospect: Prospect, data: dict, creating: bool = False):
    """Apply create/update fields, keeping company name, company link and subscription in step."""
    manager = can_manage_contacts(user)

    if "owner_id" in data:
        if not manager and data["owner_id"] != user.user_id:
            raise HTTPException(status_code=403, detail="Only users who manage prospects can reassign contacts")
        _check_owner(db, user, data["owner_id"])
        prospect.owner_id = data["owner_id"]

    for field in TEXT_FIELDS:
        if field in data:
            setattr(prospect, field, clean_str(data[field]))

    if "poc_state" in data:
        prospect.poc_state = clean_str(data["poc_state"])
        from app.utils.business_calendar import get_timezone_for_state
        prospect.timezone = get_timezone_for_state(prospect.poc_state) if prospect.poc_state else None

    if "lifecycle_stage" in data:
        stage = data["lifecycle_stage"]
        _check_enum("lifecycle_stage", stage, crm.LIFECYCLE_STAGES)
        if not crm.lifecycle_move_allowed(prospect.lifecycle_stage, stage, user):
            raise HTTPException(status_code=400, detail=(
                f"Lifecycle stage moves forward only: {crm.LIFECYCLE_STAGES[prospect.lifecycle_stage]} "
                f"can't go back to {crm.LIFECYCLE_STAGES[stage]}. Ask an admin to change it."))
        prospect.lifecycle_stage = stage
    if "lead_status" in data:
        _check_enum("lead_status", data["lead_status"], crm.LEAD_STATUSES)
        prospect.lead_status = data["lead_status"]
    if "legal_basis" in data:
        _check_enum("legal_basis", data["legal_basis"], crm.LEGAL_BASES)
        prospect.legal_basis = data["legal_basis"]

    if "consent_status" in data and data["consent_status"] != prospect.consent_status:
        _check_enum("consent_status", data["consent_status"], ("OPT_IN", "UNSUBSCRIBED"))
        prospect.consent_status = data["consent_status"]
        if not creating:
            _sync_subscription(db, prospect, prospect.consent_status, user)

    if "tags" in data:
        prospect.tags = normalize_tags(data["tags"]) or None

    if "custom_fields" in data:
        merged = dict(prospect.custom_fields or {})
        for key, value in _clean_custom_fields(db, user.tenant_id, data["custom_fields"] or {}).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        prospect.custom_fields = merged or None

    # An explicit company wins; otherwise a changed company name relinks (by domain first)
    if "account_id" in data:
        if data["account_id"]:
            account = _get_account(db, user, data["account_id"])
            prospect.account_id = account.account_id
            prospect.company_name = account.name
        else:
            prospect.account_id = None
            if "company_name" in data:
                prospect.company_name = clean_str(data["company_name"])
    elif "company_name" in data or creating:
        if "company_name" in data:
            prospect.company_name = clean_str(data["company_name"])
        account = crm.resolve_company(db, user.tenant_id, prospect.email if creating else None,
                                      prospect.company_name, industry=prospect.industry, emp_band=prospect.emp_band)
        if account is None and not creating and prospect.company_name is None:
            prospect.account_id = None
        elif account is not None:
            prospect.account_id = account.account_id
            prospect.company_name = prospect.company_name or account.name


def _required_missing(db: Session, tenant_id: str, prospect: Prospect) -> List[str]:
    required = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.required.is_(True)).all()
    values = prospect.custom_fields or {}
    return [d.label for d in required if values.get(d.field_key) in (None, "", [])]


def search_condition(q: str):
    """Match name, email, company, title or phone (digits only, formatting ignored)."""
    term = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conditions = [Prospect.search_text.like(f"%{term}%")]
    digits = phone_digits(q)
    if len(digits) >= 4 and digits != term:
        conditions.append(Prospect.search_text.like(f"%{digits}%"))
    return or_(*conditions)


def _filtered_query(db: Session, user: User, q=None, owner=None, account_id=None, tag=None, list_id=None,
                    campaign_id=None, consent_status=None, email_valid=None, has_phone=None, country=None,
                    industry=None, lifecycle_stage=None, lead_status=None, filters=None):
    query = _visible(db, user)
    # Owner filters narrow within what the user may already see
    if owner == "me":
        query = query.filter(Prospect.owner_id == user.user_id)
    elif owner == "unassigned":
        query = query.filter(Prospect.owner_id.is_(None))
    elif owner == "team":
        pass
    elif owner:
        query = query.filter(Prospect.owner_id == owner)

    if q and q.strip():
        query = query.filter(search_condition(q))

    if account_id:
        query = query.filter(Prospect.account_id == account_id)
    if tag:
        query = query.filter(func.json_contains(Prospect.tags, func.json_quote(tag)) == 1)
    if list_id:
        plist = db.query(ProspectList).filter(ProspectList.list_id == list_id,
                                              ProspectList.tenant_id == user.tenant_id).first()
        if not plist:
            raise HTTPException(status_code=404, detail="List not found")
        query = query.filter(Prospect.prospect_id.in_(crm.list_member_ids(db, plist, user)))
    if campaign_id:
        query = query.filter(Prospect.prospect_id.in_(
            db.query(CampaignProspect.prospect_id).filter(CampaignProspect.campaign_id == campaign_id)))
    if consent_status:
        query = query.filter(Prospect.consent_status == consent_status)
    if email_valid is not None:
        query = query.filter(Prospect.is_valid_email == email_valid)
    if has_phone is True:
        query = query.filter(or_(func.coalesce(Prospect.phone, "") != "", func.coalesce(Prospect.mobile_phone, "") != ""))
    elif has_phone is False:
        query = query.filter(func.coalesce(Prospect.phone, "") == "", func.coalesce(Prospect.mobile_phone, "") == "")
    if country:
        query = query.filter(Prospect.poc_country == country)
    if industry:
        query = query.filter(Prospect.industry == industry)
    if lifecycle_stage:
        query = query.filter(Prospect.lifecycle_stage == lifecycle_stage)
    if lead_status:
        query = query.filter(Prospect.lead_status == lead_status)
    if filters:
        try:
            clause = crm.filter_clause(db, crm.parse_filter_param(filters), user)
        except crm.FilterError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if clause is not None:
            query = query.filter(clause)
    return query


# =============================
# COLLECTION
# =============================

@router.get("/meta")
def contact_meta(current_user: User = Depends(tenant_user)):
    """Picklist values and table columns for the UI."""
    return {
        "lifecycle_stages": [{"value": k, "label": v} for k, v in crm.LIFECYCLE_STAGES.items()],
        "lead_statuses": [{"value": k, "label": v} for k, v in crm.LEAD_STATUSES.items()],
        "legal_bases": [{"value": k, "label": v} for k, v in crm.LEGAL_BASES.items()],
        "columns": [{"key": k, "label": v} for k, v in COLUMNS.items()],
        "can_manage": can_manage_contacts(current_user),
        "can_export": can_manage_contacts(current_user) and current_user.role in ("SUPER_ADMIN", "ADMIN", "MANAGER"),
        "can_override_lifecycle": crm.can_override_lifecycle(current_user),
        "restore_window_days": crm.RESTORE_WINDOW_DAYS,
    }


@router.get("")
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
    sort_by: str = Query("created_at", pattern="^(name|email|company|lifecycle_stage|lead_status|created_at|updated_at)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    query = _filtered_query(db, current_user, q, owner, account_id, tag, list_id, campaign_id, consent_status,
                            email_valid, has_phone, country, industry, lifecycle_stage, lead_status, filters)
    direction = (lambda c: c.asc()) if sort_order == "asc" else (lambda c: c.desc())
    order = [direction(c) for c in SORT_COLUMNS[sort_by]] + [direction(Prospect.prospect_id)]
    if q and q.strip():
        # A search usually matches few rows: scan once, counting with a window function, and
        # don't let MySQL walk the date index looking for matches (contact search < 1 s at 100k)
        paged = query.add_columns(func.count().over().label("total")).with_hint(
            Prospect, "IGNORE INDEX FOR ORDER BY (ix_prospects_tenant_live_created, ix_prospects_tenant_live_updated)",
            "mysql").order_by(*order).offset((page - 1) * page_size).limit(page_size).all()
        rows = [r[0] for r in paged]
        total = paged[0][1] if paged else query.with_entities(func.count(Prospect.prospect_id)).order_by(None).scalar()
    else:
        # COUNT on the id (not .count(), which wraps every column in a subquery); the tiebreaker
        # sorts the same way as the sort column, so MySQL walks the index instead of sorting
        total = query.with_entities(func.count(Prospect.prospect_id)).order_by(None).scalar()
        rows = query.order_by(*order).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": _summaries(db, rows), "total": total, "page": page, "page_size": page_size}


@router.get("/board")
def contacts_board(
    group_by: str = Query("lifecycle_stage", pattern="^(lifecycle_stage|lead_status)$"),
    per_column: int = Query(50, ge=1, le=200),
    q: Optional[str] = None,
    owner: Optional[str] = None,
    filters: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """Board view: contacts grouped by lifecycle stage or lead status (BR-CM-20)."""
    values = crm.LIFECYCLE_STAGES if group_by == "lifecycle_stage" else crm.LEAD_STATUSES
    column = getattr(Prospect, group_by)
    base = _filtered_query(db, current_user, q=q, owner=owner, filters=filters)
    counts = dict(base.with_entities(column, func.count(Prospect.prospect_id)).group_by(column).all())
    lanes, all_rows = [], []
    for value, label in list(values.items()) + [(None, "Not set")]:
        rows = base.filter(column == value if value else column.is_(None)).order_by(
            Prospect.updated_at.desc()).limit(per_column).all() if counts.get(value) else []
        if value is None and not rows:
            continue
        lanes.append((value, label, rows))
        all_rows.extend(rows)
    summaries = {s["prospect_id"]: s for s in _summaries(db, all_rows)}
    return {"group_by": group_by, "lanes": [
        {"value": v, "label": l, "total": counts.get(v, 0), "items": [summaries[r.prospect_id] for r in rows]}
        for v, l, rows in lanes]}


@router.post("", status_code=201)
def create_contact(
    payload: ContactCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    email = payload.email.strip()
    existing = db.query(Prospect).filter(
        Prospect.tenant_id == current_user.tenant_id, Prospect.email == email
    ).first()
    if existing:
        if existing.deleted_at is not None:
            raise HTTPException(status_code=409, detail={
                "message": "A deleted contact has this email. Restore it from Recently deleted instead.",
                "prospect_id": existing.prospect_id, "deleted": True})
        raise HTTPException(status_code=409, detail={
            "message": "A contact with this email already exists.",
            "prospect_id": existing.prospect_id if can_access_contact(current_user, existing) else None,
        })

    email_type, email_provider = _email_meta(email)
    unsubscribed = db.query(GlobalUnsubscribe).filter(
        GlobalUnsubscribe.tenant_id == current_user.tenant_id, func.lower(GlobalUnsubscribe.email) == email.lower()
    ).first()

    prospect = Prospect(
        prospect_id=str(uuid.uuid4()),
        tenant_id=current_user.tenant_id,
        email=email,
        email_type=email_type,
        email_provider=email_provider,
        consent_status="UNSUBSCRIBED" if unsubscribed else "OPT_IN",
        consent_source="MANUAL",
        consent_timestamp=datetime.utcnow(),
        is_valid_email=True,
        owner_id=current_user.user_id,
        lifecycle_stage="LEAD",
        lead_status="NEW",
    )
    data = payload.model_dump(exclude_unset=True, exclude={"email", "list_id"})
    data.setdefault("owner_id", current_user.user_id)
    if unsubscribed:
        data.pop("consent_status", None)
    _apply_fields(db, current_user, prospect, data, creating=True)
    missing = _required_missing(db, current_user.tenant_id, prospect)
    if missing:
        raise HTTPException(status_code=400, detail=f"Required: {', '.join(missing)}")
    db.add(prospect)
    db.flush()
    if prospect.consent_status == "UNSUBSCRIBED" and not unsubscribed:
        _sync_subscription(db, prospect, "UNSUBSCRIBED", current_user)

    if payload.list_id:
        target = db.query(ProspectList).filter(
            ProspectList.list_id == payload.list_id, ProspectList.tenant_id == current_user.tenant_id
        ).first()
        if not target or target.list_type != "STATIC":
            raise HTTPException(status_code=400, detail="Static list not found")
        db.add(ProspectListMember(list_id=target.list_id, prospect_id=prospect.prospect_id, is_new_prospect=True))

    db.add(PropertyChange(tenant_id=current_user.tenant_id, object_type="CONTACT", object_id=prospect.prospect_id,
                          field="created", new_value="Contact created", source="UI", changed_by=current_user.user_id))
    db.commit()
    return _contact_detail(db, current_user, prospect)


@router.get("/facets")
def contact_facets(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Values for the filter dropdowns: tags, countries, industries, lists and campaigns."""
    base = _visible(db, current_user)

    tag_counts: Dict[str, list] = {}
    for (tags,) in base.with_entities(Prospect.tags).filter(Prospect.tags.isnot(None)):
        for tag in tags or []:
            entry = tag_counts.setdefault(tag.lower(), [tag, 0])
            entry[1] += 1

    def distinct(column):
        return sorted(v for (v,) in base.with_entities(column).filter(column.isnot(None), column != "").distinct())

    lists = db.query(ProspectList.list_id, ProspectList.list_name, ProspectList.list_type).filter(
        ProspectList.tenant_id == current_user.tenant_id).order_by(ProspectList.uploaded_at.desc()).all()
    campaigns = db.query(Campaign.campaign_id, Campaign.campaign_name, Campaign.status).filter(
        Campaign.tenant_id == current_user.tenant_id).order_by(Campaign.created_at.desc()).all()

    return {
        "tags": [{"tag": t, "count": c} for t, c in sorted(tag_counts.values(), key=lambda x: (-x[1], x[0].lower()))],
        "countries": distinct(Prospect.poc_country),
        "industries": distinct(Prospect.industry),
        "lead_sources": distinct(Prospect.lead_source),
        "lists": [{"list_id": i, "list_name": n, "list_type": t} for i, n, t in lists],
        "campaigns": [{"campaign_id": i, "campaign_name": n, "status": s} for i, n, s in campaigns],
    }


@router.get("/owners")
def contact_owners(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Active users in the workspace, for owner pickers."""
    users = db.query(User).filter(
        User.tenant_id == current_user.tenant_id, User.status == "ACTIVE"
    ).order_by(User.first_name, User.last_name).all()
    visible = visible_user_ids(db, current_user)
    if visible is not None:  # hierarchy users pick owners from their own team (BR-SH-02)
        users = [u for u in users if u.user_id in visible]
    return [{"user_id": u.user_id, "name": _user_name(u), "email": u.email, "role": u.role} for u in users]


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
):
    """Export a view to CSV / XLSX. Admins and managers only (BR-CM-31)."""
    if not (can_manage_contacts(current_user) and current_user.role in ("SUPER_ADMIN", "ADMIN", "MANAGER")):
        raise HTTPException(status_code=403, detail="Only admins and managers can export contacts")
    keys = [c for c in (columns or "").split(",") if c in COLUMNS] or list(COLUMNS)
    field_defs = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == current_user.tenant_id).order_by(ContactFieldDefinition.sort_order).all()
    query = _filtered_query(db, current_user, q=q, owner=owner, list_id=list_id, lifecycle_stage=lifecycle_stage,
                            lead_status=lead_status, tag=tag, filters=filters).order_by(Prospect.created_at.desc())

    header = [COLUMNS[k] for k in keys] + [f.label for f in field_defs]
    rows = []
    for offset in range(0, 100_000, 1000):
        chunk = query.offset(offset).limit(1000).all()
        if not chunk:
            break
        for s in _summaries(db, chunk):
            values = []
            for k in keys:
                v = s.get(k)
                if k == "tags":
                    v = "; ".join(v or [])
                elif k == "lifecycle_stage":
                    v = crm.LIFECYCLE_STAGES.get(v, v)
                elif k == "lead_status":
                    v = crm.LEAD_STATUSES.get(v, v)
                elif isinstance(v, datetime):
                    v = v.strftime("%Y-%m-%d %H:%M")
                values.append(v)
            for f in field_defs:
                v = s["custom_fields"].get(f.field_key)
                values.append("; ".join(v) if isinstance(v, list) else v)
            rows.append(values)

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    if format == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Contacts"
        ws.append(header)
        for r in rows:
            ws.append(r)
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f'attachment; filename="contacts-{stamp}.xlsx"'})
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return StreamingResponse(iter([out.getvalue().encode("utf-8-sig")]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="contacts-{stamp}.csv"'})


@router.post("/bulk")
def bulk_update(
    payload: BulkAction,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """Bulk edit, assign, tag, add to list, enroll or delete (BR-CM-36/37)."""
    _require_manager(current_user)
    prospects = _visible(db, current_user).filter(Prospect.prospect_id.in_(payload.prospect_ids)).all()
    if not prospects:
        raise HTTPException(status_code=404, detail="No matching contacts")
    action = payload.action
    result: Dict[str, Any] = {"status": "ok", "action": action, "updated": len(prospects)}

    def tracked(fn, fields):
        for p in prospects:
            before = crm.snapshot(p, fields)
            fn(p)
            crm.record_changes(db, current_user.tenant_id, "CONTACT", p.prospect_id, before,
                               crm.snapshot(p, fields), current_user.user_id, "BULK")

    if action == "assign_owner":
        _check_owner(db, current_user, payload.owner_id)
        tracked(lambda p: setattr(p, "owner_id", payload.owner_id), ("owner_id",))
    elif action in ("add_tags", "remove_tags"):
        tags = normalize_tags(payload.tags)
        if not tags:
            raise HTTPException(status_code=400, detail="Give at least one tag")
        remove = {t.lower() for t in tags}
        if action == "add_tags":
            tracked(lambda p: setattr(p, "tags", normalize_tags(list(p.tags or []) + tags)), ("tags",))
        else:
            tracked(lambda p: setattr(p, "tags", [t for t in (p.tags or []) if t.lower() not in remove] or None), ("tags",))
    elif action == "set_account":
        account = _get_account(db, current_user, payload.account_id) if payload.account_id else None

        def link(p):
            p.account_id = account.account_id if account else None
            if account:
                p.company_name = account.name
        tracked(link, ("account_id", "company_name"))
    elif action == "set_property":
        field = payload.field or ""
        allowed = set(TEXT_FIELDS) | {"poc_state", "lifecycle_stage", "lead_status", "legal_basis", "consent_status"}
        if field not in allowed and not field.startswith("custom."):
            raise HTTPException(status_code=400, detail=f"Can't bulk edit '{field}'")
        data = ({"custom_fields": {field[len("custom."):]: payload.value}} if field.startswith("custom.")
                else {field: payload.value})
        skipped = []

        def edit(p):
            try:
                with db.begin_nested():
                    _apply_fields(db, current_user, p, dict(data))
            except HTTPException as exc:
                skipped.append({"prospect_id": p.prospect_id, "email": p.email, "reason": exc.detail})
        tracked(edit, tuple(TRACKED_FIELDS))
        result["skipped"] = skipped
        result["updated"] = len(prospects) - len(skipped)
    elif action in ("add_to_list", "remove_from_list"):
        if payload.new_list_name and action == "add_to_list":
            plist = ProspectList(tenant_id=current_user.tenant_id, list_name=payload.new_list_name.strip(),
                                 source_type="MANUAL", list_type="STATIC", uploaded_by=current_user.user_id)
            db.add(plist)
            db.flush()
        else:
            plist = db.query(ProspectList).filter(ProspectList.list_id == payload.list_id,
                                                  ProspectList.tenant_id == current_user.tenant_id).first()
            if not plist:
                raise HTTPException(status_code=404, detail="List not found")
        if plist.list_type != "STATIC":
            raise HTTPException(status_code=400, detail="Active lists update automatically; add contacts to a static list")
        ids = [p.prospect_id for p in prospects]
        if action == "add_to_list":
            existing = {r[0] for r in db.query(ProspectListMember.prospect_id).filter(
                ProspectListMember.list_id == plist.list_id, ProspectListMember.prospect_id.in_(ids))}
            added = [pid for pid in ids if pid not in existing]
            for pid in added:
                db.add(ProspectListMember(list_id=plist.list_id, prospect_id=pid, is_new_prospect=False))
            result.update(updated=len(added), already_in_list=len(existing))
        else:
            result["updated"] = db.query(ProspectListMember).filter(
                ProspectListMember.list_id == plist.list_id, ProspectListMember.prospect_id.in_(ids)
            ).delete(synchronize_session=False)
        result.update(list_id=plist.list_id, list_name=plist.list_name)
    elif action == "enroll":
        from app.schemas.campaign_schema import CampaignEnrollmentRequest
        from app.services.campaign_service import CampaignService
        from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
        campaign = db.query(Campaign).filter(Campaign.campaign_id == payload.campaign_id,
                                             Campaign.tenant_id == current_user.tenant_id).first()
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        try:
            count, rejections = CampaignService(db).enroll_prospects_with_report(
                campaign.campaign_id, current_user.user_id,
                CampaignEnrollmentRequest(prospect_ids=[p.prospect_id for p in prospects]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result.update(updated=count, enrolled_count=count, rejected_count=len(rejections),
                      rejected_summary=summarize(rejections), rejected=rejections[:MAX_REJECTIONS_RETURNED])
        from app.services.daily_limit import enrollment_notice
        result["daily_limit_notice"] = enrollment_notice(db, current_user, count)
    elif action == "delete":
        if current_user.role == "AGENT":
            raise HTTPException(status_code=403, detail="Agents cannot delete contacts")
        result["updated"] = crm.soft_delete_contacts(db, prospects, current_user.user_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'")

    db.commit()
    return result


# =============================
# RECENTLY DELETED (BR-CM-35)
# =============================

@router.get("/deleted")
def deleted_contacts(
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    _require_manager(current_user)
    cutoff = datetime.utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS)
    query = db.query(Prospect).filter(
        Prospect.tenant_id == current_user.tenant_id, Prospect.deleted_at.isnot(None),
        Prospect.deleted_at > cutoff, Prospect.merged_into_id.is_(None))
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(or_(Prospect.email.ilike(term), Prospect.first_name.ilike(term),
                                 Prospect.last_name.ilike(term), Prospect.company_name.ilike(term)))
    total = query.count()
    rows = query.order_by(Prospect.deleted_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    deleters = {u.user_id: u for u in db.query(User).filter(User.user_id.in_({p.deleted_by for p in rows if p.deleted_by}))}
    items = []
    for s, p in zip(_summaries(db, rows), rows):
        s.update(deleted_at=p.deleted_at, deleted_by_name=_user_name(deleters.get(p.deleted_by)),
                 purge_at=p.deleted_at + timedelta(days=crm.RESTORE_WINDOW_DAYS))
        items.append(s)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/restore")
def restore(payload: RestoreRequest, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    count = crm.restore_contacts(db, current_user.tenant_id, payload.prospect_ids, current_user.user_id)
    db.commit()
    return {"status": "restored", "restored": count}


# =============================
# DUPLICATES & MERGE
# =============================

@router.get("/duplicates")
def find_duplicates(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """
    Groups of likely duplicates (BR-CM-32): the same email apart from case or dots in
    Gmail-style addresses, a similar name at the same company, or the same phone number.
    """
    _require_manager(current_user)
    rows = _visible(db, current_user).with_entities(
        Prospect.prospect_id, Prospect.first_name, Prospect.last_name, Prospect.company_name,
        Prospect.account_id, Prospect.phone, Prospect.mobile_phone, Prospect.email,
    ).all()

    def norm(value):
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    def email_key(email):
        local, _, domain = (email or "").lower().partition("@")
        local = local.split("+")[0]
        if crm.is_personal_domain(domain):
            local = local.replace(".", "")
        return f"{local}@{domain}"

    buckets: Dict[tuple, set] = {}
    for r in rows:
        buckets.setdefault(("email", email_key(r.email)), set()).add(r.prospect_id)
        first, last = norm(r.first_name), norm(r.last_name)
        company = r.account_id or norm(r.company_name)
        if first and last and company:
            buckets.setdefault(("name", first, last, company), set()).add(r.prospect_id)
            if len(first) > 1:  # "Rob" / "Robert" and initials at the same company
                buckets.setdefault(("initial", first[0], last, company), set()).add(r.prospect_id)
        for phone in {phone_digits(r.phone), phone_digits(r.mobile_phone)}:
            if len(phone) >= 7:
                buckets.setdefault(("phone", phone[-10:]), set()).add(r.prospect_id)

    labels = {"email": "Same email address", "name": "Same name and company",
              "initial": "Similar name at the same company", "phone": "Same phone number"}
    groups: List[dict] = []
    seen: Dict[str, int] = {}
    for key, ids in buckets.items():
        if len(ids) < 2:
            continue
        reason = labels[key[0]]
        hit = next((seen[i] for i in ids if i in seen), None)
        if hit is None:
            groups.append({"ids": set(ids), "reasons": {reason}})
            hit = len(groups) - 1
        else:
            groups[hit]["ids"] |= ids
            groups[hit]["reasons"].add(reason)
        for i in groups[hit]["ids"]:
            seen[i] = hit

    groups = groups[:limit]
    all_ids = {i for g in groups for i in g["ids"]}
    by_id = {p.prospect_id: p for p in db.query(Prospect).filter(Prospect.prospect_id.in_(all_ids))} if all_ids else {}
    summaries = {s["prospect_id"]: s for s in _summaries(db, list(by_id.values()))}
    return {
        "groups": [{
            "reasons": sorted(g["reasons"]),
            "contacts": sorted((summaries[i] for i in g["ids"] if i in summaries),
                               key=lambda s: s["created_at"] or datetime.min),
        } for g in groups],
        "total_groups": len(groups),
        "mergeable_properties": list(MERGE_CHOOSABLE),
    }


@router.post("/merge")
def merge(
    payload: MergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    _require_manager(current_user)
    duplicate_ids = [i for i in dict.fromkeys(payload.duplicate_ids) if i != payload.primary_id]
    if not duplicate_ids:
        raise HTTPException(status_code=400, detail="Choose at least one other contact to merge")
    primary = _get_contact(db, current_user, payload.primary_id)
    duplicates = _visible(db, current_user).filter(Prospect.prospect_id.in_(duplicate_ids)).all()
    if len(duplicates) != len(duplicate_ids):
        raise HTTPException(status_code=404, detail="One or more contacts to merge were not found")

    before = crm.snapshot(primary, TRACKED_FIELDS)
    moved = merge_contacts(db, primary, duplicates, current_user.user_id, payload.choices)
    crm.record_changes(db, current_user.tenant_id, "CONTACT", primary.prospect_id, before,
                       crm.snapshot(primary, TRACKED_FIELDS), current_user.user_id, "MERGE")
    db.commit()
    db.refresh(primary)
    return {"status": "merged", "merged": len(duplicates), "moved": moved,
            "contact": _contact_detail(db, current_user, primary)}


# =============================
# CUSTOM PROPERTIES (BR-CM-09/10)
# =============================

def _field_dict(d: ContactFieldDefinition) -> dict:
    return {"field_id": d.field_id, "field_key": d.field_key, "label": d.label, "field_type": d.field_type,
            "options": d.options or [], "sort_order": d.sort_order or 0, "group_name": d.group_name,
            "required": bool(d.required)}


def _validate_field(payload: FieldWrite):
    field_type = payload.field_type.upper()
    if field_type not in FIELD_TYPES:
        raise HTTPException(status_code=400, detail=f"field_type must be one of {list(FIELD_TYPES)}")
    has_options = field_type in ("SELECT", "RADIO", "MULTI_CHECKBOX")
    options = normalize_tags(payload.options) if has_options else None
    if has_options and not options:
        raise HTTPException(status_code=400, detail="A choice property needs at least one option")
    return field_type, options


def create_field_definition(db: Session, tenant_id: str, label: str, field_type: str = "TEXT",
                            options=None, group_name=None, required=False) -> ContactFieldDefinition:
    """Shared with import (creating properties while mapping columns, BR-CM-26)."""
    base_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:50] or "field"
    key, n = base_key, 2
    while db.query(ContactFieldDefinition.field_id).filter(
            ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.field_key == key).first():
        key, n = f"{base_key}_{n}", n + 1
    count = db.query(func.count(ContactFieldDefinition.field_id)).filter(
        ContactFieldDefinition.tenant_id == tenant_id).scalar()
    definition = ContactFieldDefinition(tenant_id=tenant_id, field_key=key, label=label.strip(),
                                        field_type=field_type, options=options, sort_order=count,
                                        group_name=clean_str(group_name), required=required)
    db.add(definition)
    db.flush()
    return definition


@router.get("/fields")
def list_fields(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    defs = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == current_user.tenant_id
    ).order_by(ContactFieldDefinition.group_name, ContactFieldDefinition.sort_order, ContactFieldDefinition.created_at).all()
    return [_field_dict(d) for d in defs]


@router.post("/fields", status_code=201)
def create_field(payload: FieldWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    field_type, options = _validate_field(payload)
    definition = create_field_definition(db, current_user.tenant_id, payload.label, field_type, options,
                                         payload.group_name, payload.required)
    if payload.sort_order is not None:
        definition.sort_order = payload.sort_order
    db.commit()
    return _field_dict(definition)


@router.patch("/fields/{field_id}")
def update_field(field_id: str, payload: FieldWrite, db: Session = Depends(get_db),
                 current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    definition = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.field_id == field_id,
        ContactFieldDefinition.tenant_id == current_user.tenant_id).first()
    if not definition:
        raise HTTPException(status_code=404, detail="Property not found")
    field_type, options = _validate_field(payload)
    definition.label = payload.label.strip()
    definition.field_type = field_type
    definition.options = options
    definition.group_name = clean_str(payload.group_name)
    definition.required = payload.required
    if payload.sort_order is not None:
        definition.sort_order = payload.sort_order
    db.commit()
    return _field_dict(definition)


@router.delete("/fields/{field_id}")
def delete_field(field_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Removes the property definition. Stored values stay on contacts but are no longer shown."""
    _require_manager(current_user)
    deleted = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.field_id == field_id,
        ContactFieldDefinition.tenant_id == current_user.tenant_id).delete()
    if not deleted:
        raise HTTPException(status_code=404, detail="Property not found")
    db.commit()
    return {"status": "deleted", "field_id": field_id}


# =============================
# ACTIVITIES (by id)
# =============================

def _activity_dict(a: ContactActivity, authors: Dict[str, User]) -> dict:
    return {
        "activity_id": a.activity_id,
        "prospect_id": a.prospect_id,
        "activity_type": a.activity_type,
        "subject": a.subject,
        "body": a.body,
        "outcome": a.outcome,
        "duration_minutes": a.duration_minutes,
        "occurred_at": a.occurred_at,
        "created_by": a.created_by,
        "created_by_name": _user_name(authors.get(a.created_by)),
        "created_at": a.created_at,
    }


def _get_activity(db: Session, user: User, activity_id: str) -> ContactActivity:
    activity = db.query(ContactActivity).filter(
        ContactActivity.activity_id == activity_id,
        ContactActivity.tenant_id == user.tenant_id).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    _get_contact(db, user, activity.prospect_id)
    if activity.created_by != user.user_id and not can_manage_contacts(user):
        raise HTTPException(status_code=403, detail="You can only change activities you logged")
    return activity


@router.patch("/activities/{activity_id}")
def update_activity(activity_id: str, payload: ActivityUpdate, db: Session = Depends(get_db),
                    current_user: User = Depends(tenant_user)):
    activity = _get_activity(db, current_user, activity_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "occurred_at":
            if value is None:
                continue
            value = _utc_naive(value)
        setattr(activity, field, clean_str(value) if isinstance(value, str) else value)
    db.commit()
    return _activity_dict(activity, {current_user.user_id: current_user})


@router.delete("/activities/{activity_id}")
def delete_activity(activity_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    activity = _get_activity(db, current_user, activity_id)
    db.delete(activity)
    db.commit()
    return {"status": "deleted", "activity_id": activity_id}


# =============================
# SINGLE CONTACT
# =============================

def _contact_detail(db: Session, user: User, p: Prospect) -> dict:
    detail = _summaries(db, [p])[0]
    detail.update({
        "email_type": p.email_type,
        "email_provider": p.email_provider,
        "consent_source": p.consent_source,
        "consent_timestamp": p.consent_timestamp,
    })

    account = db.query(Account).filter(Account.account_id == p.account_id,
                                       Account.deleted_at.is_(None)).first() if p.account_id else None
    detail["account"] = {
        "account_id": account.account_id, "name": account.name, "domain": account.domain,
        "website": account.website, "industry": account.industry, "emp_band": account.emp_band,
        "phone": account.phone, "city": account.city, "country": account.country,
        "lifecycle_stage": account.lifecycle_stage,
        "contact_count": db.query(func.count(Prospect.prospect_id)).filter(
            Prospect.account_id == account.account_id, Prospect.deleted_at.is_(None)).scalar(),
    } if account else None

    static = [{
        "list_id": l.list_id, "list_name": l.list_name, "list_type": "STATIC", "added_at": m.added_at, "notes": m.notes,
    } for m, l in db.query(ProspectListMember, ProspectList).join(
        ProspectList, ProspectList.list_id == ProspectListMember.list_id
    ).filter(ProspectListMember.prospect_id == p.prospect_id).order_by(ProspectListMember.added_at.desc())]
    active = []
    for plist in db.query(ProspectList).filter(ProspectList.tenant_id == p.tenant_id, ProspectList.list_type == "ACTIVE"):
        try:
            if crm.list_member_ids(db, plist, user).filter(Prospect.prospect_id == p.prospect_id).first():
                active.append({"list_id": plist.list_id, "list_name": plist.list_name, "list_type": "ACTIVE"})
        except crm.FilterError:
            continue
    detail["lists"] = static + active

    detail["campaigns"] = [{
        "campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "campaign_status": c.status,
        "status": e.status, "current_step": e.current_step, "stopped_reason": e.stopped_reason,
        "enrolled_at": e.enrolled_at,
    } for e, c in db.query(CampaignProspect, Campaign).join(
        Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
    ).filter(CampaignProspect.prospect_id == p.prospect_id).order_by(CampaignProspect.enrolled_at.desc())]

    persona = db.query(ProspectPersona).filter(ProspectPersona.prospect_id == p.prospect_id).first()
    detail["persona"] = {"persona_type": persona.persona_type, "confidence_score": persona.confidence_score} if persona else None

    sent = db.query(func.count(EmailMessage.message_id), func.max(EmailMessage.sent_at)).filter(
        EmailMessage.prospect_id == p.prospect_id, EmailMessage.direction == "OUTBOUND",
        EmailMessage.sent_at.isnot(None)).one()
    received = db.query(func.count(EmailMessage.message_id), func.max(EmailMessage.sent_at)).filter(
        EmailMessage.prospect_id == p.prospect_id, EmailMessage.direction == "INBOUND").one()
    opens = db.query(func.count(func.distinct(EmailEvent.message_id))).join(
        EmailMessage, EmailMessage.message_id == EmailEvent.message_id
    ).filter(EmailMessage.prospect_id == p.prospect_id, EmailEvent.event_type == EmailEvent.EVENT_OPEN).scalar()
    activity_counts = dict(db.query(ContactActivity.activity_type, func.count(ContactActivity.activity_id)).filter(
        ContactActivity.prospect_id == p.prospect_id).group_by(ContactActivity.activity_type))
    open_tasks = db.query(func.count(CrmTask.task_id)).filter(
        CrmTask.prospect_id == p.prospect_id, CrmTask.status == "OPEN").scalar()
    detail["stats"] = {
        "emails_sent": sent[0], "last_emailed_at": sent[1],
        "replies": received[0], "last_reply_at": received[1],
        "emails_opened": opens or 0,
        "notes": activity_counts.get("NOTE", 0),
        "calls": activity_counts.get("CALL", 0),
        "meetings": activity_counts.get("MEETING", 0),
        "logged_emails": activity_counts.get("EMAIL", 0),
        "open_tasks": open_tasks or 0,
    }
    detail["can_edit_owner"] = can_manage_contacts(user)
    detail["can_delete"] = can_manage_contacts(user) and user.role != "AGENT"
    detail["can_override_lifecycle"] = crm.can_override_lifecycle(user)
    return detail


@router.get("/{prospect_id}")
def get_contact(prospect_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    return _contact_detail(db, current_user, _get_contact(db, current_user, prospect_id))


@router.patch("/{prospect_id}")
def update_contact(
    prospect_id: str,
    payload: ContactUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    prospect = _get_contact(db, current_user, prospect_id)
    before = crm.snapshot(prospect, TRACKED_FIELDS)
    data = payload.model_dump(exclude_unset=True)

    new_email = data.pop("email", None)
    if new_email and new_email.strip().lower() != (prospect.email or "").lower():
        new_email = new_email.strip()
        if db.query(Prospect.prospect_id).filter(
                Prospect.tenant_id == current_user.tenant_id, Prospect.email == new_email,
                Prospect.prospect_id != prospect_id).first():
            raise HTTPException(status_code=409, detail="A contact with this email already exists.")
        prospect.email = new_email
        prospect.email_type, prospect.email_provider = _email_meta(new_email)

    _apply_fields(db, current_user, prospect, data)
    crm.record_changes(db, current_user.tenant_id, "CONTACT", prospect.prospect_id, before,
                       crm.snapshot(prospect, TRACKED_FIELDS), current_user.user_id, "UI")
    db.commit()
    db.refresh(prospect)
    return _contact_detail(db, current_user, prospect)


@router.delete("/{prospect_id}")
def delete_contact(prospect_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Soft delete: hidden everywhere and restorable for 90 days (BR-CM-35)."""
    _require_manager(current_user)
    if current_user.role == "AGENT":
        raise HTTPException(status_code=403, detail="Agents cannot delete contacts")
    prospect = _get_contact(db, current_user, prospect_id)
    crm.soft_delete_contacts(db, [prospect], current_user.user_id)
    db.commit()
    return {"status": "deleted", "prospect_id": prospect_id, "restorable_days": crm.RESTORE_WINDOW_DAYS}


def _history_label(field: str, defs: Dict[str, str]) -> str:
    if field.startswith("custom."):
        return defs.get(field[len("custom."):], field[len("custom."):])
    return COLUMNS.get(field, {"account_id": "Company", "owner_id": "Owner", "emp_band": "Employees",
                               "deleted": "Deleted", "created": "Created"}.get(field, field.replace("_", " ").capitalize()))


def _history_value(field: str, value: Optional[str], users: Dict[str, str], companies: Dict[str, str]):
    if value is None:
        return None
    if field == "owner_id":
        return users.get(value, value)
    if field == "account_id":
        return companies.get(value, value)
    if field == "lifecycle_stage":
        return crm.LIFECYCLE_STAGES.get(value, value)
    if field == "lead_status":
        return crm.LEAD_STATUSES.get(value, value)
    if field == "legal_basis":
        return crm.LEGAL_BASES.get(value, value)
    return value


@router.get("/{prospect_id}/history")
def property_history(
    prospect_id: str,
    field: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """Every property change with old and new value, user, time and source (BR-CM-11)."""
    prospect = _get_contact(db, current_user, prospect_id)
    query = db.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                            PropertyChange.object_id == prospect.prospect_id)
    if field:
        query = query.filter(PropertyChange.field == field)
    return {"items": _history_items(db, current_user, query.order_by(PropertyChange.changed_at.desc()).limit(1000).all())}


def _history_items(db: Session, user: User, rows: List[PropertyChange]) -> List[dict]:
    defs = {d.field_key: d.label for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == user.tenant_id)}
    user_ids = {r.changed_by for r in rows} | {r.old_value for r in rows if r.field == "owner_id"} | \
               {r.new_value for r in rows if r.field == "owner_id"}
    users = {u.user_id: _user_name(u) for u in db.query(User).filter(User.user_id.in_({i for i in user_ids if i}))}
    company_ids = {v for r in rows if r.field == "account_id" for v in (r.old_value, r.new_value) if v}
    companies = {a.account_id: a.name for a in db.query(Account).filter(Account.account_id.in_(company_ids))} if company_ids else {}
    return [{
        "change_id": r.change_id, "field": r.field, "label": _history_label(r.field, defs),
        "old_value": _history_value(r.field, r.old_value, users, companies),
        "new_value": _history_value(r.field, r.new_value, users, companies),
        "source": r.source, "changed_by": r.changed_by, "changed_by_name": users.get(r.changed_by),
        "changed_at": r.changed_at,
    } for r in rows]


@router.get("/{prospect_id}/timeline")
def contact_timeline(
    prospect_id: str,
    types: Optional[str] = Query(None, description="Comma-separated: NOTE,CALL,MEETING,EMAIL,TASK,PROPERTY,CAMPAIGN,LIST"),
    user_id: Optional[str] = Query(None, description="Only items by this user"),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """Everything that happened with this contact, newest first (BR-CM-14)."""
    prospect = _get_contact(db, current_user, prospect_id)
    wanted = {t.strip().upper() for t in types.split(",")} if types else None

    def want(kind):
        return wanted is None or kind in wanted

    items = []

    activity_query = db.query(ContactActivity).filter(ContactActivity.prospect_id == prospect.prospect_id)
    if user_id:
        activity_query = activity_query.filter(ContactActivity.created_by == user_id)
    activities = activity_query.order_by(ContactActivity.occurred_at.desc()).limit(limit).all()
    author_ids = {a.created_by for a in activities if a.created_by}
    authors = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(author_ids))} if author_ids else {}
    for a in activities:
        kind = "EMAIL_LOGGED" if a.activity_type == "EMAIL" else a.activity_type
        if want(a.activity_type):
            items.append({"kind": kind, "at": a.occurred_at, "activity": _activity_dict(a, authors),
                          "user_id": a.created_by,
                          "can_edit": a.created_by == current_user.user_id or can_manage_contacts(current_user)})

    if want("TASK"):
        task_query = db.query(CrmTask).filter(CrmTask.prospect_id == prospect.prospect_id)
        if user_id:
            task_query = task_query.filter(CrmTask.created_by == user_id)
        from app.routers.tasks_router import task_dict
        for t in task_query.order_by(CrmTask.created_at.desc()).limit(limit):
            items.append({"kind": "TASK", "at": t.completed_at or t.created_at, "task": task_dict(db, t),
                          "user_id": t.created_by})

    if want("PROPERTY"):
        change_query = db.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                                       PropertyChange.object_id == prospect.prospect_id)
        if user_id:
            change_query = change_query.filter(PropertyChange.changed_by == user_id)
        for c in _history_items(db, current_user, change_query.order_by(PropertyChange.changed_at.desc()).limit(limit).all()):
            items.append({"kind": "PROPERTY_CHANGE", "at": c["changed_at"], "change": c, "user_id": c["changed_by"]})

    if want("EMAIL") and not user_id:
        messages = db.query(EmailMessage, Campaign.campaign_name).outerjoin(
            Campaign, Campaign.campaign_id == EmailMessage.campaign_id
        ).filter(
            EmailMessage.prospect_id == prospect.prospect_id,
            or_(EmailMessage.sent_at.isnot(None), EmailMessage.direction == "INBOUND"),
        ).order_by(func.coalesce(EmailMessage.sent_at, EmailMessage.scheduled_at).desc()).limit(limit).all()
        for m, campaign_name in messages:
            inbound = m.direction == "INBOUND"
            items.append({
                "kind": "EMAIL_RECEIVED" if inbound else "EMAIL_SENT",
                "at": m.sent_at or m.scheduled_at,
                "email": {
                    "message_id": m.message_id, "subject": m.subject,
                    "snippet": (m.body_text or "")[:400], "status": m.status, "final_status": m.final_status, "failure_reason": m.failure_reason,
                    "from_email": m.from_email, "to_email": m.to_email,
                    "campaign_id": m.campaign_id, "campaign_name": campaign_name,
                    "conversation_id": m.conversation_id,
                },
            })
        events = db.query(EmailEvent, EmailMessage.subject).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.prospect_id == prospect.prospect_id,
            EmailEvent.event_type.in_(list(EVENT_LABELS)),
        ).order_by(EmailEvent.event_time.asc()).limit(limit).all()
        opened = set()
        for e, subject in events:
            # One "opened" entry per email (the first open), not one per pixel load
            if e.event_type == EmailEvent.EVENT_OPEN:
                if e.message_id in opened:
                    continue
                opened.add(e.message_id)
            items.append({"kind": EVENT_LABELS[e.event_type], "at": e.event_time,
                          "email": {"message_id": e.message_id, "subject": subject}})

    if (want("CAMPAIGN") or want("EMAIL")) and not user_id:
        for e, c in db.query(CampaignProspect, Campaign).join(
                Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
        ).filter(CampaignProspect.prospect_id == prospect.prospect_id):
            if want("CAMPAIGN"):
                items.append({"kind": "CAMPAIGN_ENROLLED", "at": e.enrolled_at,
                              "campaign": {"campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "status": e.status}})

    if (want("LIST") or want("NOTE")) and not user_id:
        for m, l in db.query(ProspectListMember, ProspectList).join(
                ProspectList, ProspectList.list_id == ProspectListMember.list_id
        ).filter(ProspectListMember.prospect_id == prospect.prospect_id):
            if want("LIST"):
                items.append({"kind": "ADDED_TO_LIST", "at": m.added_at,
                              "list": {"list_id": l.list_id, "list_name": l.list_name}})
            if want("NOTE") and m.notes:
                # Notes written on an upload list before contacts had their own notes
                items.append({"kind": "LIST_NOTE", "at": m.added_at,
                              "list": {"list_id": l.list_id, "list_name": l.list_name}, "body": m.notes})

    items.sort(key=lambda i: i["at"] or datetime.min, reverse=True)
    return {"items": items[:limit]}


@router.post("/{prospect_id}/activities", status_code=201)
def log_activity(
    prospect_id: str,
    payload: ActivityWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """Add a note, or log a call, email or meeting (BR-CM-13)."""
    prospect = _get_contact(db, current_user, prospect_id)
    activity_type = payload.activity_type.upper()
    if activity_type not in ACTIVITY_TYPES:
        raise HTTPException(status_code=400, detail=f"activity_type must be one of {list(ACTIVITY_TYPES)}")
    if not clean_str(payload.body) and not clean_str(payload.subject):
        raise HTTPException(status_code=400, detail="Add a subject or some details")
    activity = ContactActivity(
        tenant_id=current_user.tenant_id,
        prospect_id=prospect.prospect_id,
        activity_type=activity_type,
        subject=clean_str(payload.subject),
        body=clean_str(payload.body),
        outcome=clean_str(payload.outcome),
        duration_minutes=payload.duration_minutes,
        occurred_at=_utc_naive(payload.occurred_at) or datetime.utcnow(),
        created_by=current_user.user_id,
    )
    db.add(activity)
    # A connected call, logged email or meeting moves a new lead's status on
    if activity_type in ("CALL", "EMAIL", "MEETING") and prospect.lead_status in (None, "NEW", "OPEN"):
        before = crm.snapshot(prospect, ("lead_status",))
        prospect.lead_status = "CONNECTED" if activity_type == "MEETING" or payload.outcome == "Connected" \
            else "ATTEMPTED_TO_CONTACT"
        crm.record_changes(db, current_user.tenant_id, "CONTACT", prospect.prospect_id, before,
                           crm.snapshot(prospect, ("lead_status",)), current_user.user_id, "SYSTEM")
    prospect.updated_at = func.now()
    db.commit()
    return _activity_dict(activity, {current_user.user_id: current_user})
