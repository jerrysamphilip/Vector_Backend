# app/routers/sales_router.py
"""
Phase 2 sales API (BRD v2.0):
  /team/hierarchy      sales levels and managers (5.4)
  /leads               leads, board, SQL queue, qualification, conversion (5.6)
  /sales/stages        configurable sales stages with probabilities (BR-SP-01)
  /opportunities       deals, board (5.7)
  /proposals           proposal pipeline (BR-SP-02)
  /pipeline/revenue    weighted / unweighted revenue pipeline (BR-SP-03)

Everything is limited to the records of the user and their team.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.sales import (CLIENT_TYPES, LEAD_STAGES, OPEN_LEAD_STAGES, PROPOSAL_STATUSES, SQL_CRITERIA, Lead,
                              Opportunity, Proposal, SalesStage)
from app.models.user import User
from app.services import crm
from app.services import sales as svc
from app.services.contact_service import (SALES_LEVELS, can_access_contact, clean_str, sees_everything,
                                          team_user_ids, visible_user_ids)

router = APIRouter(tags=["Sales"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")


def _record(db, user, object_type, object_id, before, after):
    crm.record_changes(db, user.tenant_id, object_type, object_id, before, after, user.user_id, "UI")


def _history(db: Session, object_type: str, object_id: str) -> List[dict]:
    rows = db.query(PropertyChange).filter(PropertyChange.object_type == object_type,
                                           PropertyChange.object_id == object_id
                                           ).order_by(PropertyChange.changed_at.desc()).limit(200).all()
    names = svc.user_names(db, [r.changed_by for r in rows])
    return [{"field": r.field, "old_value": r.old_value, "new_value": r.new_value, "changed_at": r.changed_at,
             "changed_by_name": names.get(r.changed_by)} for r in rows]


# =============================================================
# Sales hierarchy (BR-SH-01/02)
# =============================================================

class HierarchyUpdate(BaseModel):
    sales_level: Optional[int] = None
    manager_id: Optional[str] = None


@router.get("/team/hierarchy")
def get_hierarchy(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    users = db.query(User).filter(User.tenant_id == current_user.tenant_id,
                                  User.status != "INACTIVE").order_by(User.first_name, User.last_name).all()
    reports: Dict[str, int] = {}
    for u in users:
        if u.manager_id:
            reports[u.manager_id] = reports.get(u.manager_id, 0) + 1
    visible = visible_user_ids(db, current_user)
    return {
        "levels": [{"level": k, "label": v} for k, v in SALES_LEVELS.items()],
        "can_edit": current_user.role in ("SUPER_ADMIN", "ADMIN"),
        "my_team": sorted(visible) if visible is not None else None,
        "users": [{
            "user_id": u.user_id, "name": f"{u.first_name} {u.last_name}".strip(), "email": u.email,
            "role": u.role, "status": u.status, "sales_level": u.sales_level,
            "level_label": SALES_LEVELS.get(u.sales_level), "manager_id": u.manager_id,
            "direct_reports": reports.get(u.user_id, 0),
        } for u in users],
    }


@router.put("/team/hierarchy/{user_id}")
def set_hierarchy(user_id: str, payload: HierarchyUpdate, db: Session = Depends(get_db),
                  current_user: User = Depends(admin_user)):
    """Set a user's sales level and manager (admins). The manager must sit at a higher level."""
    target = db.query(User).filter(User.user_id == user_id, User.tenant_id == current_user.tenant_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    level, manager_id = payload.sales_level, payload.manager_id or None
    if level is not None and level not in SALES_LEVELS:
        raise HTTPException(status_code=400, detail="Sales level must be 1, 2, 3 or 4")
    if level is None or level == 1:
        manager_id = None if level is None else manager_id
    if manager_id:
        manager = db.query(User).filter(User.user_id == manager_id, User.tenant_id == current_user.tenant_id).first()
        if not manager or manager.user_id == target.user_id:
            raise HTTPException(status_code=400, detail="Choose a manager from your workspace")
        if not manager.sales_level or (level and manager.sales_level >= level):
            raise HTTPException(status_code=400, detail="The manager must be at a higher level (a lower level number)")
        if manager.user_id in team_user_ids(db, target):
            raise HTTPException(status_code=400, detail="That would make the user manage their own manager")
    elif level and level > 1:
        raise HTTPException(status_code=400, detail="Levels 2 to 4 need a manager")
    # Direct reports must stay below the user
    clash = db.query(User).filter(User.manager_id == target.user_id, User.sales_level.isnot(None)).all()
    if level is None and clash:
        raise HTTPException(status_code=400, detail="Reassign this user's team before removing them from the hierarchy")
    if level and any(c.sales_level <= level for c in clash):
        raise HTTPException(status_code=400, detail="Some of this user's team would no longer be below them; reassign them first")
    target.sales_level, target.manager_id = level, manager_id
    db.commit()
    return {"user_id": target.user_id, "sales_level": target.sales_level, "manager_id": target.manager_id}


# =============================================================
# Leads (BR-LD-01..06)
# =============================================================

class LeadCreate(BaseModel):
    prospect_id: str
    owner_id: Optional[str] = None
    source: Optional[str] = None
    stage: Optional[str] = "NEW"
    next_step: Optional[str] = None
    next_step_at: Optional[datetime] = None
    campaign_id: Optional[str] = None


class LeadUpdate(BaseModel):
    owner_id: Optional[str] = None
    stage: Optional[str] = None
    source: Optional[str] = None
    next_step: Optional[str] = None
    next_step_at: Optional[datetime] = None
    disqualified_reason: Optional[str] = None
    qualification: Optional[Dict[str, Any]] = None


class ConvertRequest(BaseModel):
    name: Optional[str] = None
    amount: Optional[Any] = None
    close_date: Optional[date] = None
    stage_id: Optional[str] = None
    owner_id: Optional[str] = None
    client_type: Optional[str] = None
    next_step: Optional[str] = None


def _naive(dt):
    """Stored as naive UTC like every other timestamp."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.get("/leads/meta")
def leads_meta(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    return {
        "stages": [{"value": k, "label": v[0], "lifecycle": v[1], "open": k in OPEN_LEAD_STAGES}
                   for k, v in LEAD_STAGES.items()],
        "criteria": [{"key": k, "label": v} for k, v in SQL_CRITERIA.items()],
        "client_types": [{"value": k, "label": v} for k, v in CLIENT_TYPES.items()],
        "proposal_statuses": [{"value": k, "label": v} for k, v in PROPOSAL_STATUSES.items()],
        "sales_stages": [svc.stage_dict(s) for s in svc.stages(db, current_user.tenant_id)],
        "fiscal_year_start_month": svc.settings.FISCAL_YEAR_START_MONTH,
        "sees_everything": sees_everything(current_user),
    }


def _lead_query(db, user, stage=None, owner=None, q=None, source=None, open_only=False, prospect_id=None):
    query = svc.visible_leads(db, user)
    if prospect_id:
        query = query.filter(Lead.prospect_id == prospect_id)
    if stage:
        query = query.filter(Lead.stage.in_(stage.split(",")))
    elif open_only:
        query = query.filter(Lead.stage.in_(OPEN_LEAD_STAGES))
    if owner == "me":
        query = query.filter(Lead.owner_id == user.user_id)
    elif owner:
        query = query.filter(Lead.owner_id == owner)
    if source:
        query = query.filter(Lead.source == source)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.join(Prospect, Prospect.prospect_id == Lead.prospect_id).filter(or_(
            Prospect.first_name.ilike(term), Prospect.last_name.ilike(term), Prospect.email.ilike(term),
            Prospect.company_name.ilike(term)))
    return query


@router.get("/leads")
def list_leads(stage: Optional[str] = None, owner: Optional[str] = None, q: Optional[str] = None,
               source: Optional[str] = None, open_only: bool = False, prospect_id: Optional[str] = None,
               sort_by: str = Query("created_at", pattern="^(created_at|stage_changed_at|next_step_at)$"),
               sort_order: str = Query("desc", pattern="^(asc|desc)$"),
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
               db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    query = _lead_query(db, current_user, stage, owner, q, source, open_only, prospect_id)
    total = query.count()
    col = getattr(Lead, sort_by)
    rows = query.order_by(col.asc() if sort_order == "asc" else col.desc(), Lead.lead_id) \
        .offset((page - 1) * page_size).limit(page_size).all()
    sources = [s for (s,) in svc.visible_leads(db, current_user).with_entities(Lead.source).distinct() if s]
    return {"items": svc.lead_dicts(db, rows), "total": total, "page": page, "page_size": page_size,
            "sources": sorted(sources)}


@router.get("/leads/board")
def lead_board(owner: Optional[str] = None, q: Optional[str] = None, source: Optional[str] = None,
               include_closed: bool = False, per_stage: int = Query(50, ge=1, le=200),
               db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Leads pipeline board by stage (BR-LD-05)."""
    columns = []
    for key, (label, _lifecycle) in LEAD_STAGES.items():
        if not include_closed and key not in OPEN_LEAD_STAGES:
            continue
        query = _lead_query(db, current_user, key, owner, q, source)
        rows = query.order_by(Lead.stage_changed_at.asc()).limit(per_stage).all()
        columns.append({"stage": key, "label": label, "count": query.count(), "items": svc.lead_dicts(db, rows)})
    return {"columns": columns}


@router.get("/leads/sql-queue")
def sql_queue(owner: Optional[str] = None, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Sales-qualified leads waiting to be converted, oldest first, with age and next step (BR-LD-03/04)."""
    query = _lead_query(db, current_user, "SQL", owner)
    rows = query.order_by(Lead.qualified_at.asc()).limit(500).all()
    items = svc.lead_dicts(db, rows)
    ages = [i["sql_age_days"] or 0 for i in items]
    by_owner: Dict[str, dict] = {}
    for i in items:
        o = by_owner.setdefault(i["owner_id"], {"owner_id": i["owner_id"], "owner_name": i["owner_name"],
                                               "count": 0, "oldest_days": 0, "no_next_step": 0})
        o["count"] += 1
        o["oldest_days"] = max(o["oldest_days"], i["sql_age_days"] or 0)
        o["no_next_step"] += 0 if i["next_step"] else 1
    return {
        "items": items, "total": len(items),
        "average_age_days": round(sum(ages) / len(ages), 1) if ages else 0,
        "over_14_days": sum(1 for a in ages if a > 14),
        "without_next_step": sum(1 for i in items if not i["next_step"]),
        "overdue_next_step": sum(1 for i in items if i["next_step_overdue"]),
        "by_owner": sorted(by_owner.values(), key=lambda o: -o["count"]),
    }


@router.post("/leads", status_code=201)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    prospect = db.query(Prospect).filter(Prospect.prospect_id == payload.prospect_id,
                                         Prospect.tenant_id == current_user.tenant_id,
                                         Prospect.deleted_at.is_(None)).first()
    if not prospect or not can_access_contact(current_user, prospect):
        raise HTTPException(status_code=404, detail="Contact not found")
    existing = db.query(Lead).filter(Lead.prospect_id == prospect.prospect_id,
                                     Lead.stage.in_(OPEN_LEAD_STAGES)).first()
    if existing:
        raise HTTPException(status_code=409, detail={"message": "This contact already has an open lead",
                                                     "lead_id": existing.lead_id})
    owner_id = payload.owner_id or prospect.owner_id or current_user.user_id
    svc.check_assignable(db, current_user, owner_id)
    lead = Lead(tenant_id=current_user.tenant_id, prospect_id=prospect.prospect_id, account_id=prospect.account_id,
                owner_id=owner_id, source=clean_str(payload.source) or prospect.lead_source,
                stage="NEW", next_step=clean_str(payload.next_step), next_step_at=_naive(payload.next_step_at),
                campaign_id=payload.campaign_id, created_by=current_user.user_id)
    db.add(lead)
    db.flush()
    if payload.stage and payload.stage not in ("NEW", "SQL"):
        svc.set_lead_stage(db, lead, payload.stage, current_user)
    svc.sync_contact_lifecycle(db, prospect, LEAD_STAGES[lead.stage][1], current_user.user_id)
    _record(db, current_user, "LEAD", lead.lead_id, {"created": None}, {"created": "Lead created"})
    db.commit()
    return svc.lead_dicts(db, [lead])[0]


@router.get("/leads/{lead_id}")
def get_lead(lead_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    lead = svc.get_lead(db, current_user, lead_id)
    result = svc.lead_dicts(db, [lead])[0]
    result["history"] = _history(db, "LEAD", lead.lead_id)
    return result


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: str, payload: LeadUpdate, db: Session = Depends(get_db),
                current_user: User = Depends(tenant_user)):
    lead = svc.get_lead(db, current_user, lead_id)
    data = payload.model_dump(exclude_unset=True)
    before = crm.snapshot(lead, svc.LEAD_FIELDS)
    if "owner_id" in data:
        if not data["owner_id"]:
            raise HTTPException(status_code=400, detail="A lead always has an owner")
        svc.check_assignable(db, current_user, data["owner_id"])
        lead.owner_id = data["owner_id"]
    for field in ("source", "next_step", "disqualified_reason"):
        if field in data:
            setattr(lead, field, clean_str(data[field]))
    if "next_step_at" in data:
        lead.next_step_at = _naive(data["next_step_at"])
    if "qualification" in data:
        q = dict(lead.qualification or {})
        for key in SQL_CRITERIA:
            if key in data["qualification"]:
                q[key] = bool(data["qualification"][key])
        if "notes" in data["qualification"]:
            q["notes"] = clean_str(data["qualification"]["notes"])
        lead.qualification = q
    if data.get("stage"):
        svc.set_lead_stage(db, lead, data["stage"], current_user)
    _record(db, current_user, "LEAD", lead.lead_id, before, crm.snapshot(lead, svc.LEAD_FIELDS))
    db.commit()
    return get_lead(lead_id, db, current_user)


@router.post("/leads/{lead_id}/convert", status_code=201)
def convert(lead_id: str, payload: ConvertRequest, db: Session = Depends(get_db),
            current_user: User = Depends(tenant_user)):
    lead = svc.get_lead(db, current_user, lead_id)
    opp = svc.convert_lead(db, current_user, lead, payload.model_dump(exclude_unset=True))
    db.commit()
    return svc.opp_dicts(db, [opp])[0]


@router.delete("/leads/{lead_id}")
def delete_lead(lead_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    lead = svc.get_lead(db, current_user, lead_id)
    if lead.stage == "CONVERTED":
        raise HTTPException(status_code=400, detail="Converted leads are kept with their opportunity")
    if current_user.role == "AGENT" and lead.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="You can only delete your own leads")
    db.delete(lead)
    db.commit()
    return {"status": "deleted", "lead_id": lead_id}


# =============================================================
# Sales stages (BR-SP-01)
# =============================================================

class StageWrite(BaseModel):
    name: Optional[str] = None
    probability: Optional[int] = None
    sort_order: Optional[int] = None
    is_won: Optional[bool] = None
    is_lost: Optional[bool] = None
    active: Optional[bool] = None


@router.get("/sales/stages")
def list_stages(include_inactive: bool = False, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    return [svc.stage_dict(s) for s in svc.stages(db, current_user.tenant_id, include_inactive)]


def _check_stage(data: dict):
    if "probability" in data and data["probability"] is not None and not 0 <= data["probability"] <= 100:
        raise HTTPException(status_code=400, detail="Probability must be between 0 and 100")
    if data.get("is_won") and data.get("is_lost"):
        raise HTTPException(status_code=400, detail="A stage cannot be both won and lost")


@router.post("/sales/stages", status_code=201)
def create_stage(payload: StageWrite, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    data = payload.model_dump(exclude_unset=True)
    _check_stage(data)
    name = clean_str(data.get("name"))
    if not name:
        raise HTTPException(status_code=400, detail="Give the stage a name")
    existing = svc.stages(db, current_user.tenant_id, True)
    stage = SalesStage(tenant_id=current_user.tenant_id, name=name, probability=data.get("probability") or 0,
                       sort_order=data.get("sort_order", max((s.sort_order for s in existing), default=0) + 1),
                       is_won=bool(data.get("is_won")), is_lost=bool(data.get("is_lost")))
    if stage.is_won:
        stage.probability = 100
    if stage.is_lost:
        stage.probability = 0
    db.add(stage)
    db.commit()
    return svc.stage_dict(stage)


@router.patch("/sales/stages/{stage_id}")
def update_stage(stage_id: str, payload: StageWrite, db: Session = Depends(get_db),
                 current_user: User = Depends(admin_user)):
    stage = db.query(SalesStage).filter(SalesStage.stage_id == stage_id,
                                        SalesStage.tenant_id == current_user.tenant_id).first()
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")
    data = payload.model_dump(exclude_unset=True)
    _check_stage({**svc.stage_dict(stage), **data})
    if "name" in data:
        stage.name = clean_str(data["name"]) or stage.name
    for f in ("probability", "sort_order", "is_won", "is_lost", "active"):
        if f in data and data[f] is not None:
            setattr(stage, f, data[f])
    if data.get("active") is False and not any(s.active for s in svc.stages(db, current_user.tenant_id, True)
                                               if s.stage_id != stage.stage_id and svc.stage_status(s) == "OPEN"):
        raise HTTPException(status_code=400, detail="Keep at least one open stage active")
    # Deals in this stage follow its won/lost flag
    status = svc.stage_status(stage)
    db.query(Opportunity).filter(Opportunity.stage_id == stage.stage_id, Opportunity.status != status) \
        .update({Opportunity.status: status}, synchronize_session=False)
    db.commit()
    return svc.stage_dict(stage)


# =============================================================
# Opportunities (BR-SP-01, 03, 04)
# =============================================================

class OppWrite(BaseModel):
    name: Optional[str] = None
    account_id: Optional[str] = None
    prospect_id: Optional[str] = None
    owner_id: Optional[str] = None
    stage_id: Optional[str] = None
    amount: Optional[Any] = None
    close_date: Optional[date] = None
    client_type: Optional[str] = None
    closed_reason: Optional[str] = None
    next_step: Optional[str] = None
    description: Optional[str] = None


def _opp_query(db, user, owner=None, status=None, stage_id=None, client_type=None, close_from=None,
               close_to=None, q=None, account_id=None, prospect_id=None):
    query = svc.visible_opps(db, user)
    if prospect_id:
        query = query.filter(Opportunity.prospect_id == prospect_id)
    if owner == "me":
        query = query.filter(Opportunity.owner_id == user.user_id)
    elif owner:
        query = query.filter(Opportunity.owner_id == owner)
    if status:
        query = query.filter(Opportunity.status.in_(status.split(",")))
    if stage_id:
        query = query.filter(Opportunity.stage_id == stage_id)
    if client_type:
        query = query.filter(Opportunity.client_type == client_type)
    if close_from:
        query = query.filter(Opportunity.close_date >= close_from)
    if close_to:
        query = query.filter(Opportunity.close_date <= close_to)
    if account_id:
        query = query.filter(Opportunity.account_id == account_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.outerjoin(Account, Account.account_id == Opportunity.account_id) \
            .filter(or_(Opportunity.name.ilike(term), Account.name.ilike(term)))
    return query


def _check_opp_links(db: Session, user: User, data: dict):
    if data.get("client_type") and data["client_type"] not in CLIENT_TYPES:
        raise HTTPException(status_code=400, detail="client_type must be NEW or EXISTING")
    if data.get("account_id") and not db.query(Account.account_id).filter(
            Account.account_id == data["account_id"], Account.tenant_id == user.tenant_id).first():
        raise HTTPException(status_code=400, detail="Company not found")
    if data.get("prospect_id"):
        p = db.query(Prospect).filter(Prospect.prospect_id == data["prospect_id"],
                                      Prospect.tenant_id == user.tenant_id).first()
        if not p or not can_access_contact(user, p):
            raise HTTPException(status_code=400, detail="Contact not found")


@router.get("/opportunities")
def list_opps(owner: Optional[str] = None, status: Optional[str] = None, stage_id: Optional[str] = None,
              client_type: Optional[str] = None, close_from: Optional[date] = None, close_to: Optional[date] = None,
              q: Optional[str] = None, account_id: Optional[str] = None, prospect_id: Optional[str] = None,
              sort_by: str = Query("close_date", pattern="^(close_date|amount|created_at|updated_at|name)$"),
              sort_order: str = Query("asc", pattern="^(asc|desc)$"),
              page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
              db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    query = _opp_query(db, current_user, owner, status, stage_id, client_type, close_from, close_to, q, account_id,
                       prospect_id)
    agg = query.with_entities(func.count(Opportunity.opportunity_id),
                              func.coalesce(func.sum(Opportunity.amount), 0)).one()
    col = getattr(Opportunity, sort_by)
    rows = query.order_by(col.is_(None), col.asc() if sort_order == "asc" else col.desc(),
                          Opportunity.opportunity_id).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": svc.opp_dicts(db, rows), "total": agg[0], "total_amount": svc.as_float(agg[1]),
            "page": page, "page_size": page_size}


@router.get("/opportunities/board")
def opp_board(owner: Optional[str] = None, client_type: Optional[str] = None, q: Optional[str] = None,
              include_closed: bool = False, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    columns = []
    for stage in svc.stages(db, current_user.tenant_id):
        if not include_closed and svc.stage_status(stage) != "OPEN":
            continue
        query = _opp_query(db, current_user, owner, None, stage.stage_id, client_type, q=q)
        rows = query.order_by(Opportunity.close_date.is_(None), Opportunity.close_date).limit(100).all()
        total = svc.as_float(query.with_entities(func.coalesce(func.sum(Opportunity.amount), 0)).scalar())
        columns.append({**svc.stage_dict(stage), "count": query.count(), "amount": total,
                        "weighted": round(total * stage.probability / 100, 2), "items": svc.opp_dicts(db, rows)})
    return {"columns": columns}


@router.post("/opportunities", status_code=201)
def create_opp(payload: OppWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    data = payload.model_dump(exclude_unset=True)
    name = clean_str(data.get("name"))
    if not name:
        raise HTTPException(status_code=400, detail="Give the opportunity a name")
    _check_opp_links(db, current_user, data)
    owner_id = data.get("owner_id") or current_user.user_id
    svc.check_assignable(db, current_user, owner_id)
    stage_list = svc.stages(db, current_user.tenant_id)
    account_id = data.get("account_id")
    if not account_id and data.get("prospect_id"):
        account_id = db.query(Prospect.account_id).filter(Prospect.prospect_id == data["prospect_id"]).scalar()
    opp = Opportunity(tenant_id=current_user.tenant_id, name=name[:255], account_id=account_id,
                      prospect_id=data.get("prospect_id"), owner_id=owner_id,
                      stage_id=data.get("stage_id") or stage_list[0].stage_id, amount=svc.money(data.get("amount")),
                      close_date=data.get("close_date"),
                      client_type=data.get("client_type") or svc.default_client_type(db, current_user.tenant_id, account_id),
                      next_step=clean_str(data.get("next_step")), description=data.get("description"),
                      created_by=current_user.user_id)
    svc.apply_stage(db, opp, opp.stage_id)
    db.add(opp)
    db.flush()
    _record(db, current_user, "DEAL", opp.opportunity_id, {"created": None}, {"created": "Opportunity created"})
    db.commit()
    return svc.opp_dicts(db, [opp])[0]


@router.get("/opportunities/{opportunity_id}")
def get_opp(opportunity_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    opp = svc.get_opp(db, current_user, opportunity_id)
    result = svc.opp_dicts(db, [opp])[0]
    result["history"] = _history(db, "DEAL", opp.opportunity_id)
    result["proposals"] = [_proposal_dict(p) for p in db.query(Proposal).filter(
        Proposal.opportunity_id == opp.opportunity_id).order_by(Proposal.created_at.desc())]
    lead = db.query(Lead).filter(Lead.lead_id == opp.lead_id).first() if opp.lead_id else None
    result["lead"] = svc.lead_dicts(db, [lead])[0] if lead else None
    return result


@router.patch("/opportunities/{opportunity_id}")
def update_opp(opportunity_id: str, payload: OppWrite, db: Session = Depends(get_db),
               current_user: User = Depends(tenant_user)):
    opp = svc.get_opp(db, current_user, opportunity_id)
    data = payload.model_dump(exclude_unset=True)
    _check_opp_links(db, current_user, data)
    before = crm.snapshot(opp, svc.OPP_FIELDS)
    if "owner_id" in data:
        if not data["owner_id"]:
            raise HTTPException(status_code=400, detail="An opportunity always has an owner")
        svc.check_assignable(db, current_user, data["owner_id"])
        opp.owner_id = data["owner_id"]
    if "name" in data:
        opp.name = clean_str(data["name"]) or opp.name
    if "amount" in data:
        opp.amount = svc.money(data["amount"])
    for f in ("close_date", "client_type", "account_id", "prospect_id", "description"):
        if f in data:
            setattr(opp, f, data[f] if data[f] != "" else None)
    for f in ("closed_reason", "next_step"):
        if f in data:
            setattr(opp, f, clean_str(data[f]))
    if data.get("stage_id") and data["stage_id"] != opp.stage_id:
        stage = svc.apply_stage(db, opp, data["stage_id"])
        if stage.is_won and opp.prospect_id:
            prospect = db.query(Prospect).filter(Prospect.prospect_id == opp.prospect_id).first()
            svc.sync_contact_lifecycle(db, prospect, "CUSTOMER", current_user.user_id)
    after = crm.snapshot(opp, svc.OPP_FIELDS)
    if before.get("stage_id") != after.get("stage_id"):
        names = {s.stage_id: s.name for s in svc.stages(db, current_user.tenant_id, True)}
        before["stage_id"], after["stage_id"] = names.get(before["stage_id"]), names.get(after["stage_id"])
    _record(db, current_user, "DEAL", opp.opportunity_id, before, after)
    db.commit()
    return get_opp(opportunity_id, db, current_user)


@router.delete("/opportunities/{opportunity_id}")
def delete_opp(opportunity_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    opp = svc.get_opp(db, current_user, opportunity_id)
    if current_user.role == "AGENT":
        raise HTTPException(status_code=403, detail="Agents cannot delete opportunities")
    db.query(Proposal).filter(Proposal.opportunity_id == opp.opportunity_id).delete(synchronize_session=False)
    db.query(Lead).filter(Lead.opportunity_id == opp.opportunity_id).update(
        {Lead.opportunity_id: None, Lead.stage: "SQL"}, synchronize_session=False)
    db.delete(opp)
    db.commit()
    return {"status": "deleted", "opportunity_id": opportunity_id}


# =============================================================
# Proposals (BR-SP-02)
# =============================================================

class ProposalWrite(BaseModel):
    opportunity_id: Optional[str] = None
    title: Optional[str] = None
    amount: Optional[Any] = None
    status: Optional[str] = None
    valid_until: Optional[date] = None
    notes: Optional[str] = None


def _proposal_dict(p: Proposal, opp: Optional[dict] = None) -> dict:
    d = {"proposal_id": p.proposal_id, "opportunity_id": p.opportunity_id, "title": p.title,
         "amount": svc.as_float(p.amount) if p.amount is not None else None, "status": p.status,
         "status_label": PROPOSAL_STATUSES.get(p.status), "valid_until": p.valid_until, "notes": p.notes,
         "sent_at": p.sent_at, "decided_at": p.decided_at, "created_at": p.created_at}
    if opp:
        d.update({"opportunity_name": opp["name"], "company_name": opp["company_name"],
                  "owner_id": opp["owner_id"], "owner_name": opp["owner_name"], "client_type": opp["client_type"]})
    return d


def _set_proposal_status(p: Proposal, status: str):
    if status not in PROPOSAL_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {list(PROPOSAL_STATUSES)}")
    if status != "DRAFT" and not p.sent_at:
        p.sent_at = datetime.utcnow()
    p.decided_at = datetime.utcnow() if status in ("ACCEPTED", "REJECTED") else None
    p.status = status


def _get_proposal(db, user, proposal_id) -> Proposal:
    p = db.query(Proposal).filter(Proposal.proposal_id == proposal_id, Proposal.tenant_id == user.tenant_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found")
    svc.get_opp(db, user, p.opportunity_id)  # visibility through its opportunity
    return p


@router.get("/proposals")
def list_proposals(status: Optional[str] = None, client_type: Optional[str] = None, owner: Optional[str] = None,
                   opportunity_id: Optional[str] = None, db: Session = Depends(get_db),
                   current_user: User = Depends(tenant_user)):
    """The proposal pipeline: each status with its proposals and total (BR-SP-02, BR-SP-04)."""
    opps = _opp_query(db, current_user, owner, None, None, client_type)
    if opportunity_id:
        opps = opps.filter(Opportunity.opportunity_id == opportunity_id)
    opp_ids = opps.with_entities(Opportunity.opportunity_id)
    query = db.query(Proposal).filter(Proposal.tenant_id == current_user.tenant_id,
                                      Proposal.opportunity_id.in_(opp_ids))
    if status:
        query = query.filter(Proposal.status.in_(status.split(",")))
    rows = query.order_by(Proposal.created_at.desc()).limit(1000).all()
    opp_map = {o["opportunity_id"]: o for o in svc.opp_dicts(db, db.query(Opportunity).filter(
        Opportunity.opportunity_id.in_({p.opportunity_id for p in rows})).all())} if rows else {}
    items = [_proposal_dict(p, opp_map.get(p.opportunity_id)) for p in rows]
    columns = []
    for key, label in PROPOSAL_STATUSES.items():
        col = [i for i in items if i["status"] == key]
        columns.append({"status": key, "label": label, "count": len(col),
                        "amount": round(sum(i["amount"] or 0 for i in col), 2), "items": col})
    return {"columns": columns, "items": items, "total": len(items),
            "total_amount": round(sum(i["amount"] or 0 for i in items), 2)}


@router.post("/proposals", status_code=201)
def create_proposal(payload: ProposalWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    data = payload.model_dump(exclude_unset=True)
    if not data.get("opportunity_id"):
        raise HTTPException(status_code=400, detail="A proposal belongs to an opportunity")
    opp = svc.get_opp(db, current_user, data["opportunity_id"])
    title = clean_str(data.get("title")) or f"Proposal - {opp.name}"
    p = Proposal(tenant_id=current_user.tenant_id, opportunity_id=opp.opportunity_id, title=title[:255],
                 amount=svc.money(data.get("amount")) if "amount" in data else opp.amount,
                 valid_until=data.get("valid_until"), notes=data.get("notes"), created_by=current_user.user_id)
    _set_proposal_status(p, data.get("status") or "DRAFT")
    db.add(p)
    _record(db, current_user, "DEAL", opp.opportunity_id, {"proposal": None}, {"proposal": f"{title} ({p.status.lower()})"})
    db.commit()
    return _proposal_dict(p, svc.opp_dicts(db, [opp])[0])


@router.patch("/proposals/{proposal_id}")
def update_proposal(proposal_id: str, payload: ProposalWrite, db: Session = Depends(get_db),
                    current_user: User = Depends(tenant_user)):
    p = _get_proposal(db, current_user, proposal_id)
    data = payload.model_dump(exclude_unset=True)
    old_status = p.status
    if "title" in data:
        p.title = clean_str(data["title"]) or p.title
    if "amount" in data:
        p.amount = svc.money(data["amount"])
    for f in ("valid_until", "notes"):
        if f in data:
            setattr(p, f, data[f])
    if data.get("status"):
        _set_proposal_status(p, data["status"])
    if p.status != old_status:
        _record(db, current_user, "DEAL", p.opportunity_id, {"proposal": f"{p.title}: {old_status.lower()}"},
                {"proposal": f"{p.title}: {p.status.lower()}"})
    db.commit()
    opp = db.query(Opportunity).filter(Opportunity.opportunity_id == p.opportunity_id).first()
    return _proposal_dict(p, svc.opp_dicts(db, [opp])[0])


@router.delete("/proposals/{proposal_id}")
def delete_proposal(proposal_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    p = _get_proposal(db, current_user, proposal_id)
    db.delete(p)
    db.commit()
    return {"status": "deleted", "proposal_id": proposal_id}


# =============================================================
# Revenue pipeline (BR-SP-03/04)
# =============================================================

@router.get("/pipeline/revenue")
def revenue_pipeline(owner: Optional[str] = None, client_type: Optional[str] = None,
                     close_from: Optional[date] = None, close_to: Optional[date] = None,
                     db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """Open pipeline by stage, unweighted and weighted, plus won and lost in the same filter.
    Totals are sums of the same opportunities GET /opportunities returns for these filters."""
    stage_list = svc.stages(db, current_user.tenant_id, include_inactive=True)
    base = _opp_query(db, current_user, owner, None, None, client_type, close_from, close_to)
    rows = base.with_entities(Opportunity.stage_id, Opportunity.client_type,
                              func.count(Opportunity.opportunity_id),
                              func.coalesce(func.sum(Opportunity.amount), 0)) \
        .group_by(Opportunity.stage_id, Opportunity.client_type).all()
    by_stage: Dict[str, dict] = {}
    for stage_id, ctype, n, amount in rows:
        s = by_stage.setdefault(stage_id, {"count": 0, "amount": 0.0, "NEW": 0.0, "EXISTING": 0.0})
        s["count"] += n
        s["amount"] += svc.as_float(amount)
        s[ctype if ctype in ("NEW", "EXISTING") else "NEW"] += svc.as_float(amount)
    stages_out, totals = [], {"open_count": 0, "open_amount": 0.0, "weighted_amount": 0.0,
                              "won_count": 0, "won_amount": 0.0, "lost_count": 0, "lost_amount": 0.0,
                              "by_client_type": {"NEW": {"amount": 0.0, "weighted": 0.0},
                                                 "EXISTING": {"amount": 0.0, "weighted": 0.0}}}
    for stage in stage_list:
        s = by_stage.get(stage.stage_id)
        if not s and not stage.active:
            continue
        s = s or {"count": 0, "amount": 0.0, "NEW": 0.0, "EXISTING": 0.0}
        status = svc.stage_status(stage)
        weighted = round(s["amount"] * stage.probability / 100, 2) if status == "OPEN" else 0.0
        stages_out.append({**svc.stage_dict(stage), "count": s["count"], "amount": round(s["amount"], 2),
                           "weighted": weighted, "new_amount": round(s["NEW"], 2),
                           "existing_amount": round(s["EXISTING"], 2)})
        if status == "OPEN":
            totals["open_count"] += s["count"]
            totals["open_amount"] += s["amount"]
            totals["weighted_amount"] += weighted
            for ct in ("NEW", "EXISTING"):
                totals["by_client_type"][ct]["amount"] += s[ct]
                totals["by_client_type"][ct]["weighted"] += s[ct] * stage.probability / 100
        elif status == "WON":
            totals["won_count"] += s["count"]
            totals["won_amount"] += s["amount"]
        else:
            totals["lost_count"] += s["count"]
            totals["lost_amount"] += s["amount"]
    for k in ("open_amount", "weighted_amount", "won_amount", "lost_amount"):
        totals[k] = round(totals[k], 2)
    for ct in totals["by_client_type"].values():
        ct["amount"], ct["weighted"] = round(ct["amount"], 2), round(ct["weighted"], 2)
    return {"stages": stages_out, "totals": totals}
