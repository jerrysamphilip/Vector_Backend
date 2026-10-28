# app/schemas/campaign_schema.py
"""
Pydantic schemas for Campaign API.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Dict, Literal, Optional, List
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
    wait_business_days: bool = Field(False, description="Wait days between steps count business days (F-04)")

    # Sending Schedule
    sending_mode: str = Field("spread", description="spread | random | batch")
    min_gap_minutes: int = Field(2, ge=0, le=10080, description="Min gap between emails (random mode)")
    batch_size: Optional[int] = Field(None, ge=1, le=100000, description="Emails per batch (batch mode)")
    batch_gap_minutes: int = Field(30, ge=1, le=10080, description="Minutes between batch starts (batch mode)")

    # Daily batch throttle (optional)
    daily_batch_size: Optional[int] = Field(None, ge=1, le=1000000, description="Max new prospects to email per day (e.g. 100 → day 1: 1-100, day 2: 101-200)")

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
    wait_business_days: Optional[bool] = Field(None, description="Wait days count business days (F-04)")
    sending_mode: Optional[str] = None
    min_gap_minutes: Optional[int] = Field(None, ge=0, le=10080)
    batch_size: Optional[int] = Field(None, ge=1, le=100000)
    batch_gap_minutes: Optional[int] = Field(None, ge=1, le=10080)
    unsubscribe_mode: Optional[str] = Field(None, description="'plain' or 'html' — controls unsubscribe footer style")
    daily_batch_size: Optional[int] = Field(None, ge=1, le=1000000, description="Max new prospects to email per day")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    inbox_ids: Optional[List[str]] = Field(None, description="Update list of Sending Inboxes")
    expected_version: Optional[int] = Field(None, ge=1, description="row_version the client loaded (T-3)")

    @field_validator("campaign_name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("campaign_name must not be blank")
        return v

    @model_validator(mode="after")
    def _dates_in_order(self) -> "CampaignUpdate":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self

    def changes(self) -> Dict[str, Any]:
        """
        The fields this request changes (S-23): a field left out is unchanged; an explicit null
        clears a field in CLEARABLE_FIELDS and is ignored for the others (they cannot be empty).
        ``inbox_ids`` and ``expected_version`` are not included (handled separately).
        """
        out: Dict[str, Any] = {}
        for name in self.model_fields_set:
            if name in ("inbox_ids", "expected_version"):
                continue
            value = getattr(self, name)
            if value is None and name not in CLEARABLE_FIELDS:
                continue
            out[name] = value
        return out


# Campaign settings an update may clear with an explicit null (S-23); every other field ignores null
CLEARABLE_FIELDS = frozenset({
    "sender_name", "sender_title", "cta_link", "batch_size", "daily_batch_size", "start_date", "end_date",
})


class CampaignVersionedAction(BaseModel):
    """Optional body of launch / pause / resume: the row_version the client loaded (T-3)."""

    expected_version: Optional[int] = Field(None, ge=1)


class CampaignFilter(BaseModel):
    """Schema for filtering campaigns."""
    
    status: Optional[CampaignStatus] = Field(None, description="Filter by status")
    created_by: Optional[str] = Field(None, description="Filter by creator user ID")
    campaign_name: Optional[str] = Field(None, description="Search by campaign name (partial match)")
    
    # Pagination
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(20, ge=1, le=100, description="Items per page")


class CampaignDuplicateRequest(BaseModel):
    """POST /campaigns/{id}/duplicates: optional overrides for the copy (S-12). Unset = the source's value."""

    campaign_name: Optional[str] = Field(None, min_length=1, max_length=255,
                                         description="Name of the copy (default 'Copy of <source>')")
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    send_window_start: Optional[time] = None
    send_window_end: Optional[time] = None
    campaign_timezone: Optional[str] = Field(None, max_length=50)
    inbox_ids: Optional[List[str]] = Field(None, description="Mailboxes of the copy (default: the source's usable ones)")
    list_ids: Optional[List[str]] = Field(None, description="Lists to enrol into the copy")
    prospect_ids: Optional[List[str]] = Field(None, description="Contacts to enrol into the copy")
    skip_source_prospects: bool = Field(True, description="Do not enrol contacts already in the source campaign")
    copy_automation_rules: bool = True
    copy_attachments: bool = True

    @field_validator("campaign_name")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else v

    @model_validator(mode="after")
    def _ordered(self) -> "CampaignDuplicateRequest":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.send_window_start and self.send_window_end and self.send_window_end <= self.send_window_start:
            raise ValueError("send_window_end must be after send_window_start")
        return self


class EnrollmentCheckRequest(BaseModel):
    """
    POST /campaigns/{id}/enrollments/checks: who would be enrolled and why the others would not,
    without enrolling anyone (S-20). ``path`` picks the rules: ``campaign`` = POST /campaigns/{id}/prospects
    (list_ids / prospect_ids, 24h cool-off), ``wizard`` = POST /campaigns/{id}/enrollments (list_id +
    segmentation, cool_off_days).
    """

    path: Literal["campaign", "wizard"] = "campaign"
    list_ids: Optional[List[str]] = None
    prospect_ids: Optional[List[str]] = None
    list_id: Optional[str] = None
    persona_types: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    cool_off_days: int = Field(7, ge=0, le=3650)
    excluded_prospect_ids: Optional[List[str]] = None

    @model_validator(mode="after")
    def _has_source(self) -> "EnrollmentCheckRequest":
        if self.path == "wizard" and not self.list_id:
            raise ValueError("list_id is required for the wizard check")
        return self


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
    row_version: int = 1  # send back as expected_version on update / launch / pause / resume (T-3)
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
    wait_business_days: bool = False  # wait days count business days (F-04)
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
    # The campaign this one was duplicated from (S-12)
    source_campaign_id: Optional[str] = None

    class Config:
        from_attributes = True


class PersonaGap(BaseModel):
    """A persona of the enrolled contacts with no template (own or generic) for some steps."""

    persona_type: str
    prospect_count: int
    missing_steps: List[int]


class CampaignDuplicateResponse(BaseModel):
    campaign: CampaignResponse
    source_campaign_id: str
    copied: Dict[str, int]
    skipped_inboxes: List[str] = []
    enrollment: Optional[Dict[str, Any]] = None
    persona_gaps: List[PersonaGap] = []


class CampaignListItem(BaseModel):
    """Simplified campaign for list display."""
    
    campaign_id: str
    campaign_name: str
    cta_link: Optional[str] = None
    status: CampaignStatus
    row_version: int = 1  # T-3
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
    row_version: Optional[int] = None  # the campaign's new row_version (T-3)
    # Launch only: the planned volume exceeds what the mailboxes can send in the window (F-04)
    pacing_warning: Optional[Dict[str, Any]] = None
