"""
Tasks on contacts, companies and deals, with due date, reminder, owner and priority, and a
"My tasks" queue (BR-CM-15). A task is visible to its owner and creator and to whoever may see the
owner's records (BR-SH-02); tasks on a deal follow the deal's visibility (BR-SF-06).

``TaskService`` serves app/routers/tasks_router.py, one unit of work per call; SQL is in
app/repositories/tasks.py and app/repositories/contacts.py.
"""

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.core.clock import utcnow
from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.domain.contacts import TASK_PRIORITIES, TASK_TYPES, TaskFilter
from app.domain.paging import Paging
from app.models.crm import CrmTask
from app.models.user import User
from app.repositories.contacts import ContactsUnitOfWork, Session, contacts_uow
from app.repositories.tasks import SqlAlchemyTaskRepository, TaskRepository
from app.schemas.task_schema import TaskWrite
from app.services import contact_scoring, sales
from app.services.contact_service import (
    can_access_contact,
    can_see_account,
    can_see_owner,
    clean_str,
    visible_user_ids,
)
from app.services.notifications import notify

PRIORITIES = TASK_PRIORITIES


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def task_card(repo: TaskRepository, t: CrmTask, names: Optional[dict] = None) -> dict:
    """A task as the API shows it, with owner / creator names and its contact, company and deal."""
    names = names if names is not None else {}
    need = {i for i in (t.owner_id, t.created_by) if i and i not in names}
    if need:
        names.update(repo.user_names(need))
    contact = repo.contact(t.tenant_id, t.prospect_id) if t.prospect_id else None
    now = utcnow()
    return {
        "task_id": t.task_id, "title": t.title, "notes": t.notes, "task_type": t.task_type,
        "priority": t.priority, "status": t.status, "due_at": t.due_at, "reminder_at": t.reminder_at,
        "owner_id": t.owner_id, "owner_name": names.get(t.owner_id), "created_by": t.created_by,
        "created_by_name": names.get(t.created_by), "completed_at": t.completed_at, "created_at": t.created_at,
        "overdue": bool(t.status == "OPEN" and t.due_at and t.due_at < now),
        "reminder_due": bool(t.status == "OPEN" and t.reminder_at and t.reminder_at <= now),
        "prospect_id": t.prospect_id, "contact_name": contact.full_name if contact else None,
        "contact_email": contact.email if contact else None,
        "account_id": t.account_id,
        "company_name": repo.company_name(t.tenant_id, t.account_id) if t.account_id else None,
        "opportunity_id": t.opportunity_id,
        "opportunity_name": repo.opportunity_name(t.opportunity_id) if t.opportunity_id else None,
    }


def task_dict(db: Session, t: CrmTask, names: Optional[dict] = None) -> dict:
    """``task_card`` on a caller's session (the contact and deal timelines render tasks with it)."""
    return task_card(SqlAlchemyTaskRepository(db), t, names)


