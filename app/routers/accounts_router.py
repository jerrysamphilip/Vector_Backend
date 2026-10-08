# app/routers/accounts_router.py
"""
Companies (BR-CM-03/04). Each company has a unique domain per workspace; contacts are
associated automatically from their email domain, or by company name for personal email
addresses. Renaming a company updates the company name on its contacts. Deleting is a
soft delete: the company is hidden (contacts keep their link) and can be restored for
90 days.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.user import User
from app.routers.contacts_router import _history_items, _summaries, _user_name
from app.services import crm
from app.services.contact_service import can_manage_contacts, can_see_owner, clean_str, scope, sees_everything, visible_user_ids
from app.services.sales_settings import can_see_amounts, masked_for

router = APIRouter(prefix="/accounts", tags=["Companies"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

EDITABLE = ("name", "domain", "website", "phone", "industry", "emp_band", "street", "city", "state",
            "postal_code", "country", "description", "owner_id", "annual_revenue", "lifecycle_stage")


class AccountWrite(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = Field(None, max_length=500)
    phone: Optional[str] = Field(None, max_length=50)
    industry: Optional[str] = Field(None, max_length=100)
    emp_band: Optional[str] = Field(None, max_length=50)
    street: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    owner_id: Optional[str] = None
    annual_revenue: Optional[str] = None
    lifecycle_stage: Optional[str] = None


def _require_manager(user: User):
    if not can_manage_contacts(user):
        raise HTTPException(status_code=403, detail="Your account does not have the 'manage_prospects' permission.")


def _get_account(db: Session, user: User, account_id: str) -> Account:
    account = db.query(Account).filter(
        Account.account_id == account_id, Account.tenant_id == user.tenant_id, Account.deleted_at.is_(None)).first()
    if not account:
        raise HTTPException(status_code=404, detail="Company not found")
    return account


def _can_see_account(db: Session, user: User, account: Account) -> bool:
    """Companies owned by the user's team, or holding at least one contact they can see (BR-SH-02)."""
    if sees_everything(user) or (account.owner_id and can_see_owner(db, user, account.owner_id)):
        return True
    return db.query(Prospect.prospect_id).filter(
        Prospect.account_id == account.account_id, Prospect.deleted_at.is_(None),
        Prospect.owner_id.in_(visible_user_ids(db, user) or {"-"})).first() is not None


def _can_edit_account(db: Session, user: User, account: Account) -> bool:
    if not _can_see_account(db, user, account):
        return False
    return (can_manage_contacts(user) and (sees_everything(user) or account.owner_id is None
                                           or can_see_owner(db, user, account.owner_id))) or (
        account.owner_id is not None and can_see_owner(db, user, account.owner_id))


def _visible_contacts(db: Session, user: User):
    query = db.query(Prospect).filter(Prospect.tenant_id == user.tenant_id, Prospect.deleted_at.is_(None))
    return scope(query, db, user, Prospect.owner_id)


def _account_dict(a: Account, owner: Optional[User] = None, contact_count: int = 0) -> dict:
    return {
        "account_id": a.account_id, "name": a.name, "domain": a.domain, "website": a.website,
        "phone": a.phone, "industry": a.industry, "emp_band": a.emp_band, "street": a.street,
        "city": a.city, "state": a.state, "postal_code": a.postal_code, "country": a.country,
        "description": a.description, "annual_revenue": float(a.annual_revenue) if a.annual_revenue is not None else None,
        "lifecycle_stage": a.lifecycle_stage,
        "owner_id": a.owner_id, "owner_name": _user_name(owner), "contact_count": contact_count,
        "created_at": a.created_at, "updated_at": a.updated_at,
    }


