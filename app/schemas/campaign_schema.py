# app/schemas/campaign_schema.py
"""
Pydantic schemas for Campaign API.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime, date, time
from enum import Enum


class CampaignStatus(str, Enum):
    """Campaign status enum."""
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


# ===========================
# REQUEST SCHEMAS
# ===========================

class CampaignCreate(BaseModel):
    """Schema for creating a new campaign."""
    
    campaign_name: str = Field(..., min_length=1, max_length=255, description="Campaign name")
    sender_name: Optional[str] = Field(None, max_length=100, description="Name to use for email sign-off")
    sender_title: Optional[str] = Field(None, max_length=150, description="Sender's title/role, shown in the email signature")
    cta_link: Optional[str] = Field(None, max_length=500, description="Calendly or booking URL for emails")
    
    # Send Window (optional)
    send_window_start: Optional[time] = Field(None, description="Start time for sending emails")
    send_window_end: Optional[time] = Field(None, description="End time for sending emails")
    respect_timezone: bool = Field(True, description="Respect prospect's timezone")
    campaign_timezone: str = Field("UTC", description="Campaign timezone")

    # Sending Schedule
    sending_mode: str = Field("spread", description="spread | random | batch")
    min_gap_minutes: int = Field(2, ge=0, description="Min gap between emails (random mode)")
    batch_size: Optional[int] = Field(None, ge=1, description="Emails per batch (batch mode)")
    batch_gap_minutes: int = Field(30, ge=1, description="Minutes between batch starts (batch mode)")

    # Daily batch throttle (optional)
    daily_batch_size: Optional[int] = Field(None, ge=1, description="Max new prospects to email per day (e.g. 100 → day 1: 1-100, day 2: 101-200)")

    # Campaign Duration (optional)
    start_date: Optional[date] = Field(None, description="Campaign start date")
    end_date: Optional[date] = Field(None, description="Campaign end date")
    inbox_ids: Optional[List[str]] = Field(None, description="List of Sending Inbox IDs to rotate")
    
    @field_validator("campaign_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return v.strip()
    
    class Config:
        json_schema_extra = {
            "example": {
                "campaign_name": "Winter Promo 2025",
                "inbox_ids": ["i001", "i002"],
                "send_window_start": "09:00:00",
                "send_window_end": "17:00:00",
                "respect_timezone": True,
                "start_date": "2025-01-15",
                "end_date": "2025-02-15"
            }
        }


class CampaignUpdate(BaseModel):
    """Schema for updating an existing campaign."""

    campaign_name: Optional[str] = Field(None, min_length=1, max_length=255)
    sender_name: Optional[str] = Field(None, max_length=100)
    sender_title: Optional[str] = Field(None, max_length=150)
    cta_link: Optional[str] = Field(None, max_length=500)
    send_window_start: Optional[time] = None
    send_window_end: Optional[time] = None
    respect_timezone: Optional[bool] = None
    campaign_timezone: Optional[str] = None
    sending_mode: Optional[str] = None
    min_gap_minutes: Optional[int] = None
    batch_size: Optional[int] = None
    batch_gap_minutes: Optional[int] = None
    unsubscribe_mode: Optional[str] = Field(None, description="'plain' or 'html' — controls unsubscribe footer style")
    daily_batch_size: Optional[int] = Field(None, ge=1, description="Max new prospects to email per day")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    inbox_ids: Optional[List[str]] = Field(None, description="Update list of Sending Inboxes")


class CampaignFilter(BaseModel):
    """Schema for filtering campaigns."""
    
    status: Optional[CampaignStatus] = Field(None, description="Filter by status")
    created_by: Optional[str] = Field(None, description="Filter by creator user ID")
    campaign_name: Optional[str] = Field(None, description="Search by campaign name (partial match)")
    
    # Pagination
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(20, ge=1, le=100, description="Items per page")


class CampaignEnrollmentRequest(BaseModel):
    """Schema for enrolling prospects into a campaign."""
    
    list_ids: Optional[List[str]] = Field(None, description="List of Prospect List IDs to enroll")
    prospect_ids: Optional[List[str]] = Field(None, description="List of specific Prospect IDs to enroll")
    
    @field_validator("list_ids", "prospect_ids")
    @classmethod
    def validate_input(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return v if v else None
    
    class Config:
        json_schema_extra = {
            "example": {
                "list_ids": ["l0000000-0000-0000-0000-000000000001"],
                "prospect_ids": []
            }
        }


# ===========================
# RESPONSE SCHEMAS
# ===========================

class CampaignMetrics(BaseModel):
    """Campaign metrics for dashboard display."""
    
    sent_count: int = 0
    opened_count: int = 0
    replied_count: int = 0
    bounced_count: int = 0
    sender_bounced_count: int = 0
    positive_replied_count: int = 0
    ooo_count: int = 0
    unsubscribed_count: int = 0

    @property
    def open_rate(self) -> float:
        if self.sent_count == 0:
            return 0.0
        return round((self.opened_count / self.sent_count) * 100, 2)
    
    @property
    def reply_rate(self) -> float:
        if self.sent_count == 0:
            return 0.0
        return round((self.replied_count / self.sent_count) * 100, 2)
    
    class Config:
        from_attributes = True


class CampaignResponse(BaseModel):
    """Full campaign response with metrics."""
    
    campaign_id: str
    tenant_id: str
    campaign_name: str
    sender_name: Optional[str] = None
    sender_title: Optional[str] = None
    cta_link: Optional[str] = None
    status: CampaignStatus
    # Why it is paused; auto_paused = paused by the deliverability guard (BR-DF-07)
    paused_reason: Optional[str] = None
    paused_at: Optional[datetime] = None
    auto_paused: Optional[bool] = False
    
    created_by: str
    creator_name: Optional[str] = None
    
    send_window_start: Optional[time] = None
    send_window_end: Optional[time] = None
    respect_timezone: bool = True
    campaign_timezone: str = "UTC"
    sending_mode: str = "spread"
    min_gap_minutes: int = 2
    batch_size: Optional[int] = None
    batch_gap_minutes: int = 30
    unsubscribe_mode: str = "plain"
    daily_batch_size: Optional[int] = None

    start_date: Optional[date] = None
    end_date: Optional[date] = None

    created_at: datetime
    updated_at: datetime
    
    # Metrics (optional, loaded separately)
    metrics: Optional[CampaignMetrics] = None
    
    # Sequence step count
    sequence_count: int = 0
    prospect_count: int = 0
    
    # Assigned sending inboxes (serialized for frontend)
    sending_inboxes: List[dict] = []

    class Config:
        from_attributes = True


class CampaignListItem(BaseModel):
    """Simplified campaign for list display."""
    
    campaign_id: str
    campaign_name: str
    cta_link: Optional[str] = None
    status: CampaignStatus
    created_by: str
    creator_name: Optional[str] = None
    
    # Key metrics
    sent_count: int = 0
    opened_count: int = 0
    replied_count: int = 0
    bounced_count: int = 0
    sender_bounced_count: int = 0
    positive_replied_count: int = 0
    ooo_count: int = 0
    unsubscribed_count: int = 0
    in_progress_count: int = 0
    prospect_count: int = 0

    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class CampaignListResponse(BaseModel):
    """Paginated list of campaigns."""
    
    items: List[CampaignListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class CampaignStateChange(BaseModel):
    """Response for pause/resume operations."""
    
    campaign_id: str
    previous_status: CampaignStatus
    new_status: CampaignStatus
    reason: Optional[str] = None
    changed_at: datetime
