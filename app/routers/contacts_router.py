# app/routers/contacts_router.py
"""
Contact management API: one view over every contact in the tenant (not per upload list),
contact detail with a unified timeline, manual add, owners, tags, custom fields,
logged activities (notes, calls, meetings) and duplicate merging.

Users with the manage_prospects permission see every contact. Everyone else sees and
edits only the contacts they own, and can't reassign, bulk edit, merge or delete.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ACTIVITY_TYPES, ContactActivity
from app.models.contact_field import FIELD_TYPES, ContactFieldDefinition
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.user import User
from app.services.contact_service import (
    can_access_contact,
    can_manage_contacts,
    clean_str,
    delete_contacts,
    get_or_create_account,
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
    "created_at": (Prospect.created_at,),
    "updated_at": (Prospect.updated_at,),
}

EVENT_LABELS = {
    EmailEvent.EVENT_OPEN: "EMAIL_OPENED",
    EmailEvent.EVENT_CLICK: "EMAIL_CLICKED",
    EmailEvent.EVENT_BOUNCE: "EMAIL_BOUNCED",
    EmailEvent.EVENT_UNSUBSCRIBE: "UNSUBSCRIBED",
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
    prospect_ids: List[str] = Field(..., min_length=1, max_length=1000)
    action: str  # assign_owner / add_tags / remove_tags / set_account / delete
    owner_id: Optional[str] = None
    tags: Optional[List[str]] = None
    account_id: Optional[str] = None


class MergeRequest(BaseModel):
    primary_id: str
    duplicate_ids: List[str] = Field(..., min_length=1, max_length=20)


class FieldWrite(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    field_type: str = "TEXT"
    options: Optional[List[str]] = None
    sort_order: Optional[int] = None


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


def _get_contact(db: Session, user: User, prospect_id: str) -> Prospect:
    prospect = db.query(Prospect).filter(
        Prospect.prospect_id == prospect_id,
        Prospect.tenant_id == user.tenant_id,
    ).first()
    if not prospect or not can_access_contact(user, prospect):
        raise HTTPException(status_code=404, detail="Contact not found")
    return prospect


def _check_owner(db: Session, user: User, owner_id: Optional[str]):
    if owner_id and not db.query(User.user_id).filter(
        User.user_id == owner_id, User.tenant_id == user.tenant_id
    ).first():
        raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")


def _get_account(db: Session, user: User, account_id: str) -> Account:
    account = db.query(Account).filter(
        Account.account_id == account_id, Account.tenant_id == user.tenant_id
    ).first()
    if not account:
        raise HTTPException(status_code=400, detail="Account not found")
    return account


def _user_name(u: Optional[User]) -> Optional[str]:
    return f"{u.first_name} {u.last_name}".strip() if u else None


def _summaries(db: Session, prospects: List[Prospect]) -> List[dict]:
    """Serialise contacts for list views, batch-loading owners, accounts and last activity."""
    ids = [p.prospect_id for p in prospects]
    owner_ids = {p.owner_id for p in prospects if p.owner_id}
    account_ids = {p.account_id for p in prospects if p.account_id}
    owners = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(owner_ids))} if owner_ids else {}
    accounts = {a.account_id: a.name for a in db.query(Account.account_id, Account.name).filter(
        Account.account_id.in_(account_ids))} if account_ids else {}

    last_activity = {}
    if ids:
        for pid, ts in db.query(ContactActivity.prospect_id, func.max(ContactActivity.occurred_at)).filter(
                ContactActivity.prospect_id.in_(ids)).group_by(ContactActivity.prospect_id):
            last_activity[pid] = ts
        for pid, ts in db.query(EmailMessage.prospect_id, func.max(EmailMessage.sent_at)).filter(
                EmailMessage.prospect_id.in_(ids), EmailMessage.sent_at.isnot(None)).group_by(EmailMessage.prospect_id):
            if ts and (not last_activity.get(pid) or ts > last_activity[pid]):
                last_activity[pid] = ts

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
        "account_name": accounts.get(p.account_id),
        "owner_id": p.owner_id,
        "owner_name": _user_name(owners.get(p.owner_id)),
        "tags": p.tags or [],
        "industry": p.industry,
        "poc_city": p.poc_city,
        "poc_state": p.poc_state,
        "poc_country": p.poc_country,
        "consent_status": p.consent_status,
        "is_valid_email": p.is_valid_email,
        "last_activity_at": last_activity.get(p.prospect_id),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    } for p in prospects]


def _email_meta(email: str) -> tuple:
    from app.utils.email_utils import parse_email
    info = parse_email(email)
    if info:
        return info.get("email_type"), info.get("email_provider")
    return "PERSONAL", email.split("@")[-1] if "@" in email else None


def _clean_custom_fields(db: Session, tenant_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only defined keys; blank values remove the key."""
    defs = {d.field_key: d for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id)}
    cleaned = {}
    for key, value in (values or {}).items():
        definition = defs.get(key)
        if not definition:
            raise HTTPException(status_code=400, detail=f"Unknown custom field '{key}'")
        if value is None or (isinstance(value, str) and not value.strip()):
            cleaned[key] = None
            continue
        if definition.field_type == "NUMBER":
            try:
                value = float(value)
                value = int(value) if value.is_integer() else value
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"'{definition.label}' must be a number")
        elif definition.field_type == "SELECT" and definition.options and value not in definition.options:
            raise HTTPException(status_code=400, detail=f"'{definition.label}' must be one of {definition.options}")
        else:
            value = str(value).strip()[:1000]
        cleaned[key] = value
    return cleaned