def _apply(db: Session, user: User, account: Account, data: dict):
    if "owner_id" in data and data["owner_id"]:
        if not db.query(User.user_id).filter(User.user_id == data["owner_id"],
                                             User.tenant_id == user.tenant_id).first():
            raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")
        if not can_see_owner(db, user, data["owner_id"]):
            raise HTTPException(status_code=403, detail="You can only assign records to yourself or your team")
    if "annual_revenue" in data and not can_see_amounts(db, user):
        if data.pop("annual_revenue") not in (None, ""):  # a blank from a masked form leaves it as is
            raise HTTPException(status_code=403, detail="Your role cannot see or change amounts")
    if "domain" in data:
        domain = crm.clean_domain(data["domain"])
        if domain and db.query(Account.account_id).filter(
                Account.tenant_id == user.tenant_id, Account.domain == domain,
                Account.account_id != account.account_id).first():
            raise HTTPException(status_code=409, detail=f"Another company already has the domain {domain}.")
        data["domain"] = domain
    if "annual_revenue" in data and data["annual_revenue"] not in (None, ""):
        try:
            data["annual_revenue"] = Decimal(str(data["annual_revenue"]).replace(",", "").strip())
        except InvalidOperation:
            raise HTTPException(status_code=400, detail="Annual revenue must be a number")
    if data.get("lifecycle_stage") and data["lifecycle_stage"] not in crm.LIFECYCLE_STAGES:
        raise HTTPException(status_code=400, detail=f"lifecycle_stage must be one of {list(crm.LIFECYCLE_STAGES)}")
    if "lifecycle_stage" in data and not crm.lifecycle_move_allowed(account.lifecycle_stage, data["lifecycle_stage"], user):
        raise HTTPException(status_code=400, detail="Lifecycle stage moves forward only. Ask an admin to change it.")
    for field in EDITABLE:
        if field in data:
            value = data[field]
            setattr(account, field, clean_str(value) if isinstance(value, str) and field != "description" else (value or None))


@router.get("")
def list_accounts(
    q: Optional[str] = None,
    owner: Optional[str] = Query(None, description="'me', 'unassigned' or a user id"),
    lifecycle_stage: Optional[str] = None,
    industry: Optional[str] = None,
    sort_by: str = Query("name", pattern="^(name|contacts|annual_revenue|created_at|updated_at)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    if sort_by == "annual_revenue" and not can_see_amounts(db, current_user):
        sort_by = "name"  # sorting by a hidden amount would reveal it (BR-SF-12)
    counts = _visible_contacts(db, current_user).with_entities(
        Prospect.account_id, func.count(Prospect.prospect_id).label("n")
    ).filter(Prospect.account_id.isnot(None)).group_by(Prospect.account_id).subquery()

    query = db.query(Account, func.coalesce(counts.c.n, 0)).outerjoin(
        counts, counts.c.account_id == Account.account_id
    ).filter(Account.tenant_id == current_user.tenant_id, Account.deleted_at.is_(None))

    visible = visible_user_ids(db, current_user)
    if visible is not None:
        # Companies owned by you or your team, or holding at least one contact you can see
        query = query.filter(Account.owner_id.in_(visible) | (counts.c.n > 0))
    if owner == "me":
        query = query.filter(Account.owner_id == current_user.user_id)
    elif owner == "unassigned":
        query = query.filter(Account.owner_id.is_(None))
    elif owner:
        query = query.filter(Account.owner_id == owner)
    if lifecycle_stage:
        query = query.filter(Account.lifecycle_stage == lifecycle_stage)
    if industry:
        query = query.filter(Account.industry == industry)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(or_(Account.name.ilike(term), Account.domain.ilike(term), Account.industry.ilike(term)))

    total = query.count()
    column = {"name": Account.name, "contacts": func.coalesce(counts.c.n, 0), "annual_revenue": Account.annual_revenue,
              "created_at": Account.created_at, "updated_at": Account.updated_at}[sort_by]
    rows = query.order_by(column.asc() if sort_order == "asc" else column.desc(), Account.account_id).offset(
        (page - 1) * page_size).limit(page_size).all()

    owner_ids = {a.owner_id for a, _ in rows if a.owner_id}
    owners = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(owner_ids))} if owner_ids else {}
    return masked_for(db, current_user, {"items": [_account_dict(a, owners.get(a.owner_id), n) for a, n in rows],
                                         "total": total, "page": page, "page_size": page_size})


