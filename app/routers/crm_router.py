# app/routers/crm_router.py
"""Saved views (BR-CM-19) and global search across contacts and companies (BR-CM-21)."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.crm import SavedView
from app.models.prospect import Prospect
from app.models.user import User
from app.services import crm
from app.services.contact_service import can_manage_contacts, clean_str, phone_digits

router = APIRouter(tags=["CRM"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


class ViewWrite(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    object_type: Optional[str] = None
    shared: Optional[bool] = None
    filters: Optional[Any] = None
    columns: Optional[list] = None
    sort: Optional[dict] = None


def _view_dict(v: SavedView, user: User) -> dict:
    return {"view_id": v.view_id, "name": v.name, "object_type": v.object_type, "shared": v.shared,
            "filters": v.filters, "columns": v.columns, "sort": v.sort, "owner_id": v.owner_id,
            "is_mine": v.owner_id == user.user_id,
            "can_edit": v.owner_id == user.user_id or (v.shared and user.role in ("SUPER_ADMIN", "ADMIN"))}


def _get_view(db: Session, user: User, view_id: str, editing: bool = False) -> SavedView:
    view = db.query(SavedView).filter(SavedView.view_id == view_id, SavedView.tenant_id == user.tenant_id).first()
    if not view or (not view.shared and view.owner_id != user.user_id):
        raise HTTPException(status_code=404, detail="View not found")
    if editing and not _view_dict(view, user)["can_edit"]:
        raise HTTPException(status_code=403, detail="Only the view's owner (or an admin, for shared views) can change it")
    return view


def _check_filters(db: Session, user: User, filters):
    if filters:
        try:
            crm.filter_clause(db, filters, user)
        except crm.FilterError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@router.get("/views")
def list_views(object_type: str = Query("CONTACT", pattern="^(CONTACT|COMPANY)$"),
               db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    rows = db.query(SavedView).filter(
        SavedView.tenant_id == current_user.tenant_id, SavedView.object_type == object_type,
        or_(SavedView.owner_id == current_user.user_id, SavedView.shared.is_(True)),
    ).order_by(SavedView.shared, SavedView.name).all()
    return [_view_dict(v, current_user) for v in rows]


@router.post("/views", status_code=201)
def create_view(payload: ViewWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    name = clean_str(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="Give the view a name")
    if payload.shared and not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Only admins can share views")
    object_type = (payload.object_type or "CONTACT").upper()
    if object_type == "CONTACT":
        _check_filters(db, current_user, payload.filters)
    view = SavedView(tenant_id=current_user.tenant_id, object_type=object_type, name=name,
                     owner_id=current_user.user_id, shared=bool(payload.shared), filters=payload.filters,
                     columns=payload.columns, sort=payload.sort)
    db.add(view)
    db.commit()
    return _view_dict(view, current_user)


@router.patch("/views/{view_id}")
def update_view(view_id: str, payload: ViewWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    view = _get_view(db, current_user, view_id, editing=True)
    data = payload.model_dump(exclude_unset=True)
    if "shared" in data and data["shared"] and not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Only admins can share views")
    if "filters" in data and view.object_type == "CONTACT":
        _check_filters(db, current_user, data["filters"])
    if "name" in data and not clean_str(data["name"]):
        raise HTTPException(status_code=400, detail="Give the view a name")
    for field in ("name", "shared", "filters", "columns", "sort"):
        if field in data:
            setattr(view, field, clean_str(data[field]) if field == "name" else data[field])
    db.commit()
    return _view_dict(view, current_user)


@router.delete("/views/{view_id}")
def delete_view(view_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    view = _get_view(db, current_user, view_id, editing=True)
    db.delete(view)
    db.commit()
    return {"status": "deleted", "view_id": view_id}


@router.get("/search")
def global_search(q: str = Query(..., min_length=2, max_length=100), limit: int = Query(8, ge=1, le=25),
                  db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Contacts by name, email, phone, company or domain, and companies by name or domain."""
    term = f"%{q.strip()}%"
    contacts = db.query(Prospect).filter(Prospect.tenant_id == current_user.tenant_id, Prospect.deleted_at.is_(None))
    if not can_manage_contacts(current_user):
        contacts = contacts.filter(Prospect.owner_id == current_user.user_id)
    conditions = [Prospect.email.ilike(term), Prospect.first_name.ilike(term), Prospect.last_name.ilike(term),
                  func.concat_ws(" ", Prospect.first_name, Prospect.last_name).ilike(term),
                  Prospect.company_name.ilike(term)]
    digits = phone_digits(q)
    if len(digits) >= 4:
        conditions += [func.regexp_replace(Prospect.phone, "[^0-9]", "").like(f"%{digits}%"),
                       func.regexp_replace(Prospect.mobile_phone, "[^0-9]", "").like(f"%{digits}%")]
    contact_rows = contacts.filter(or_(*conditions)).order_by(Prospect.updated_at.desc()).limit(limit).all()

    companies = db.query(Account).filter(Account.tenant_id == current_user.tenant_id, Account.deleted_at.is_(None),
                                         or_(Account.name.ilike(term), Account.domain.ilike(term)))
    company_rows = companies.order_by(Account.name).limit(max(3, limit // 2)).all()
    return {
        "contacts": [{"prospect_id": p.prospect_id, "full_name": p.full_name, "email": p.email, "phone": p.phone,
                      "company_name": p.company_name, "lifecycle_stage": p.lifecycle_stage} for p in contact_rows],
        "companies": [{"account_id": a.account_id, "name": a.name, "domain": a.domain, "industry": a.industry}
                      for a in company_rows],
    }
