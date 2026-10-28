# app/routers/tasks_router.py
"""
Tasks on contacts and companies, with due date, reminder, owner and priority, and a
"My tasks" queue (BR-CM-15).

Layered like contacts_router (ADR 0009): parse into a TaskFilter / Paging, one call to TaskService.
``task_dict`` stays importable from here for the deal timeline (app/routers/sales_router.py).
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.contacts import TASK_TYPES, TaskFilter
from app.domain.paging import Paging
from app.schemas.task_schema import TaskWrite
from app.services.task_service import PRIORITIES, TaskService, task_dict

__all__ = ["PRIORITIES", "TASK_TYPES", "TaskWrite", "router", "task_dict"]

router = APIRouter(prefix="/tasks", tags=["Tasks"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def task_service(db=Depends(get_db)) -> TaskService:
    return TaskService.for_session(db)


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
    service: TaskService = Depends(task_service),
    current_user: Any = Depends(tenant_user),
):
    """My tasks queue (default), everyone's tasks (admins), or a record's tasks."""
    f = TaskFilter(scope=scope, status=status, due=due, prospect_id=prospect_id, account_id=account_id,
                   opportunity_id=opportunity_id)
    return service.list(current_user, f, Paging(page, page_size))


@router.post("", status_code=201)
def create_task(payload: TaskWrite, service: TaskService = Depends(task_service),
                current_user: Any = Depends(tenant_user)):
    return service.create(current_user, payload)


@router.patch("/{task_id}")
def update_task(task_id: str, payload: TaskWrite, service: TaskService = Depends(task_service),
                current_user: Any = Depends(tenant_user)):
    return service.update(current_user, task_id, payload)


@router.delete("/{task_id}")
def delete_task(task_id: str, service: TaskService = Depends(task_service), current_user: Any = Depends(tenant_user)):
    return service.delete(current_user, task_id)
