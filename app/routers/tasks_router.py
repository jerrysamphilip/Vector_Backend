# app/routers/tasks_router.py
"""
Tasks on contacts and companies, with due date, reminder, owner and priority, and a
"My tasks" queue (BR-CM-15).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.crm import CrmTask
from app.models.prospect import Prospect
from app.models.user import User
from app.services.contact_service import can_access_contact, can_manage_contacts, can_see_owner, clean_str, visible_user_ids

router = APIRouter(prefix="/tasks", tags=["Tasks"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

PRIORITIES = ("LOW", "MEDIUM", "HIGH")
TASK_TYPES = ("TODO", "CALL", "EMAIL", "MEETING")


class TaskWrite(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None
    task_type: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None  # OPEN / DONE
    due_at: Optional[datetime] = None
    reminder_at: Optional[datetime] = None
    owner_id: Optional[str] = None
    prospect_id: Optional[str] = None
    account_id: Optional[str] = None
    opportunity_id: Optional[str] = None


def _naive(value):
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def task_dict(db: Session, t: CrmTask, names: dict = None) -> dict:
    names = names if names is not None else {}
    need = {i for i in (t.owner_id, t.created_by) if i and i not in names}
    if need:
        names.update({u.user_id: f"{u.first_name} {u.last_name}".strip() for u in db.query(User).filter(User.user_id.in_(need))})
    contact = db.query(Prospect).filter(Prospect.prospect_id == t.prospect_id).first() if t.prospect_id else None
    company = db.query(Account.account_id, Account.name).filter(Account.account_id == t.account_id).first() if t.account_id else None
    now = datetime.utcnow()
    return {
        "task_id": t.task_id, "title": t.title, "notes": t.notes, "task_type": t.task_type,
        "priority": t.priority, "status": t.status, "due_at": t.due_at, "reminder_at": t.reminder_at,
        "owner_id": t.owner_id, "owner_name": names.get(t.owner_id), "created_by": t.created_by,
        "created_by_name": names.get(t.created_by), "completed_at": t.completed_at, "created_at": t.created_at,
        "overdue": bool(t.status == "OPEN" and t.due_at and t.due_at < now),
        "reminder_due": bool(t.status == "OPEN" and t.reminder_at and t.reminder_at <= now),
        "prospect_id": t.prospect_id, "contact_name": contact.full_name if contact else None,
        "contact_email": contact.email if contact else None,
        "account_id": t.account_id, "company_name": company.name if company else None,
        "opportunity_id": t.opportunity_id, "opportunity_name": _opp_name(db, t.opportunity_id),
    }


def _opp_name(db: Session, opportunity_id: Optional[str]) -> Optional[str]:
    if not opportunity_id:
        return None
    from app.models.sales import Opportunity
    return db.query(Opportunity.name).filter(Opportunity.opportunity_id == opportunity_id).scalar()


def _check_deal(db: Session, user: User, opportunity_id: Optional[str]):
    """Tasks on a deal (BR-SF-06) follow the deal's visibility."""
    if opportunity_id:
        from app.services.sales import get_opp
        return get_opp(db, user, opportunity_id)
    return None


def _check_targets(db: Session, user: User, prospect_id: Optional[str], account_id: Optional[str]):
    if prospect_id:
        p = db.query(Prospect).filter(Prospect.prospect_id == prospect_id, Prospect.tenant_id == user.tenant_id,
                                      Prospect.deleted_at.is_(None)).first()
        if not p or not can_access_contact(user, p):
            raise HTTPException(status_code=404, detail="Contact not found")
    if account_id:
        account = db.query(Account).filter(Account.account_id == account_id,
                                           Account.tenant_id == user.tenant_id).first()
        from app.routers.accounts_router import _can_see_account
        if not account or not _can_see_account(db, user, account):
            raise HTTPException(status_code=404, detail="Company not found")


def _validate(data: dict, user: User, db: Session):
    if "priority" in data and data["priority"] not in PRIORITIES:
        raise HTTPException(status_code=400, detail=f"priority must be one of {list(PRIORITIES)}")
    if "task_type" in data and data["task_type"] not in TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"task_type must be one of {list(TASK_TYPES)}")
    if "status" in data and data["status"] not in ("OPEN", "DONE"):
        raise HTTPException(status_code=400, detail="status must be OPEN or DONE")
    if data.get("owner_id"):
        if data["owner_id"] != user.user_id and not can_see_owner(db, user, data["owner_id"]):
            raise HTTPException(status_code=403, detail="You can assign tasks only to yourself or your team")
        if not db.query(User.user_id).filter(User.user_id == data["owner_id"], User.tenant_id == user.tenant_id).first():
            raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")


def _get_task(db: Session, user: User, task_id: str) -> CrmTask:
    task = db.query(CrmTask).filter(CrmTask.task_id == task_id, CrmTask.tenant_id == user.tenant_id).first()
    if not task or not (user.user_id in (task.owner_id, task.created_by)
                        or (task.owner_id and can_see_owner(db, user, task.owner_id))):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("")