def _apply_fields(db: Session, user: User, prospect: Prospect, data: dict):
    """Apply create/update fields, keeping company_name and account in step."""
    manager = can_manage_contacts(user)

    if "owner_id" in data:
        if not manager and data["owner_id"] != user.user_id:
            raise HTTPException(status_code=403, detail="Only users who manage prospects can reassign contacts")
        _check_owner(db, user, data["owner_id"])
        prospect.owner_id = data["owner_id"]

    for field in ("first_name", "last_name", "phone", "mobile_phone", "designation", "industry",
                  "emp_band", "linkedin_url", "poc_city", "poc_country"):
        if field in data:
            setattr(prospect, field, clean_str(data[field]))

    if "poc_state" in data:
        prospect.poc_state = clean_str(data["poc_state"])
        from app.utils.business_calendar import get_timezone_for_state
        prospect.timezone = get_timezone_for_state(prospect.poc_state) if prospect.poc_state else None

    if "tags" in data:
        prospect.tags = normalize_tags(data["tags"]) or None

    if "custom_fields" in data:
        merged = dict(prospect.custom_fields or {})
        for key, value in _clean_custom_fields(db, user.tenant_id, data["custom_fields"]).items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        prospect.custom_fields = merged or None

    # An explicit account wins; otherwise a changed company name relinks to that account.
    if "account_id" in data:
        if data["account_id"]:
            account = _get_account(db, user, data["account_id"])
            prospect.account_id = account.account_id
            prospect.company_name = account.name
        else:
            prospect.account_id = None
            if "company_name" in data:
                prospect.company_name = clean_str(data["company_name"])
    elif "company_name" in data:
        prospect.company_name = clean_str(data["company_name"])
        account = get_or_create_account(db, user.tenant_id, prospect.company_name,
                                        industry=prospect.industry, emp_band=prospect.emp_band)
        prospect.account_id = account.account_id if account else None


