# app/routers/lists_router.py
"""
Contact lists for the CRM (BR-CM-22/23/24).

- STATIC lists hold a fixed set of contacts, added by hand, by bulk action or by import.
- ACTIVE lists are a saved filter: membership is evaluated whenever the list is read or
  used as a campaign audience, so it is always current.

Upload lists from the Prospects page are static lists too. Deleting a list here never
deletes contacts (unlike the Prospects page's upload-list delete).
"""

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.user import User
from app.services import crm
from app.services.contact_service import can_manage_contacts, clean_str

router = APIRouter(prefix="/lists", tags=["Lists"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


class ListWrite(BaseModel):
    list_name: Optional[str] = Field(None, max_length=255)
    list_type: Optional[str] = None  # STATIC / ACTIVE (create only)
    filters: Optional[Any] = None
    description: Optional[str] = None


class Members(BaseModel):
    prospect_ids: List[str] = Field(..., min_length=1, max_length=10000)


class FilterPreview(BaseModel):
    filters: Any = None


def _require_manager(user: User):
    if not can_manage_contacts(user):
        raise HTTPException(status_code=403, detail="Your account does not have the 'manage_prospects' permission.")


def _get_list(db: Session, user: User, list_id: str) -> ProspectList:
    plist = db.query(ProspectList).filter(ProspectList.list_id == list_id,
                                          ProspectList.tenant_id == user.tenant_id).first()
    if not plist:
        raise HTTPException(status_code=404, detail="List not found")
    return plist


def _count(db: Session, user: User, plist: ProspectList) -> int:
    try:
        member_ids = crm.list_member_ids(db, plist, user)
    except crm.FilterError:
        return 0
    query = db.query(func.count(Prospect.prospect_id)).filter(
        Prospect.prospect_id.in_(member_ids), Prospect.deleted_at.is_(None))
    if not can_manage_contacts(user):
        query = query.filter(Prospect.owner_id == user.user_id)
    return query.scalar() or 0


def _list_dict(db: Session, user: User, plist: ProspectList, owners: dict = None) -> dict:
    owners = owners or {}
    return {
        "list_id": plist.list_id, "list_name": plist.list_name, "list_type": plist.list_type or "STATIC",
        "source_type": plist.source_type, "filters": plist.filters, "description": plist.description,
        "created_by": plist.uploaded_by, "created_by_name": owners.get(plist.uploaded_by),
        "created_at": plist.uploaded_at, "member_count": _count(db, user, plist),
    }


def _validate_filters(db: Session, user: User, filters):
    if not filters or not isinstance(filters, dict) or not filters.get("conditions"):
        raise HTTPException(status_code=400, detail="An active list needs at least one filter condition")
    try:
        crm.filter_clause(db, filters, user)
    except crm.FilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("")
def list_lists(
    list_type: Optional[str] = Query(None, pattern="^(STATIC|ACTIVE)$"),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    query = db.query(ProspectList).filter(ProspectList.tenant_id == current_user.tenant_id)
    if list_type:
        query = query.filter(ProspectList.list_type == list_type)
    if q and q.strip():
        query = query.filter(ProspectList.list_name.ilike(f"%{q.strip()}%"))
    rows = query.order_by(ProspectList.uploaded_at.desc()).limit(500).all()
    owners = {u.user_id: f"{u.first_name} {u.last_name}".strip()
              for u in db.query(User).filter(User.user_id.in_({r.uploaded_by for r in rows}))}
    return {"items": [_list_dict(db, current_user, r, owners) for r in rows]}


@router.post("/preview")
def preview_filters(payload: FilterPreview, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """How many contacts a filter matches right now (for building active lists)."""
    try:
        clause = crm.filter_clause(db, payload.filters, current_user)
    except crm.FilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    query = db.query(Prospect).filter(Prospect.tenant_id == current_user.tenant_id, Prospect.deleted_at.is_(None))
    if not can_manage_contacts(current_user):
        query = query.filter(Prospect.owner_id == current_user.user_id)
    if clause is not None:
        query = query.filter(clause)
    sample = query.order_by(Prospect.created_at.desc()).limit(5).all()
    return {"count": query.count(), "sample": [{"prospect_id": p.prospect_id, "full_name": p.full_name,
                                                "email": p.email} for p in sample]}


@router.post("", status_code=201)
def create_list(payload: ListWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    name = clean_str(payload.list_name)
    if not name:
        raise HTTPException(status_code=400, detail="Give the list a name")
    list_type = (payload.list_type or "STATIC").upper()
    if list_type not in ("STATIC", "ACTIVE"):
        raise HTTPException(status_code=400, detail="list_type must be STATIC or ACTIVE")
    if list_type == "ACTIVE":
        _validate_filters(db, current_user, payload.filters)
    plist = ProspectList(tenant_id=current_user.tenant_id, list_name=name, list_type=list_type,
                         source_type="MANUAL", filters=payload.filters if list_type == "ACTIVE" else None,
                         description=clean_str(payload.description), uploaded_by=current_user.user_id)
    db.add(plist)
    db.commit()
    return _list_dict(db, current_user, plist)


@router.get("/{list_id}")
def get_list(list_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    return _list_dict(db, current_user, _get_list(db, current_user, list_id))


@router.patch("/{list_id}")
def update_list(list_id: str, payload: ListWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    plist = _get_list(db, current_user, list_id)
    data = payload.model_dump(exclude_unset=True)
    if "list_name" in data:
        if not clean_str(data["list_name"]):
            raise HTTPException(status_code=400, detail="Give the list a name")
        plist.list_name = clean_str(data["list_name"])
    if "description" in data:
        plist.description = clean_str(data["description"])
    if "filters" in data:
        if plist.list_type != "ACTIVE":
            raise HTTPException(status_code=400, detail="Only active lists have filters")
        _validate_filters(db, current_user, data["filters"])
        plist.filters = data["filters"]
    db.commit()
    return _list_dict(db, current_user, plist)


@router.delete("/{list_id}")
def delete_list(list_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Delete the list only; its contacts are kept."""
    _require_manager(current_user)
    plist = _get_list(db, current_user, list_id)
    db.query(ProspectListMember).filter(ProspectListMember.list_id == list_id).delete(synchronize_session=False)
    db.delete(plist)
    db.commit()
    return {"status": "deleted", "list_id": list_id}


@router.post("/{list_id}/members")
def add_members(list_id: str, payload: Members, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    plist = _get_list(db, current_user, list_id)
    if plist.list_type != "STATIC":
        raise HTTPException(status_code=400, detail="Active lists update automatically from their filters")
    valid = {r[0] for r in db.query(Prospect.prospect_id).filter(
        Prospect.tenant_id == current_user.tenant_id, Prospect.prospect_id.in_(payload.prospect_ids),
        Prospect.deleted_at.is_(None))}
    existing = {r[0] for r in db.query(ProspectListMember.prospect_id).filter(
        ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(valid))}
    added = [pid for pid in payload.prospect_ids if pid in valid and pid not in existing]
    now = datetime.utcnow()
    for pid in dict.fromkeys(added):
        db.add(ProspectListMember(list_id=list_id, prospect_id=pid, is_new_prospect=False, added_at=now))
    db.commit()
    return {"added": len(set(added)), "already_in_list": len(existing),
            "not_found": len(set(payload.prospect_ids) - valid)}


@router.post("/{list_id}/members/remove")
def remove_members(list_id: str, payload: Members, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    _require_manager(current_user)
    plist = _get_list(db, current_user, list_id)
    if plist.list_type != "STATIC":
        raise HTTPException(status_code=400, detail="Active lists update automatically from their filters")
    removed = db.query(ProspectListMember).filter(
        ProspectListMember.list_id == list_id, ProspectListMember.prospect_id.in_(payload.prospect_ids)
    ).delete(synchronize_session=False)
    db.commit()
    return {"removed": removed}
