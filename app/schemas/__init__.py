# app/schemas/__init__.py
"""
Pydantic schemas for request/response validation.
"""

from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignUpdate,
    CampaignResponse,
    CampaignListResponse,
    CampaignFilter,
)
from app.schemas.sequence_schema import (
    SequenceStepCreate,
    SequenceStepUpdate,
    SequenceStepResponse,
)
from app.schemas.template_schema import (
    EmailTemplateCreate,
    EmailTemplateUpdate,
    EmailTemplateResponse,
)

__all__ = [
    "CampaignCreate",
    "CampaignUpdate", 
    "CampaignResponse",
    "CampaignListResponse",
    "CampaignFilter",
    "SequenceStepCreate",
    "SequenceStepUpdate",
    "SequenceStepResponse",
    "EmailTemplateCreate",
    "EmailTemplateUpdate",
    "EmailTemplateResponse",
]
