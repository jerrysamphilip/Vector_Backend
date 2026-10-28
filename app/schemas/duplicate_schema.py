"""Request models of the duplicate review and merge preview API (app/routers/duplicates_router.py)."""

from typing import List

from pydantic import BaseModel, Field


class MergePreviewRequest(BaseModel):
    primary_id: str
    duplicate_ids: List[str] = Field(..., min_length=1, max_length=20)
