"""Request bodies of the tasks API (app/routers/tasks_router.py, BR-CM-15)."""

from typing import Optional

from pydantic import BaseModel, Field
from app.schemas.types import UtcDatetime


class TaskWrite(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    notes: Optional[str] = None
    task_type: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None  # OPEN / DONE
    due_at: Optional[UtcDatetime] = None
    reminder_at: Optional[UtcDatetime] = None
    owner_id: Optional[str] = None
    prospect_id: Optional[str] = None
    account_id: Optional[str] = None
    opportunity_id: Optional[str] = None