def list_tasks(
    scope: str = Query("mine", pattern="^(mine|all|created|team)$"),
    status: Optional[str] = Query("OPEN", pattern="^(OPEN|DONE|ALL)$"),
    due: Optional[str] = Query(None, pattern="^(overdue|today|week|none)$"),
    prospect_id: Optional[str] = None,
    account_id: Optional[str] = None,
    opportunity_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    """My tasks queue (default), everyone's tasks (admins), or a record's tasks."""
    query = db.query(CrmTask).filter(CrmTask.tenant_id == current_user.tenant_id)
    if opportunity_id:
        _check_deal(db, current_user, opportunity_id)
        query = query.filter(CrmTask.opportunity_id == opportunity_id)
    elif prospect_id or account_id:
        _check_targets(db, current_user, prospect_id, account_id)
        if prospect_id:
            query = query.filter(CrmTask.prospect_id == prospect_id)
        if account_id:
            query = query.filter(CrmTask.account_id == account_id)
            # A company is shared across teams: show only the tasks of people you can see (BR-SH-02)
            visible = visible_user_ids(db, current_user)
            if visible is not None:
                query = query.filter(or_(CrmTask.owner_id.in_(visible), CrmTask.created_by == current_user.user_id))
    elif scope in ("all", "team"):
        # Everyone's tasks you may see: the whole workspace, or you and your team (BR-SH-02)
        visible = visible_user_ids(db, current_user)
        if visible is not None:
            query = query.filter(CrmTask.owner_id.in_(visible))
    elif scope == "created":
        query = query.filter(CrmTask.created_by == current_user.user_id)
    else:
        query = query.filter(CrmTask.owner_id == current_user.user_id)
    if status != "ALL":
        query = query.filter(CrmTask.status == status)
    now = datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59)
    if due == "overdue":
        query = query.filter(CrmTask.due_at < now)
    elif due == "today":
        query = query.filter(CrmTask.due_at <= today_end)
    elif due == "week":
        query = query.filter(CrmTask.due_at <= today_end + timedelta(days=7))
    elif due == "none":
        query = query.filter(CrmTask.due_at.is_(None))
    total = query.count()
    priority_rank = case({"HIGH": 0, "MEDIUM": 1, "LOW": 2}, value=CrmTask.priority, else_=3)
    rows = query.order_by(CrmTask.status.desc(), CrmTask.due_at.is_(None), CrmTask.due_at.asc(), priority_rank,
                          CrmTask.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    names: dict = {}
    counts = db.query(CrmTask).filter(CrmTask.tenant_id == current_user.tenant_id,
                                      CrmTask.owner_id == current_user.user_id, CrmTask.status == "OPEN")
    return {"items": [task_dict(db, t, names) for t in rows], "total": total, "page": page, "page_size": page_size,
            "my_open": counts.count(), "my_overdue": counts.filter(CrmTask.due_at < now).count(),
            "my_reminders_due": counts.filter(CrmTask.reminder_at <= now).count()}


@router.post("", status_code=201)
def create_task(payload: TaskWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    data = payload.model_dump(exclude_unset=True)
    if not clean_str(data.get("title")):
        raise HTTPException(status_code=400, detail="Give the task a title")
    _validate(data, current_user, db)
    _check_targets(db, current_user, data.get("prospect_id"), data.get("account_id"))
    deal = _check_deal(db, current_user, data.get("opportunity_id"))
    if deal:  # a deal task also shows on its contact and company
        data.setdefault("prospect_id", deal.prospect_id)
        data.setdefault("account_id", deal.account_id)
    task = CrmTask(tenant_id=current_user.tenant_id, title=clean_str(data["title"]), notes=clean_str(data.get("notes")),
                   task_type=data.get("task_type") or "TODO", priority=data.get("priority") or "MEDIUM",
                   due_at=_naive(data.get("due_at")), reminder_at=_naive(data.get("reminder_at")),
                   owner_id=data.get("owner_id") or current_user.user_id, created_by=current_user.user_id,
                   prospect_id=data.get("prospect_id"), account_id=data.get("account_id"),
                   opportunity_id=data.get("opportunity_id"))
    db.add(task)
    if deal:
        deal.updated_at = datetime.utcnow()
    if task.owner_id != current_user.user_id:
        from app.services.notifications import notify
        notify(db, current_user.tenant_id, [task.owner_id], "TASK", f"New task: {task.title}",
               f"Assigned by {current_user.first_name} {current_user.last_name}".strip(),
               f"/app/deals/{deal.opportunity_id}" if deal else "/app/tasks")
    db.commit()
    return task_dict(db, task)


@router.patch("/{task_id}")
def update_task(task_id: str, payload: TaskWrite, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    task = _get_task(db, current_user, task_id)
    data = payload.model_dump(exclude_unset=True)
    data.pop("prospect_id", None)
    data.pop("account_id", None)
    data.pop("opportunity_id", None)
    _validate(data, current_user, db)
    for field, value in data.items():
        if field in ("due_at", "reminder_at"):
            value = _naive(value)
        elif isinstance(value, str):
            value = clean_str(value)
        if field == "title" and not value:
            raise HTTPException(status_code=400, detail="Give the task a title")
        setattr(task, field, value)
    if "status" in data:
        task.completed_at = datetime.utcnow() if task.status == "DONE" else None
    db.commit()
    return task_dict(db, task)


@router.delete("/{task_id}")
def delete_task(task_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    task = _get_task(db, current_user, task_id)
    db.delete(task)
    db.commit()
    return {"status": "deleted", "task_id": task_id}
