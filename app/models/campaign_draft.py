# app/models/campaign_draft.py
"""
Campaign Draft Model.
Stores incomplete campaign creation state for resume functionality.
"""

from sqlalchemy import Column, String, Integer, DateTime, Text, JSON
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class CampaignDraft(Base):
    """
    Stores the state of an incomplete campaign creation wizard.
    Enables users to save progress and resume later.
    """
    __tablename__ = "campaign_drafts"

    draft_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    
    # Draft metadata
    draft_name = Column(String(255), nullable=True)  # Auto-generated from campaign name
    current_step = Column(Integer, default=1)
    
    # Full wizard state as JSON
    draft_data = Column(JSON, nullable=False, default=dict)
    # Stores: formData, selectedLists, sequenceSteps, processingResult, selectedPersona, etc.
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    def __repr__(self):
        return f"<CampaignDraft {self.draft_id} - Step {self.current_step}>"
