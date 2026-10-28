"""Request models of the outbound webhooks API (BR-SF-20), moved out of app/routers/webhooks_router.py."""
from typing import List, Optional

from pydantic import BaseModel, Field

from app.utils import webhooks as wh


class EndpointCreate(BaseModel):
    url: str = Field(..., max_length=wh.MAX_URL_LENGTH)
    description: Optional[str] = Field(None, max_length=255)
    event_types: List[str] = Field(..., min_length=1, max_length=len(wh.EVENT_TYPES))
    active: bool = True


class EndpointUpdate(BaseModel):
    url: Optional[str] = Field(None, max_length=wh.MAX_URL_LENGTH)
    description: Optional[str] = Field(None, max_length=255)
    event_types: Optional[List[str]] = Field(None, min_length=1, max_length=len(wh.EVENT_TYPES))
    active: Optional[bool] = None
