# app/routers/sales_admin_router.py
"""
Phase 2 "Should" features (BRD v2.0 5.12):
  /sales/settings        lead assignment, reasons, stale days, amount visibility (BR-SF-02, 03, 05, 12)
  /workflow/rules        field-change rules: task / alert / set field (BR-SF-13)
  /notifications         in-app notifications and email preference (BR-SF-07)
  /sales/targets         revenue targets per rep per quarter (BR-SF-08)
  /template-library      email template library with merge fields (BR-SF-10)
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.prospect import Prospect
from app.models.sales_extra import MessageTemplate, Notification, SalesTarget, WorkflowRule
from app.models.user import User
from app.services import sales as svc
from app.services import workflow
from app.services.contact_service import can_access_contact, clean_str, sees_everything, team_user_ids, visible_user_ids
from app.services.notifications import as_dict as notification_dict
from app.services.sales_settings import DEFAULTS, can_see_amounts, get_settings, save_settings

router = APIRouter(tags=["Sales settings"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")


# ── Settings ─────────────────────────────────────────────────

@router.get("/sales/settings")
def read_settings(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    s = get_settings(db, current_user.tenant_id)
    s["can_edit"] = current_user.role in ("SUPER_ADMIN", "ADMIN")
    return s


@router.put("/sales/settings")
def write_settings(payload: Dict[str, Any], db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    changes = {k: v for k, v in payload.items() if k in DEFAULTS}
    la = changes.get("lead_assignment")
    if la is not None:
        if la.get("mode") not in (None, "off", "round_robin", "region"):
            raise HTTPException(status_code=400, detail="mode must be off, round_robin or region")
        ids = set(la.get("users") or []) | {r.get("user_id") for r in la.get("regions") or []}
        if ids and db.query(User.user_id).filter(User.user_id.in_(ids), User.tenant_id == current_user.tenant_id).count() != len(ids):
            raise HTTPException(status_code=400, detail="Assignment users must belong to your workspace")
    for key in ("stale_deal_days", "default_recycle_days"):
        if key in changes and (not isinstance(changes[key], int) or changes[key] < 1):
            raise HTTPException(status_code=400, detail=f"{key} must be a positive number of days")
    result = save_settings(db, current_user.tenant_id, changes)
    db.commit()
    return result


# ── Workflow rules (BR-SF-13) ────────────────────────────────

class RuleWrite(BaseModel):
    name: Optional[str] = None
    object_type: Optional[str] = None
    field: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    actions: Optional[List[Dict[str, Any]]] = None
    active: Optional[bool] = None


def _rule_dict(r: WorkflowRule) -> dict:
    return {"rule_id": r.rule_id, "name": r.name, "object_type": r.object_type, "field": r.field,
            "operator": r.operator, "value": r.value, "actions": r.actions, "active": r.active,
            "run_count": r.run_count, "last_run_at": r.last_run_at, "created_at": r.created_at}


def _validate_rule(data: dict, db: Session, user: User):
    obj = data.get("object_type")
    if obj not in workflow.WATCH_FIELDS:
        raise HTTPException(status_code=400, detail="object_type must be LEAD, DEAL or CONTACT")
    if data.get("field") not in workflow.WATCH_FIELDS[obj]:
        raise HTTPException(status_code=400, detail=f"field must be one of {list(workflow.WATCH_FIELDS[obj])}")
    if data.get("operator") not in workflow.OPERATORS:
        raise HTTPException(status_code=400, detail=f"operator must be one of {list(workflow.OPERATORS)}")
    if data["operator"] != "changes" and not clean_str(data.get("value")):
        raise HTTPException(status_code=400, detail="Give the value to compare with")
    if data["operator"] == "greater_than":
        try:
            float(data["value"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'greater than' needs a number")
    actions = data.get("actions") or []
    if not actions:
        raise HTTPException(status_code=400, detail="Add at least one action")
    for a in actions:
        if a.get("type") not in workflow.ACTION_TYPES:
            raise HTTPException(status_code=400, detail=f"Action type must be one of {list(workflow.ACTION_TYPES)}")
        if a["type"] == "create_task" and not clean_str(a.get("title")):
            raise HTTPException(status_code=400, detail="A task action needs a title")
        if a["type"] == "update_field" and a.get("field") not in workflow.SETTABLE_FIELDS[obj]:
            raise HTTPException(status_code=400, detail=f"Can set only {list(workflow.SETTABLE_FIELDS[obj])} on this object")
        for who in ([a.get("assign_to")] if a["type"] == "create_task" else (a.get("to") or []) if a["type"] == "notify" else []):
            if who and who not in ("owner", "manager", "actor") and not db.query(User.user_id).filter(
                    User.user_id == who, User.tenant_id == user.tenant_id).first():
                raise HTTPException(status_code=400, detail="Recipients must be owner, manager or a workspace user")


@router.get("/workflow/meta")
def workflow_meta(current_user: User = Depends(tenant_user)):
    return {"watch_fields": workflow.WATCH_FIELDS, "settable_fields": workflow.SETTABLE_FIELDS,
            "operators": workflow.OPERATORS, "action_types": list(workflow.ACTION_TYPES)}


@router.get("/workflow/rules")
def list_rules(db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    rows = db.query(WorkflowRule).filter(WorkflowRule.tenant_id == current_user.tenant_id) \
        .order_by(WorkflowRule.created_at.desc()).all()
    return [_rule_dict(r) for r in rows]


@router.post("/workflow/rules", status_code=201)
def create_rule(payload: RuleWrite, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    data = payload.model_dump(exclude_unset=True)
    if not clean_str(data.get("name")):
        raise HTTPException(status_code=400, detail="Give the rule a name")
    data.setdefault("operator", "changes")
    _validate_rule(data, db, current_user)
    rule = WorkflowRule(tenant_id=current_user.tenant_id, name=clean_str(data["name"]), object_type=data["object_type"],
                        field=data["field"], operator=data["operator"], value=clean_str(data.get("value")),
                        actions=data["actions"], active=data.get("active", True), created_by=current_user.user_id)
    db.add(rule)
    db.commit()
    return _rule_dict(rule)


@router.patch("/workflow/rules/{rule_id}")
def update_rule(rule_id: str, payload: RuleWrite, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    rule = db.query(WorkflowRule).filter(WorkflowRule.rule_id == rule_id,
                                         WorkflowRule.tenant_id == current_user.tenant_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    data = {**_rule_dict(rule), **payload.model_dump(exclude_unset=True)}
    _validate_rule(data, db, current_user)
    for f in ("object_type", "field", "operator", "actions", "active"):
        setattr(rule, f, data[f])
    rule.name = clean_str(data["name"]) or rule.name
    rule.value = clean_str(data.get("value"))
    db.commit()
    return _rule_dict(rule)


@router.delete("/workflow/rules/{rule_id}")
def delete_rule(rule_id: str, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    n = db.query(WorkflowRule).filter(WorkflowRule.rule_id == rule_id,
                                      WorkflowRule.tenant_id == current_user.tenant_id).delete()
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "deleted", "rule_id": rule_id}


# ── Notifications (BR-SF-07) ─────────────────────────────────

@router.get("/notifications")
def list_notifications(unread_only: bool = False, limit: int = Query(30, ge=1, le=100),
                       db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    q = db.query(Notification).filter(Notification.user_id == current_user.user_id)
    unread = q.filter(Notification.read_at.is_(None)).count()
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    rows = q.order_by(Notification.created_at.desc()).limit(limit).all()
    return {"items": [notification_dict(n) for n in rows], "unread": unread,
            "email_enabled": bool(current_user.notify_email)}


class ReadRequest(BaseModel):
    ids: Optional[List[str]] = None   # none = all


@router.post("/notifications/read")
def mark_read(payload: ReadRequest, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    q = db.query(Notification).filter(Notification.user_id == current_user.user_id, Notification.read_at.is_(None))
    if payload.ids:
        q = q.filter(Notification.notification_id.in_(payload.ids))
    n = q.update({Notification.read_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    return {"marked": n}


class PrefRequest(BaseModel):
    email: bool


@router.put("/notifications/preferences")
def notification_prefs(payload: PrefRequest, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    current_user.notify_email = payload.email
    db.commit()
    return {"email_enabled": payload.email}


# ── Targets (BR-SF-08) ───────────────────────────────────────

class TargetWrite(BaseModel):
    user_id: str
    fy: int
    quarter: int
    amount: Any


def _can_set_target(db: Session, user: User, target_user_id: str) -> bool:
    if user.role in ("SUPER_ADMIN", "ADMIN") or user.sales_level == 1:
        return True
    return target_user_id != user.user_id and target_user_id in team_user_ids(db, user)


@router.get("/sales/targets")
def list_targets(fy: Optional[int] = None, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    fy = fy or svc.fiscal_year_of(date.today())
    visible = visible_user_ids(db, current_user)
    users = db.query(User).filter(User.tenant_id == current_user.tenant_id, User.status == "ACTIVE",
                                  User.role.in_(("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")))
    if visible is not None:
        users = users.filter(User.user_id.in_(visible))
    users = users.order_by(User.sales_level.is_(None), User.sales_level, User.first_name).all()
    rows = {(t.user_id, t.quarter): svc.as_float(t.amount) for t in db.query(SalesTarget).filter(
        SalesTarget.tenant_id == current_user.tenant_id, SalesTarget.fy == fy)}
    result = {"fy": fy, "label": svc.fiscal_label(fy), "users": [{
        "user_id": u.user_id, "name": f"{u.first_name} {u.last_name}".strip(), "sales_level": u.sales_level,
        "manager_id": u.manager_id, "can_edit": _can_set_target(db, current_user, u.user_id),
        "quarters": [rows.get((u.user_id, q)) for q in (1, 2, 3, 4)],
        "total": round(sum(rows.get((u.user_id, q)) or 0 for q in (1, 2, 3, 4)), 2),
    } for u in users]}
    if not can_see_amounts(db, current_user):
        for u in result["users"]:
            u["quarters"], u["total"], u["can_edit"] = [None] * 4, None, False
    return result


@router.put("/sales/targets")
def set_target(payload: TargetWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    if payload.quarter not in (1, 2, 3, 4):
        raise HTTPException(status_code=400, detail="quarter must be 1 to 4")
    if not db.query(User.user_id).filter(User.user_id == payload.user_id, User.tenant_id == current_user.tenant_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    if not _can_set_target(db, current_user, payload.user_id) or not can_see_amounts(db, current_user):
        raise HTTPException(status_code=403, detail="You can set targets only for people in your team")
    amount = svc.money(payload.amount) or Decimal("0")
    row = db.query(SalesTarget).filter(SalesTarget.tenant_id == current_user.tenant_id, SalesTarget.user_id == payload.user_id,
                                       SalesTarget.fy == payload.fy, SalesTarget.quarter == payload.quarter).first()
    if row:
        row.amount, row.set_by = amount, current_user.user_id
    else:
        db.add(SalesTarget(tenant_id=current_user.tenant_id, user_id=payload.user_id, fy=payload.fy,
                           quarter=payload.quarter, amount=amount, set_by=current_user.user_id))
    db.commit()
    return {"user_id": payload.user_id, "fy": payload.fy, "quarter": payload.quarter, "amount": float(amount)}


# ── Template library (BR-SF-10) ──────────────────────────────

MERGE_FIELDS = [
    ("{{first_name}}", "First name"), ("{{last_name}}", "Last name"), ("{{full_name}}", "Full name"),
    ("{{company_name}}", "Company"), ("{{designation}}", "Job title"), ("{{email}}", "Email"),
    ("{{industry}}", "Industry"), ("{{your_name}}", "Your name"), ("{{calendar_link}}", "Calendar link"),
]


class TemplateWrite(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    shared: Optional[bool] = None


def _template_dict(t: MessageTemplate, user: User, names: dict = None) -> dict:
    return {"template_id": t.template_id, "name": t.name, "category": t.category, "subject": t.subject, "body": t.body,
            "shared": t.shared, "owner_id": t.owner_id, "owner_name": (names or {}).get(t.owner_id),
            "usage_count": t.usage_count, "updated_at": t.updated_at,
            "can_edit": t.owner_id == user.user_id or user.role in ("SUPER_ADMIN", "ADMIN")}


def _get_template(db, user, template_id, edit=False) -> MessageTemplate:
    t = db.query(MessageTemplate).filter(MessageTemplate.template_id == template_id,
                                         MessageTemplate.tenant_id == user.tenant_id).first()
    if not t or not (t.shared or t.owner_id == user.user_id):
        raise HTTPException(status_code=404, detail="Template not found")
    if edit and not _template_dict(t, user)["can_edit"]:
        raise HTTPException(status_code=403, detail="Only the template's owner or an admin can change it")
    return t


@router.get("/template-library")
def list_templates(q: Optional[str] = None, category: Optional[str] = None,
                   db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    query = db.query(MessageTemplate).filter(MessageTemplate.tenant_id == current_user.tenant_id,
                                             or_(MessageTemplate.shared.is_(True), MessageTemplate.owner_id == current_user.user_id))
    if category:
        query = query.filter(MessageTemplate.category == category)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(or_(MessageTemplate.name.ilike(term), MessageTemplate.subject.ilike(term)))
    rows = query.order_by(MessageTemplate.usage_count.desc(), MessageTemplate.name).limit(500).all()
    names = svc.user_names(db, [t.owner_id for t in rows])
    cats = sorted({c for (c,) in db.query(MessageTemplate.category).filter(
        MessageTemplate.tenant_id == current_user.tenant_id, MessageTemplate.category.isnot(None)).distinct()})
    return {"items": [_template_dict(t, current_user, names) for t in rows], "categories": cats,
            "merge_fields": [{"token": k, "label": v} for k, v in MERGE_FIELDS]}


@router.post("/template-library", status_code=201)
def create_template(payload: TemplateWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    data = payload.model_dump(exclude_unset=True)
    for f in ("name", "subject", "body"):
        if not clean_str(data.get(f)):
            raise HTTPException(status_code=400, detail=f"The template needs a {f}")
    t = MessageTemplate(tenant_id=current_user.tenant_id, name=clean_str(data["name"]), category=clean_str(data.get("category")),
                        subject=data["subject"].strip(), body=data["body"], shared=data.get("shared", True),
                        owner_id=current_user.user_id)
    db.add(t)
    db.commit()
    return _template_dict(t, current_user)


@router.patch("/template-library/{template_id}")
def update_template(template_id: str, payload: TemplateWrite, db: Session = Depends(get_db),
                    current_user: User = Depends(tenant_user)):
    t = _get_template(db, current_user, template_id, edit=True)
    data = payload.model_dump(exclude_unset=True)
    for f in ("name", "subject", "body"):
        if f in data:
            if not clean_str(data[f]):
                raise HTTPException(status_code=400, detail=f"The template needs a {f}")
            setattr(t, f, data[f] if f == "body" else data[f].strip())
    if "category" in data:
        t.category = clean_str(data["category"])
    if "shared" in data:
        t.shared = bool(data["shared"])
    db.commit()
    return _template_dict(t, current_user)


@router.delete("/template-library/{template_id}")
def delete_template(template_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    t = _get_template(db, current_user, template_id, edit=True)
    db.delete(t)
    db.commit()
    return {"status": "deleted", "template_id": template_id}


class PreviewRequest(BaseModel):
    prospect_id: Optional[str] = None
    count_use: bool = False


@router.post("/template-library/{template_id}/render")
def render_template(template_id: str, payload: PreviewRequest, db: Session = Depends(get_db),
                    current_user: User = Depends(tenant_user)):
    """The template with merge fields filled in for one contact (or sample values)."""
    from types import SimpleNamespace
    from app.services.email_sender_service import email_sender
    t = _get_template(db, current_user, template_id)
    prospect = None
    if payload.prospect_id:
        prospect = db.query(Prospect).filter(Prospect.prospect_id == payload.prospect_id,
                                             Prospect.tenant_id == current_user.tenant_id).first()
        if not prospect or not can_access_contact(current_user, prospect):
            raise HTTPException(status_code=404, detail="Contact not found")
    sample = prospect or SimpleNamespace(first_name="Alex", last_name="Morgan", company_name="Acme Corp",
                                          designation="Head of Operations", email="alex@acme.example", industry="Software",
                                          linkedin_url="", poc_city="", poc_state="", poc_country="")
    sender = f"{current_user.first_name} {current_user.last_name}".strip()
    subject = email_sender._substitute_placeholders(t.subject, sample, sender)
    body = email_sender._substitute_placeholders(t.body, sample, sender)
    if payload.count_use:
        t.usage_count = (t.usage_count or 0) + 1
        db.commit()
    return {"subject": subject, "body": body, "sample": prospect is None}