# =============================
# COLLECTION
# =============================

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
    sort_by: str = Query("created_at", pattern="^(name|email|company|created_at|updated_at)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    query = db.query(Prospect).filter(Prospect.tenant_id == current_user.tenant_id)

    if not can_manage_contacts(current_user):
        query = query.filter(Prospect.owner_id == current_user.user_id)
    elif owner == "me":
        query = query.filter(Prospect.owner_id == current_user.user_id)
    elif owner == "unassigned":
        query = query.filter(Prospect.owner_id.is_(None))
    elif owner:
        query = query.filter(Prospect.owner_id == owner)

    if q and q.strip():
        term = f"%{q.strip()}%"
        conditions = [
            Prospect.first_name.ilike(term),
            Prospect.last_name.ilike(term),
            func.concat_ws(" ", Prospect.first_name, Prospect.last_name).ilike(term),
            Prospect.email.ilike(term),
            Prospect.company_name.ilike(term),
            Prospect.designation.ilike(term),
        ]
        digits = phone_digits(q)
        if len(digits) >= 4:
            for column in (Prospect.phone, Prospect.mobile_phone):
                conditions.append(func.regexp_replace(column, "[^0-9]", "").like(f"%{digits}%"))
        query = query.filter(or_(*conditions))

    if account_id:
        query = query.filter(Prospect.account_id == account_id)
    if tag:
        query = query.filter(func.json_contains(Prospect.tags, func.json_quote(tag)) == 1)
    if list_id:
        query = query.filter(Prospect.prospect_id.in_(
            db.query(ProspectListMember.prospect_id).filter(ProspectListMember.list_id == list_id)))
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

    total = query.count()
    columns = SORT_COLUMNS[sort_by]
    order = [c.asc() if sort_order == "asc" else c.desc() for c in columns] + [Prospect.prospect_id.asc()]
    rows = query.order_by(*order).offset((page - 1) * page_size).limit(page_size).all()

    return {"items": _summaries(db, rows), "total": total, "page": page, "page_size": page_size}


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
        raise HTTPException(status_code=409, detail={
            "message": "A contact with this email already exists.",
            "prospect_id": existing.prospect_id if can_access_contact(current_user, existing) else None,
        })

    email_type, email_provider = _email_meta(email)
    unsubscribed = db.query(GlobalUnsubscribe).filter(
        GlobalUnsubscribe.tenant_id == current_user.tenant_id, GlobalUnsubscribe.email == email
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
    )
    data = payload.model_dump(exclude_unset=True, exclude={"email", "list_id"})
    data.setdefault("owner_id", current_user.user_id)
    _apply_fields(db, current_user, prospect, data)
    db.add(prospect)

    if payload.list_id:
        target = db.query(ProspectList).filter(
            ProspectList.list_id == payload.list_id, ProspectList.tenant_id == current_user.tenant_id
        ).first()
        if not target:
            raise HTTPException(status_code=400, detail="List not found")
        db.add(ProspectListMember(list_id=target.list_id, prospect_id=prospect.prospect_id, is_new_prospect=True))

    db.commit()
    return _contact_detail(db, current_user, prospect)


@router.get("/facets")
def contact_facets(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Values for the filter dropdowns: tags, countries, industries, lists and campaigns."""
    base = db.query(Prospect).filter(Prospect.tenant_id == current_user.tenant_id)
    if not can_manage_contacts(current_user):
        base = base.filter(Prospect.owner_id == current_user.user_id)

    tag_counts: Dict[str, list] = {}
    for (tags,) in base.with_entities(Prospect.tags).filter(Prospect.tags.isnot(None)):
        for tag in tags or []:
            entry = tag_counts.setdefault(tag.lower(), [tag, 0])
            entry[1] += 1

    def distinct(column):
        return sorted(v for (v,) in base.with_entities(column).filter(column.isnot(None), column != "").distinct())

    lists = db.query(ProspectList.list_id, ProspectList.list_name).filter(
        ProspectList.tenant_id == current_user.tenant_id).order_by(ProspectList.uploaded_at.desc()).all()
    campaigns = db.query(Campaign.campaign_id, Campaign.campaign_name).filter(
        Campaign.tenant_id == current_user.tenant_id).order_by(Campaign.created_at.desc()).all()

    return {
        "tags": [{"tag": t, "count": c} for t, c in sorted(tag_counts.values(), key=lambda x: (-x[1], x[0].lower()))],
        "countries": distinct(Prospect.poc_country),
        "industries": distinct(Prospect.industry),
        "lists": [{"list_id": i, "list_name": n} for i, n in lists],
        "campaigns": [{"campaign_id": i, "campaign_name": n} for i, n in campaigns],
    }


@router.get("/owners")
def contact_owners(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Active users in the workspace, for owner pickers."""
    users = db.query(User).filter(
        User.tenant_id == current_user.tenant_id, User.status == "ACTIVE"
    ).order_by(User.first_name, User.last_name).all()
    return [{"user_id": u.user_id, "name": _user_name(u), "email": u.email, "role": u.role} for u in users]


@router.post("/bulk")
def bulk_update(
    payload: BulkAction,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    _require_manager(current_user)
    prospects = db.query(Prospect).filter(
        Prospect.tenant_id == current_user.tenant_id, Prospect.prospect_id.in_(payload.prospect_ids)
    ).all()
    if not prospects:
        raise HTTPException(status_code=404, detail="No matching contacts")

    if payload.action == "assign_owner":
        _check_owner(db, current_user, payload.owner_id)
        for p in prospects:
            p.owner_id = payload.owner_id
    elif payload.action in ("add_tags", "remove_tags"):
        tags = normalize_tags(payload.tags)
        if not tags:
            raise HTTPException(status_code=400, detail="Give at least one tag")
        remove = {t.lower() for t in tags}
        for p in prospects:
            if payload.action == "add_tags":
                p.tags = normalize_tags(list(p.tags or []) + tags)
            else:
                p.tags = [t for t in (p.tags or []) if t.lower() not in remove] or None
    elif payload.action == "set_account":
        account = _get_account(db, current_user, payload.account_id) if payload.account_id else None
        for p in prospects:
            p.account_id = account.account_id if account else None
            if account:
                p.company_name = account.name
    elif payload.action == "delete":
        if current_user.role == "AGENT":
            raise HTTPException(status_code=403, detail="Agents cannot delete contacts")
        delete_contacts(db, [p.prospect_id for p in prospects])
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{payload.action}'")

    db.commit()
    return {"status": "ok", "action": payload.action, "updated": len(prospects)}


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
    Groups of likely duplicates: same first + last name at the same company, or the same
    phone number. (Emails are unique per workspace, so exact email matches can't occur.)
    """
    _require_manager(current_user)
    rows = db.query(
        Prospect.prospect_id, Prospect.first_name, Prospect.last_name, Prospect.company_name,
        Prospect.account_id, Prospect.phone, Prospect.mobile_phone,
    ).filter(Prospect.tenant_id == current_user.tenant_id).all()

    def norm(value):
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    buckets: Dict[tuple, set] = {}
    for r in rows:
        first, last = norm(r.first_name), norm(r.last_name)
        company = r.account_id or norm(r.company_name)
        if first and last and company:
            buckets.setdefault(("name", first, last, company), set()).add(r.prospect_id)
        for phone in {phone_digits(r.phone), phone_digits(r.mobile_phone)}:
            if len(phone) >= 7:
                buckets.setdefault(("phone", phone[-10:]), set()).add(r.prospect_id)

    # Merge overlapping buckets into groups
    groups: List[dict] = []
    seen: Dict[str, int] = {}
    for key, ids in buckets.items():
        if len(ids) < 2:
            continue
        reason = "Same name and company" if key[0] == "name" else "Same phone number"
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
    duplicates = db.query(Prospect).filter(
        Prospect.tenant_id == current_user.tenant_id, Prospect.prospect_id.in_(duplicate_ids)
    ).all()
    if len(duplicates) != len(duplicate_ids):
        raise HTTPException(status_code=404, detail="One or more contacts to merge were not found")

    moved = merge_contacts(db, primary, duplicates, current_user.user_id)
    db.commit()
    db.refresh(primary)
    return {"status": "merged", "merged": len(duplicates), "moved": moved,
            "contact": _contact_detail(db, current_user, primary)}


# =============================
# CUSTOM FIELDS
# =============================

def _field_dict(d: ContactFieldDefinition) -> dict:
    return {"field_id": d.field_id, "field_key": d.field_key, "label": d.label,
            "field_type": d.field_type, "options": d.options or [], "sort_order": d.sort_order or 0}


def _validate_field(payload: FieldWrite):
    field_type = payload.field_type.upper()
    if field_type not in FIELD_TYPES:
        raise HTTPException(status_code=400, detail=f"field_type must be one of {list(FIELD_TYPES)}")
    options = normalize_tags(payload.options) if field_type == "SELECT" else None
    if field_type == "SELECT" and not options:
        raise HTTPException(status_code=400, detail="A choice field needs at least one option")
    return field_type, options


@router.get("/fields")
def list_fields(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    defs = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == current_user.tenant_id
    ).order_by(ContactFieldDefinition.sort_order, ContactFieldDefinition.created_at).all()
    return [_field_dict(d) for d in defs]


@router.post("/fields", status_code=201)
def create_field(payload: FieldWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    field_type, options = _validate_field(payload)
    base_key = re.sub(r"[^a-z0-9]+", "_", payload.label.lower()).strip("_")[:50] or "field"
    key, n = base_key, 2
    while db.query(ContactFieldDefinition.field_id).filter(
            ContactFieldDefinition.tenant_id == current_user.tenant_id,
            ContactFieldDefinition.field_key == key).first():
        key, n = f"{base_key}_{n}", n + 1
    count = db.query(func.count(ContactFieldDefinition.field_id)).filter(
        ContactFieldDefinition.tenant_id == current_user.tenant_id).scalar()
    definition = ContactFieldDefinition(
        tenant_id=current_user.tenant_id, field_key=key, label=payload.label.strip(),
        field_type=field_type, options=options,
        sort_order=payload.sort_order if payload.sort_order is not None else count,
    )
    db.add(definition)
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
        raise HTTPException(status_code=404, detail="Field not found")
    field_type, options = _validate_field(payload)
    definition.label = payload.label.strip()
    definition.field_type = field_type
    definition.options = options
    if payload.sort_order is not None:
        definition.sort_order = payload.sort_order
    db.commit()
    return _field_dict(definition)


@router.delete("/fields/{field_id}")
def delete_field(field_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Removes the field definition. Stored values stay on contacts but are no longer shown."""
    _require_manager(current_user)
    deleted = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.field_id == field_id,
        ContactFieldDefinition.tenant_id == current_user.tenant_id).delete()
    if not deleted:
        raise HTTPException(status_code=404, detail="Field not found")
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
        "emp_band": p.emp_band,
        "linkedin_url": p.linkedin_url,
        "timezone": p.timezone,
        "consent_source": p.consent_source,
        "consent_timestamp": p.consent_timestamp,
        "custom_fields": p.custom_fields or {},
    })

    account = db.query(Account).filter(Account.account_id == p.account_id).first() if p.account_id else None
    detail["account"] = {
        "account_id": account.account_id, "name": account.name, "domain": account.domain,
        "website": account.website, "industry": account.industry, "emp_band": account.emp_band,
    } if account else None

    detail["lists"] = [{
        "list_id": l.list_id, "list_name": l.list_name, "added_at": m.added_at, "notes": m.notes,
    } for m, l in db.query(ProspectListMember, ProspectList).join(
        ProspectList, ProspectList.list_id == ProspectListMember.list_id
    ).filter(ProspectListMember.prospect_id == p.prospect_id).order_by(ProspectListMember.added_at.desc())]

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
    detail["stats"] = {
        "emails_sent": sent[0], "last_emailed_at": sent[1],
        "replies": received[0], "last_reply_at": received[1],
        "emails_opened": opens or 0,
        "notes": activity_counts.get("NOTE", 0),
        "calls": activity_counts.get("CALL", 0),
        "meetings": activity_counts.get("MEETING", 0),
    }
    detail["can_edit_owner"] = can_manage_contacts(user)
    detail["can_delete"] = can_manage_contacts(user) and user.role != "AGENT"
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
    db.commit()
    db.refresh(prospect)
    return _contact_detail(db, current_user, prospect)


@router.delete("/{prospect_id}")
def delete_contact(prospect_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    if current_user.role == "AGENT":
        raise HTTPException(status_code=403, detail="Agents cannot delete contacts")
    prospect = _get_contact(db, current_user, prospect_id)
    delete_contacts(db, [prospect.prospect_id])
    db.commit()
    return {"status": "deleted", "prospect_id": prospect_id}


@router.get("/{prospect_id}/timeline")
def contact_timeline(
    prospect_id: str,
    types: Optional[str] = Query(None, description="Comma-separated: NOTE,CALL,MEETING,EMAIL,CAMPAIGN,LIST"),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """Everything that happened with this contact, newest first."""
    prospect = _get_contact(db, current_user, prospect_id)
    wanted = {t.strip().upper() for t in types.split(",")} if types else None

    def want(kind):
        return wanted is None or kind in wanted

    items = []

    activities = db.query(ContactActivity).filter(ContactActivity.prospect_id == prospect.prospect_id).order_by(
        ContactActivity.occurred_at.desc()).limit(limit).all()
    author_ids = {a.created_by for a in activities if a.created_by}
    authors = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(author_ids))} if author_ids else {}
    for a in activities:
        if want(a.activity_type):
            items.append({"kind": a.activity_type, "at": a.occurred_at, "activity": _activity_dict(a, authors),
                          "can_edit": a.created_by == current_user.user_id or can_manage_contacts(current_user)})

    if want("EMAIL"):
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
                    "snippet": (m.body_text or "")[:400], "status": m.status,
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
        ).order_by(EmailEvent.event_time.desc()).limit(limit).all()
        first_open = {}
        for e, subject in events:
            # One "opened" entry per email (the first open), not one per pixel load
            if e.event_type == EmailEvent.EVENT_OPEN:
                if e.message_id in first_open and first_open[e.message_id]["at"] <= e.event_time:
                    continue
                entry = {"kind": "EMAIL_OPENED", "at": e.event_time, "email": {"message_id": e.message_id, "subject": subject}}
                if e.message_id in first_open:
                    items.remove(first_open[e.message_id])
                first_open[e.message_id] = entry
                items.append(entry)
            else:
                items.append({"kind": EVENT_LABELS[e.event_type], "at": e.event_time,
                              "email": {"message_id": e.message_id, "subject": subject}})

    if want("CAMPAIGN"):
        for e, c in db.query(CampaignProspect, Campaign).join(
                Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
        ).filter(CampaignProspect.prospect_id == prospect.prospect_id):
            items.append({"kind": "CAMPAIGN_ENROLLED", "at": e.enrolled_at,
                          "campaign": {"campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "status": e.status}})

    if want("LIST") or want("NOTE"):
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
    prospect.updated_at = func.now()
    db.commit()
    return _activity_dict(activity, {current_user.user_id: current_user})
