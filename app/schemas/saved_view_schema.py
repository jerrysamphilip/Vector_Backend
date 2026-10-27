"""Request body of the saved views API (app/routers/crm_router.py, BR-CM-19)."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class ViewWrite(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    object_type: Optional[str] = None
    shared: Optional[bool] = None
    filters: Optional[Any] = None
    columns: Optional[list] = None
    sort: Optional[dict] = None