class TaskService:
    def __init__(self, uow: ContactsUnitOfWork,
                 tasks: Callable[[Session], TaskRepository] = SqlAlchemyTaskRepository) -> None:
        self.uow = uow
        self._task_repo = tasks

    @classmethod
    def for_session(cls, db: Session) -> "TaskService":
        return cls(contacts_uow(db))

    # ── checks ────────────────────────────────────────────────────
    @staticmethod
    def _check_deal(db: Session, user: User, opportunity_id: Optional[str]) -> Any:
        """Tasks on a deal (BR-SF-06) follow the deal's visibility."""
        return sales.get_opp(db, user, opportunity_id) if opportunity_id else None

    @staticmethod
    def _check_targets(uow: ContactsUnitOfWork, user: User, prospect_id: Optional[str],
                       account_id: Optional[str]) -> None:
        if prospect_id:
            p = uow.contacts.get_live(user.tenant_id, prospect_id)
            if not p or not can_access_contact(user, p):
                raise NotFoundError("Contact not found")
        if account_id:
            account = uow.contacts.account(user.tenant_id, account_id)
            if not account or not can_see_account(uow.session, user, account):
                raise NotFoundError("Company not found")

    @staticmethod
    def _validate(uow: ContactsUnitOfWork, data: dict, user: User) -> None:
        if "priority" in data and data["priority"] not in PRIORITIES:
            raise BadRequestError(f"priority must be one of {list(PRIORITIES)}")
        if "task_type" in data and data["task_type"] not in TASK_TYPES:
            raise BadRequestError(f"task_type must be one of {list(TASK_TYPES)}")
        if "status" in data and data["status"] not in ("OPEN", "DONE"):
            raise BadRequestError("status must be OPEN or DONE")
        if data.get("owner_id"):
            if data["owner_id"] != user.user_id and not can_see_owner(uow.session, user, data["owner_id"]):
                raise PermissionDenied("You can assign tasks only to yourself or your team")
            if not uow.contacts.user_in_tenant(user.tenant_id, data["owner_id"]):
                raise BadRequestError("Owner must be a user in your workspace")

    @staticmethod
    def _get_task(uow: ContactsUnitOfWork, repo: TaskRepository, user: User, task_id: str) -> CrmTask:
        task = repo.get(user.tenant_id, task_id)
        if not task or not (user.user_id in (task.owner_id, task.created_by)
                            or (task.owner_id and can_see_owner(uow.session, user, task.owner_id))):
            raise NotFoundError("Task not found")
        return task

    def _tasks(self, uow: ContactsUnitOfWork) -> TaskRepository:
        return self._task_repo(uow.session)

    # ── endpoints ─────────────────────────────────────────────────
    def list(self, user: User, f: TaskFilter, paging: Paging) -> dict:
        """My tasks queue (default), everyone's tasks (admins), or a record's tasks."""
        with self.uow as uow:
            db, repo = uow.session, self._tasks(uow)
            if f.opportunity_id:
                self._check_deal(db, user, f.opportunity_id)
                needs_visible = False
            elif f.prospect_id or f.account_id:
                self._check_targets(uow, user, f.prospect_id, f.account_id)
                needs_visible = bool(f.account_id)
            else:
                needs_visible = f.scope in ("all", "team")
            visible = visible_user_ids(db, user) if needs_visible else None
            now = utcnow()
            page = repo.page(user.tenant_id, f, user.user_id, visible, now, paging)
            names: dict = {}
            my_open, my_overdue, my_reminders = repo.my_counts(user.tenant_id, user.user_id, now)
            return {"items": [task_card(repo, t, names) for t in page.items], "total": page.total,
                    "page": paging.page, "page_size": paging.page_size,
                    "my_open": my_open, "my_overdue": my_overdue, "my_reminders_due": my_reminders}

    def create(self, user: User, payload: TaskWrite) -> dict:
        data = payload.model_dump(exclude_unset=True)
        if not clean_str(data.get("title")):
            raise BadRequestError("Give the task a title")
        with self.uow as uow:
            db, repo = uow.session, self._tasks(uow)
            self._validate(uow, data, user)
            self._check_targets(uow, user, data.get("prospect_id"), data.get("account_id"))
            deal = self._check_deal(db, user, data.get("opportunity_id"))
            if deal:  # a deal task also shows on its contact and company
                data.setdefault("prospect_id", deal.prospect_id)
                data.setdefault("account_id", deal.account_id)
            task = CrmTask(tenant_id=user.tenant_id, title=clean_str(data["title"]), notes=clean_str(data.get("notes")),
                           task_type=data.get("task_type") or "TODO", priority=data.get("priority") or "MEDIUM",
                           due_at=_naive(data.get("due_at")), reminder_at=_naive(data.get("reminder_at")),
                           owner_id=data.get("owner_id") or user.user_id, created_by=user.user_id,
                           prospect_id=data.get("prospect_id"), account_id=data.get("account_id"),
                           opportunity_id=data.get("opportunity_id"))
            repo.add(user.tenant_id, task)
            if deal:
                deal.updated_at = utcnow()
            if task.owner_id != user.user_id:
                notify(db, user.tenant_id, [task.owner_id], "TASK", f"New task: {task.title}",
                       f"Assigned by {user.first_name} {user.last_name}".strip(),
                       f"/app/deals/{deal.opportunity_id}" if deal else "/app/tasks")
            uow.commit()
            return task_card(repo, task)

    def update(self, user: User, task_id: str, payload: TaskWrite) -> dict:
        with self.uow as uow:
            repo = self._tasks(uow)
            task = self._get_task(uow, repo, user, task_id)
            data = payload.model_dump(exclude_unset=True)
            for record in ("prospect_id", "account_id", "opportunity_id"):
                data.pop(record, None)
            self._validate(uow, data, user)
            for field, value in data.items():
                if field in ("due_at", "reminder_at"):
                    value = _naive(value)
                elif isinstance(value, str):
                    value = clean_str(value)
                if field == "title" and not value:
                    raise BadRequestError("Give the task a title")
                setattr(task, field, value)
            if "status" in data:
                task.completed_at = utcnow() if task.status == "DONE" else None
            uow.commit()
            if "status" in data and task.prospect_id:
                # Completed tasks count towards the contact's engagement score (BR-CM-43)
                contact_scoring.rescore(uow.session, task.tenant_id, [task.prospect_id])
            return task_card(repo, task)

    def delete(self, user: User, task_id: str) -> dict:
        with self.uow as uow:
            repo = self._tasks(uow)
            repo.remove(self._get_task(uow, repo, user, task_id))
            uow.commit()
        return {"status": "deleted", "task_id": task_id}
