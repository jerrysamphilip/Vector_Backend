
# app/schemas/deliverability_schema.py
"""
Pydantic schemas for Deliverability and Domain Reputation.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Any
from app.schemas.types import UtcDatetime

# Domain Schemas
class SendingDomainBase(BaseModel):
    domain_name: str
    domain_id: Optional[str] = None  # the tenant's record; the same name can exist for other tenants
    spf_status: str = "UNKNOWN"
    dkim_status: str = "UNKNOWN"
    dmarc_status: str = "UNKNOWN"
    current_reputation_score: int = 100
    is_blacklisted: bool = False
    warmup_status: str = "COLD"
    ses_reputation_status: Optional[str] = "UNKNOWN"
    sesv2_enabled: Optional[bool] = False
    account_reputation_score: Optional[float] = 1.0

class SendingDomainResponse(SendingDomainBase):
    created_at: UtcDatetime
    updated_at: UtcDatetime
    
    # Associations
    associated_inboxes: list[str] = []
    active_campaigns: list[str] = []

    class Config:
        from_attributes = True

# Alert Schemas
class ReputationAlertResponse(BaseModel):
    alert_id: str
    domain_name: str
    alert_type: str
    severity: str
    details: Optional[str] = None
    is_resolved: bool
    created_at: UtcDatetime
    
    class Config:
        from_attributes = True

# Statistics Schema
class SESDataPoint(BaseModel):
    Timestamp: UtcDatetime
    DeliveryAttempts: int
    Bounces: int
    Complaints: int
    Rejects: int

class SESStatisticsResponse(BaseModel):
    data_points: List[SESDataPoint]
    summary: dict # { "total_bounces": 10, "bounce_rate": 0.05 }


class ProviderIngestRequest(BaseModel):
    provider: str  # GOOGLE_POSTMASTER / SNDS / JMRP
    domain_name: str
    payload: Optional[Any] = None
    source: str = "MANUAL"  # MANUAL / FEED_URL
    dry_run: bool = False


class ExternalMetricResponse(BaseModel):
    metric_id: str
    provider: str
    domain_name: str
    metric_date: str
    spam_rate: Optional[float] = None
    complaint_rate: Optional[float] = None
    bounce_rate: Optional[float] = None
    reputation_score: Optional[float] = None
    delivery_error_rate: Optional[float] = None
    source: str
    ingested_at: UtcDatetime

    class Config:
        from_attributes = True


class ExternalFeedbackEventResponse(BaseModel):
    event_id: str
    provider: str
    domain_name: str
    event_type: str
    event_time: Optional[UtcDatetime] = None
    recipient_hash: Optional[str] = None
    ingested_at: UtcDatetime

    class Config:
        from_attributes = True


class DashboardAlertResponse(BaseModel):
    id: str
    source: str
    severity: str
    title: str
    message: str
    created_at: Optional[UtcDatetime] = None
    route: Optional[str] = None
    action_label: Optional[str] = None
    alert_type: str
    scope_type: str
    scope_id: Optional[str] = None


class AlertPreferenceItem(BaseModel):
    scope_type: str
    scope_id: Optional[str] = None
    alert_type: str
    enabled: bool
    email_enabled: bool
    cooldown_minutes: int = 360


class InboxAlertPreferenceGroup(BaseModel):
    inbox_id: str
    email_address: str
    preferences: List[AlertPreferenceItem]


class AlertPreferenceOverviewResponse(BaseModel):
    tenant_preferences: List[AlertPreferenceItem]
    inbox_preferences: List[InboxAlertPreferenceGroup]


class AlertPreferenceBulkUpsertRequest(BaseModel):
    preferences: List[AlertPreferenceItem]


class SentEmailLogEntry(BaseModel):
    message_id: str
    subject: Optional[str] = None
    to_email: str
    from_email: Optional[str] = None
    sent_at: Optional[UtcDatetime] = None
    scheduled_at: Optional[UtcDatetime] = None
    status: str
    campaign_name: Optional[str] = None

    class Config:
        from_attributes = True


# POST /deliverability/domains: a sending domain to add to the workspace (no docstring: it would
# become the schema's OpenAPI description)
class DomainCreate(BaseModel):
    domain_name: str = Field(..., min_length=1, max_length=300)
