# app/models/campaign_draft.py
"""
Campaign Draft Model.
Stores incomplete campaign creation state for resume functionality.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class CampaignDraft(Base):
    """
    Stores the state of an incomplete campaign creation wizard.
    Enables users to save progress and resume later.
    """
    __tablename__ = "campaign_drafts"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # Draft metadata
    draft_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Auto-generated from campaign name
    current_step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=1)

    # Full wizard state as JSON
    # Stores: formData, selectedLists, sequenceSteps, processingResult, selectedPersona, etc.
    draft_data: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)

    # Timestamps
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)

    def __repr__(self) -> str:
        return f"<CampaignDraft {self.draft_id} - Step {self.current_step}>"
