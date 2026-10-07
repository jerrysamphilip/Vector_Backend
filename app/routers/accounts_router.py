# app/routers/accounts_router.py
"""
Accounts (companies). Contacts link to an account; renaming an account updates the
company name on its contacts, and deleting one unlinks them (contacts are kept).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.user import User
from app.routers.contacts_router import _summaries, _user_name
from app.services.contact_service import backfill_accounts, can_manage_contacts, clean_str

router = APIRouter(prefix="/accounts", tags=["Accounts"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

EDITABLE = ("name", "domain", "website", "phone", "industry", "emp_band", "city", "state", "country",
            "description", "owner_id")


class AccountWrite(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = Field(None, max_length=500)
    phone: Optional[str] = Field(None, max_length=50)
    industry: Optional[str] = Field(None, max_length=100)
    emp_band: Optional[str] = Field(None, max_length=50)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    owner_id: Optional[str] = None


def _require_manager(user: User):
    if not can_manage_contacts(user):
        raise HTTPException(status_code=403, detail="Your account does not have the 'manage_prospects' permission.")


def _get_account(db: Session, user: User, account_id: str) -> Account:
    account = db.query(Account).filter(
        Account.account_id == account_id, Account.tenant_id == user.tenant_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _visible_contacts(db: Session, user: User):
    query = db.query(Prospect).filter(Prospect.tenant_id == user.tenant_id)
    if not can_manage_contacts(user):
        query = query.filter(Prospect.owner_id == user.user_id)
    return query


def _account_dict(a: Account, owner: Optional[User] = None, contact_count: int = 0) -> dict:
    return {
        "account_id": a.account_id, "name": a.name, "domain": a.domain, "website": a.website,
        "phone": a.phone, "industry": a.industry, "emp_band": a.emp_band, "city": a.city,
        "state": a.state, "country": a.country, "description": a.description,
        "owner_id": a.owner_id, "owner_name": _user_name(owner), "contact_count": contact_count,
        "created_at": a.created_at, "updated_at": a.updated_at,
    }


def _apply(db: Session, user: User, account: Account, data: dict):
    if "owner_id" in data and data["owner_id"]:
        if not db.query(User.user_id).filter(User.user_id == data["owner_id"],
                                             User.tenant_id == user.tenant_id).first():
            raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")
    for field in EDITABLE:
        if field in data:
            setattr(account, field, clean_str(data[field]) if field != "description" else data[field])


@router.get("")
def list_accounts(
    q: Optional[str] = None,
    owner: Optional[str] = Query(None, description="'me', 'unassigned' or a user id"),
    sort_by: str = Query("name", pattern="^(name|contacts|created_at|updated_at)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    counts = _visible_contacts(db, current_user).with_entities(
        Prospect.account_id, func.count(Prospect.prospect_id).label("n")
    ).filter(Prospect.account_id.isnot(None)).group_by(Prospect.account_id).subquery()

    query = db.query(Account, func.coalesce(counts.c.n, 0)).outerjoin(
        counts, counts.c.account_id == Account.account_id
    ).filter(Account.tenant_id == current_user.tenant_id)

    if not can_manage_contacts(current_user):
        # Accounts you own, or that hold at least one of your contacts
        query = query.filter((Account.owner_id == current_user.user_id) | (counts.c.n > 0))
    if owner == "me":
        query = query.filter(Account.owner_id == current_user.user_id)
    elif owner == "unassigned":
        query = query.filter(Account.owner_id.is_(None))
    elif owner:
        query = query.filter(Account.owner_id == owner)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(Account.name.ilike(term) | Account.domain.ilike(term) | Account.industry.ilike(term))

    total = query.count()
    column = {"name": Account.name, "contacts": func.coalesce(counts.c.n, 0),
              "created_at": Account.created_at, "updated_at": Account.updated_at}[sort_by]
    rows = query.order_by(column.asc() if sort_order == "asc" else column.desc(), Account.account_id).offset(
        (page - 1) * page_size).limit(page_size).all()

    owner_ids = {a.owner_id for a, _ in rows if a.owner_id}
    owners = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(owner_ids))} if owner_ids else {}
    return {"items": [_account_dict(a, owners.get(a.owner_id), n) for a, n in rows],
            "total": total, "page": page, "page_size": page_size}


@router.post("", status_code=201)
def create_account(payload: AccountWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    name = clean_str(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="Account name is required")
    if db.query(Account.account_id).filter(Account.tenant_id == current_user.tenant_id, Account.name == name).first():
        raise HTTPException(status_code=409, detail="An account with this name already exists.")
    data = payload.model_dump(exclude_unset=True)
    if data.get("owner_id") not in (None, current_user.user_id) and not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Only users who manage prospects can assign accounts to others")
    account = Account(tenant_id=current_user.tenant_id, owner_id=current_user.user_id)
    _apply(db, current_user, account, data)
    account.name = name
    db.add(account)
    db.commit()
    return _account_dict(account, current_user if account.owner_id == current_user.user_id else None)


@router.post("/backfill")
def backfill(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Create accounts from contacts' company names and link every contact that has no account."""
    _require_manager(current_user)
    linked = backfill_accounts(db, current_user.tenant_id)
    db.commit()
    return {"status": "ok", "linked_contacts": linked}


@router.get("/{account_id}")
def get_account(account_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    account = _get_account(db, current_user, account_id)
    contacts = _visible_contacts(db, current_user).filter(Prospect.account_id == account_id).order_by(
        Prospect.first_name, Prospect.last_name).limit(500).all()
    if not can_manage_contacts(current_user) and not contacts and account.owner_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="Account not found")

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
    result["stats"] = {"contacts": len(contacts), "emails_sent": emails_sent, "replies": replies}
    result["can_edit"] = can_manage_contacts(current_user) or account.owner_id == current_user.user_id
    return result


@router.patch("/{account_id}")
def update_account(account_id: str, payload: AccountWrite, db: Session = Depends(get_db),
                   current_user: User = Depends(tenant_user)):
    account = _get_account(db, current_user, account_id)
    if not (can_manage_contacts(current_user) or account.owner_id == current_user.user_id):
        raise HTTPException(status_code=403, detail="You can only edit accounts you own")
    data = payload.model_dump(exclude_unset=True)
    if "owner_id" in data and not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Only users who manage prospects can reassign accounts")
    if "name" in data:
        name = clean_str(data["name"])
        if not name:
            raise HTTPException(status_code=400, detail="Account name is required")
        if name != account.name:
            if db.query(Account.account_id).filter(Account.tenant_id == current_user.tenant_id,
                                                   Account.name == name,
                                                   Account.account_id != account_id).first():
                raise HTTPException(status_code=409, detail="An account with this name already exists.")
            db.query(Prospect).filter(Prospect.account_id == account_id).update(
                {Prospect.company_name: name}, synchronize_session=False)
        data["name"] = name
    _apply(db, current_user, account, data)
    db.commit()
    return get_account(account_id, db, current_user)


@router.delete("/{account_id}")
def delete_account(account_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Delete the account record. Its contacts are kept and simply unlinked."""
    _require_manager(current_user)
    account = _get_account(db, current_user, account_id)
    unlinked = db.query(Prospect).filter(Prospect.account_id == account_id).update(
        {Prospect.account_id: None}, synchronize_session=False)
    db.delete(account)
    db.commit()
    return {"status": "deleted", "account_id": account_id, "unlinked_contacts": unlinked}
