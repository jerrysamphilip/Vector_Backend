"""
Request / response models of the campaign drafts API (app/routers/campaign_draft_router.py). Moved out of
the router unchanged (chunk A2); the OpenAPI schema is the same.
"""

from typing import Any, Optional

from pydantic import BaseModel
from app.schemas.types import UtcDatetime

class SaveDraftRequest(BaseModel):
    """Request to save campaign draft state."""
    draft_name: Optional[str] = None
    current_step: int = 1
    draft_data: dict  # Full wizard state as JSON

class DraftResponse(BaseModel):
    """Response for single draft."""
    draft_id: str
    draft_name: Optional[str]
    current_step: int
    draft_data: dict[str, Any]
    created_at: Optional[UtcDatetime] = None
    updated_at: Optional[UtcDatetime] = None

    class Config:
        from_attributes = True
