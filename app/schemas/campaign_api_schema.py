"""
Response / request models of the campaign API (app/routers/campaign_router.py): analytics, templates,
schedule, regeneration, approval, rescheduling and bounce re-validation. Moved out of the router
unchanged (chunk A2); the OpenAPI schema is the same.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from app.schemas.types import UtcDatetime

class DailyMetrics(BaseModel):
    date: str
    sent: int = 0
    opened: int = 0
    replied: int = 0
    bounced: int = 0
    sender_bounced: int = 0
    positive_replied: int = 0
    ooo: int = 0

class SequenceMetrics(BaseModel):
    step: int
    type: str = "Email"
    sent: int = 0
    opened_count: int = 0
    replied_count: int = 0
    bounced_count: int = 0
    open_rate: float = 0.0
    click_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0
    positive_replied_count: int = 0
    ooo_count: int = 0
    sender_bounced_count: int = 0

class LeadStatsItem(BaseModel):
    name: str
    value: int


class TemplateInfo(BaseModel):
    template_id: str
    sequence_id: Optional[str] = None
    step_number: Optional[int] = None
    designation: Optional[str] = None
    subject: str
    body: str
    tone: Optional[str] = None
    ai_model: Optional[str] = None
    is_ai_generated: bool = False
    is_approved: bool = False
    approved_at: Optional[UtcDatetime] = None
    created_at: UtcDatetime

class CampaignTemplatesResponse(BaseModel):
    campaign_id: str
    templates: List[TemplateInfo]
    total: int

class ScheduleItem(BaseModel):
    date: str
    status: str
    prospect_count: int
    step_number: int
    prospects: List[dict] = []

class CampaignScheduleResponse(BaseModel):
    campaign_id: str
    schedule: List[ScheduleItem]
    total_scheduled: int
    total_sent: int

class RegenerateResponse(BaseModel):
    template_id: str
    subject: str
    body: str
    ai_model: str
    regenerated_at: UtcDatetime

class ApproveResponse(BaseModel):
    template_id: str
    is_approved: bool
    approved_at: Optional[UtcDatetime] = None

class ApproveAllResponse(BaseModel):
    approved_count: int
    template_ids: List[str]

class RescheduleRequest(BaseModel):
    """
    Request body for rescheduling a queued/scheduled email message.
    new_scheduled_at should be a UTC datetime string, e.g. '2026-03-15T09:00:00'.
    """
    new_scheduled_at: UtcDatetime
    reason: Optional[str] = None

class RescheduleDateRequest(BaseModel):
    """
    Bulk-reschedule all QUEUED messages on a given date + sequence step.
    date            : ISO date string 'YYYY-MM-DD'
    step_number     : sequence step to target
    new_scheduled_at: user's desired UTC datetime (treated as local campaign-TZ wall-clock)
    """
    date: str
    step_number: int
    new_scheduled_at: datetime  # campaign-zone wall clock, so not UtcDatetime
    reason: Optional[str] = None

class RevalidateProspectRequest(BaseModel):
    new_email: str = Field(..., description="The prospect's updated, valid email address")
    reason: str = Field(..., min_length=5, description="Why was the email changed? (audit trail)")
