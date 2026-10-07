
# app/schemas/inbox_schema.py
"""
Pydantic schemas for Sending Inboxes.
"""
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

class SendingInboxResponse(BaseModel):
    inbox_id: str
    email_address: str
    provider: Optional[str] = None
    status: str
    last_sent_at: Optional[datetime] = None
    daily_limit: int

    # SMTP fields
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_username: Optional[str] = None
    smtp_use_ssl: Optional[bool] = False

    # IMAP fields
    imap_host: Optional[str] = None
    imap_port: Optional[int] = 993
    imap_username: Optional[str] = None
    last_sync_at: Optional[datetime] = None

    # Warm-up & Throttle fields
    warmup_enabled: Optional[bool] = True
    warmup_day: Optional[int] = 0
    warmup_start_date: Optional[datetime] = None
    max_emails_per_day: Optional[int] = None
    delay_between_emails: Optional[int] = 60
    emails_sent_today: Optional[int] = 0
    current_daily_limit: Optional[int] = None
    warmup_status: Optional[str] = "ACTIVE"
    warmup_pool: Optional[str] = "FOUNDATION"
    warmup_reputation: Optional[float] = 65.0
    warmup_auto_adjust: Optional[bool] = True
    warmup_randomize: Optional[bool] = True
    warmup_reply_rate_target: Optional[int] = 35
    warmup_max_target: Optional[int] = None
    warmup_daily_target: Optional[int] = 0
    warmup_identifier: Optional[str] = None
    warmup_issue_code: Optional[str] = None
    warmup_issue_message: Optional[str] = None
    warmup_last_activity_at: Optional[datetime] = None
    warmup_today_sent: Optional[int] = 0
    warmup_today_opened: Optional[int] = 0
    warmup_today_replied: Optional[int] = 0
    warmup_today_saved: Optional[int] = 0

    class Config:
        from_attributes = True

class SendingInboxUpdate(BaseModel):
    # SMTP settings
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_ssl: Optional[bool] = None
    # IMAP settings
    imap_host: Optional[str] = None
    imap_port: Optional[int] = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    daily_limit: Optional[int] = None
    status: Optional[str] = None
    # Warm-up & Throttle settings
    warmup_enabled: Optional[bool] = None
    max_emails_per_day: Optional[int] = None
    delay_between_emails: Optional[int] = None
    warmup_auto_adjust: Optional[bool] = None
    warmup_randomize: Optional[bool] = None
    warmup_reply_rate_target: Optional[int] = None
    warmup_max_target: Optional[int] = None
    warmup_identifier: Optional[str] = None
    warmup_status: Optional[str] = None

class SendingInboxCreate(BaseModel):
    email_address: str
    provider: Optional[str] = "SMTP"
    daily_limit: Optional[int] = 500
    # SMTP credentials
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_ssl: Optional[bool] = False
    # IMAP credentials
    imap_host: Optional[str] = None
    imap_port: Optional[int] = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    # Warm-up & Throttle settings
    warmup_enabled: Optional[bool] = False
    max_emails_per_day: Optional[int] = None      # null = use warmup schedule
    delay_between_emails: Optional[int] = 60       # seconds between emails
    warmup_auto_adjust: Optional[bool] = True
    warmup_randomize: Optional[bool] = True
    warmup_reply_rate_target: Optional[int] = 35
    warmup_max_target: Optional[int] = None
    warmup_identifier: Optional[str] = None

class SendingInboxCreatedResponse(SendingInboxResponse):
    domain_verification: Optional[dict] = None
    # e.g. { "dns_records": [...] }


class WarmupOverviewResponse(BaseModel):
    total_accounts: int
    warmup_enabled: int
    accounts_with_issues: int
    average_reputation: float
    current_pool: str
    pool_growth_score: float
    planned_today: int
    warmup_sent_today: int
    warmup_replied_today: int
    saved_today: int


class InboxWarmupMetricResponse(BaseModel):
    metric_id: str
    metric_date: str
    planned_sends: int
    actual_sends: int
    received_count: int
    open_count: int
    reply_count: int
    saved_from_spam_count: int
    bounce_count: int
    complaint_count: int
    reputation_score: float
    pool_tier: str
    issue_code: Optional[str] = None

    class Config:
        from_attributes = True


class InboxWarmupEventResponse(BaseModel):
    event_id: str
    inbox_id: str
    peer_inbox_id: Optional[str] = None
    event_type: str
    status: str
    subject: Optional[str] = None
    detail: Optional[str] = None
    event_metadata: Optional[Dict[str, Any]] = None
    event_time: datetime

    class Config:
        from_attributes = True


class InboxWarmupDetailResponse(BaseModel):
    inbox: SendingInboxResponse
    today_metric: InboxWarmupMetricResponse
    history: List[InboxWarmupMetricResponse]
    events: List[InboxWarmupEventResponse]