@router.post("", status_code=201)
def create_account(payload: AccountWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    data = payload.model_dump(exclude_unset=True)
    name = clean_str(data.get("name"))
    domain = crm.clean_domain(data.get("domain") or data.get("website"))
    if not name and domain:
        name = crm._name_from_domain(domain)
    if not name:
        raise HTTPException(status_code=400, detail="Company name or domain is required")
    if db.query(Account.account_id).filter(Account.tenant_id == current_user.tenant_id, Account.name == name).first():
        raise HTTPException(status_code=409, detail="A company with this name already exists.")
    if data.get("owner_id") not in (None, current_user.user_id) and not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Only users who manage prospects can assign companies to others")
    account = Account(tenant_id=current_user.tenant_id, owner_id=current_user.user_id, lifecycle_stage="LEAD")
    data["name"] = name
    if domain:
        data["domain"] = domain
    _apply(db, current_user, account, data)
    db.add(account)
    db.flush()
    db.add(PropertyChange(tenant_id=current_user.tenant_id, object_type="COMPANY", object_id=account.account_id,
                          field="created", new_value="Company created", source="UI", changed_by=current_user.user_id))
    db.commit()
    return masked_for(db, current_user,
                      _account_dict(account, current_user if account.owner_id == current_user.user_id else None))


@router.post("/backfill")
def backfill(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """
    Associate every contact without a company (BR-CM-04): by email domain, creating the
    company if needed, or by company name for personal email addresses.
    """
    _require_manager(current_user)
    linked = 0
    while True:
        batch = db.query(Prospect).filter(
            Prospect.tenant_id == current_user.tenant_id, Prospect.deleted_at.is_(None),
            Prospect.account_id.is_(None)).limit(500).all()
        progress = 0
        for p in batch:
            account = crm.resolve_company(db, current_user.tenant_id, p.email, p.company_name,
                                          industry=p.industry, emp_band=p.emp_band)
            if account:
                p.account_id = account.account_id
                p.company_name = p.company_name or account.name
                linked += 1
                progress += 1
        db.commit()
        if len(batch) < 500 or progress == 0:
            break
    return {"status": "ok", "linked_contacts": linked}


@router.get("/deleted")
def deleted_accounts(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    cutoff = datetime.utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS)
    rows = db.query(Account).filter(Account.tenant_id == current_user.tenant_id, Account.deleted_at.isnot(None),
                                    Account.deleted_at > cutoff).order_by(Account.deleted_at.desc()).all()
    rows = [a for a in rows if _can_see_account(db, current_user, a)]
    return masked_for(db, current_user, {"items": [{**_account_dict(a), "deleted_at": a.deleted_at,
                       "purge_at": a.deleted_at + timedelta(days=crm.RESTORE_WINDOW_DAYS)} for a in rows]})


@router.post("/{account_id}/restore")
def restore_account(account_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    account = db.query(Account).filter(Account.account_id == account_id, Account.tenant_id == current_user.tenant_id,
                                       Account.deleted_at.isnot(None)).first()
    if not account or not _can_see_account(db, current_user, account):
        raise HTTPException(status_code=404, detail="Deleted company not found")
    if account.domain and db.query(Account.account_id).filter(
            Account.tenant_id == current_user.tenant_id, Account.domain == account.domain,
            Account.deleted_at.is_(None), Account.account_id != account_id).first():
        raise HTTPException(status_code=409, detail=f"Another company now uses {account.domain}")
    account.deleted_at, account.deleted_by = None, None
    db.add(PropertyChange(tenant_id=current_user.tenant_id, object_type="COMPANY", object_id=account_id,
                          field="deleted", old_value="deleted", source="UI", changed_by=current_user.user_id))
    db.commit()
    return {"status": "restored", "account_id": account_id}


@router.get("/{account_id}")
def get_account(account_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    account = _get_account(db, current_user, account_id)
    contacts = _visible_contacts(db, current_user).filter(Prospect.account_id == account_id).order_by(
        Prospect.first_name, Prospect.last_name).limit(500).all()
    if not contacts and not _can_see_account(db, current_user, account):
        raise HTTPException(status_code=404, detail="Company not found")

    contact_ids = [c.prospect_id for c in contacts]
    emails_sent = replies = 0
    if contact_ids:
        emails_sent = db.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.prospect_id.in_(contact_ids), EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.isnot(None)).scalar()
        replies = db.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.prospect_id.in_(contact_ids), EmailMessage.direction == "INBOUND").scalar()

    owner = db.query(User).filter(User.user_id == account.owner_id).first() if account.owner_id else None
    result = _account_dict(account, owner, len(contacts))
    result["contacts"] = _summaries(db, contacts)
    result["stats"] = {"contacts": len(contacts), "emails_sent": emails_sent, "replies": replies,
                       "open_tasks": db.query(func.count(CrmTask.task_id)).filter(
                           CrmTask.account_id == account_id, CrmTask.status == "OPEN").scalar() or 0}
    result["can_edit"] = _can_edit_account(db, current_user, account)
    result["can_delete"] = (can_manage_contacts(current_user) and current_user.role != "AGENT"
                            and result["can_edit"])
    result["history"] = _history_items(db, current_user, db.query(PropertyChange).filter(
        PropertyChange.object_type == "COMPANY", PropertyChange.object_id == account_id
    ).order_by(PropertyChange.changed_at.desc()).limit(200).all())
    return masked_for(db, current_user, result)


@router.patch("/{account_id}")
def update_account(account_id: str, payload: AccountWrite, db: Session = Depends(get_db),
                   current_user: User = Depends(tenant_user)):
    account = _get_account(db, current_user, account_id)
    if not _can_see_account(db, current_user, account):
        raise HTTPException(status_code=404, detail="Company not found")
    if not _can_edit_account(db, current_user, account):
        raise HTTPException(status_code=403, detail="You can only edit companies you own")
    data = payload.model_dump(exclude_unset=True)
    if "owner_id" in data and not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Only users who manage prospects can reassign companies")
    before = crm.snapshot(account, EDITABLE)
    if "name" in data:
        name = clean_str(data["name"])
        if not name:
            raise HTTPException(status_code=400, detail="Company name is required")
        if name != account.name:
            if db.query(Account.account_id).filter(Account.tenant_id == current_user.tenant_id,
                                                   Account.name == name,
                                                   Account.account_id != account_id).first():
                raise HTTPException(status_code=409, detail="A company with this name already exists.")
            db.query(Prospect).filter(Prospect.account_id == account_id).update(
                {Prospect.company_name: name}, synchronize_session=False)
        data["name"] = name
    _apply(db, current_user, account, data)
    crm.record_changes(db, current_user.tenant_id, "COMPANY", account_id, before,
                       crm.snapshot(account, EDITABLE), current_user.user_id, "UI")
    db.commit()
    return get_account(account_id, db, current_user)


@router.delete("/{account_id}")
def delete_account(account_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Soft delete: hidden and restorable for 90 days; contacts are kept."""
    _require_manager(current_user)
    if current_user.role == "AGENT":
        raise HTTPException(status_code=403, detail="Agents cannot delete companies")
    account = _get_account(db, current_user, account_id)
    if not _can_see_account(db, current_user, account):
        raise HTTPException(status_code=404, detail="Company not found")
    if not _can_edit_account(db, current_user, account):
        raise HTTPException(status_code=403, detail="You can only delete companies you or your team own")
    account.deleted_at, account.deleted_by = datetime.utcnow(), current_user.user_id
    db.add(PropertyChange(tenant_id=current_user.tenant_id, object_type="COMPANY", object_id=account_id,
                          field="deleted", new_value="deleted", source="UI", changed_by=current_user.user_id))
    db.commit()
    return {"status": "deleted", "account_id": account_id, "restorable_days": crm.RESTORE_WINDOW_DAYS}
