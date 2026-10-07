# app/schemas/reports_schema.py
"""
Pydantic schemas for the Reports API.
"""

from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


# ===========================
# GLOBAL ANALYTICS
# ===========================

class GlobalKPIs(BaseModel):
    """Aggregate KPIs across all campaigns for a tenant."""
    total_leads_contacted: int = 0
    total_emails_sent: int = 0
    total_opened: int = 0
    total_replied: int = 0
    total_bounced: int = 0
    total_clicked: int = 0
    overall_open_rate: float = 0.0
    overall_reply_rate: float = 0.0
    overall_bounce_rate: float = 0.0
    active_campaigns: int = 0


class TimeSeriesPoint(BaseModel):
    """Single data point in a time-series chart."""
    date: str
    sent: int = 0
    opened: int = 0
    replied: int = 0
    bounced: int = 0


class GlobalAnalyticsResponse(BaseModel):
    """Full response for the Global Analytics dashboard."""
    kpis: GlobalKPIs
    timeseries: List[TimeSeriesPoint]


# ===========================
# CAMPAIGN COMPARISON
# ===========================

class CampaignComparisonItem(BaseModel):
    """Single campaign row in the comparison table."""
    campaign_id: str
    campaign_name: str
    status: str
    sent_count: int = 0
    opened_count: int = 0
    replied_count: int = 0
    bounced_count: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0
    created_at: Optional[datetime] = None


class CampaignComparisonResponse(BaseModel):
    """Paginated campaign comparison list."""
    items: List[CampaignComparisonItem]
    total: int
    page: int
    page_size: int


# ===========================
# PROVIDER PERFORMANCE
# ===========================

class ProviderPerformanceItem(BaseModel):
    """Per-provider (Gmail/Outlook/SES) aggregate metrics."""
    provider: str
    sent_count: int = 0
    delivered_count: int = 0
    opened_count: int = 0
    replied_count: int = 0
    bounced_count: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0


class ProviderPerformanceResponse(BaseModel):
    """Provider performance comparison."""
    providers: List[ProviderPerformanceItem]


# ===========================
# LEAD STATS
# ===========================

class LeadStats(BaseModel):
    """New leads vs follow-up leads breakdown."""
    total_leads_contacted: int = 0
    new_leads_reached: int = 0
    follow_up_leads: int = 0


# ===========================
# MAILBOX STATS
# ===========================

class MailboxStats(BaseModel):
    """Mailbox connection statistics."""
    total_connected: int = 0
    mailbox_in_use: int = 0
    disconnected: int = 0
    without_warmup: int = 0


# ===========================
# CAMPAIGN STATS
# ===========================

class CampaignStats(BaseModel):
    """Campaign status breakdown."""
    total_campaigns: int = 0
    active: int = 0
    paused: int = 0
    drafted: int = 0
    completed: int = 0


# ===========================
# EMAIL HEALTH PER MAILBOX
# ===========================

class MailboxHealthItem(BaseModel):
    """Per-mailbox email metrics."""
    mailbox: str
    lead_contacted: int = 0
    email_sent: int = 0
    opened: int = 0
    opened_rate: float = 0.0
    replied: int = 0
    replied_rate: float = 0.0
    bounced: int = 0
    bounce_rate: float = 0.0


# ===========================
# CAMPAIGN OPTIMIZATION METRICS
# ===========================

class CampaignOptimizationMetrics(BaseModel):
    """Key campaign optimization metrics."""
    avg_leads_before_first_reply: float = 0.0
    follow_up_reply_rate: float = 0.0
    median_time_to_first_reply_hours: float = 0.0


# ===========================
# USER PERFORMANCE BREAKDOWN
# ===========================

class UserPerformanceItem(BaseModel):
    """Per-user campaign performance summary."""
    user_id: str
    first_name: str
    last_name: str
    email: str
    role: str
    campaigns_created: int = 0
    total_sent: int = 0
    total_opened: int = 0
    total_replied: int = 0
    total_bounced: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0


# ===========================
# FULL DASHBOARD SUMMARY
# ===========================

class DashboardSummaryResponse(BaseModel):
    """Full Smartlead-style dashboard response."""
    kpis: GlobalKPIs
    timeseries: List[TimeSeriesPoint]
    lead_stats: LeadStats
    campaign_stats: CampaignStats
    mailbox_stats: MailboxStats
    providers: List[ProviderPerformanceItem]
    mailbox_health: List[MailboxHealthItem]
    top_campaigns: List[CampaignComparisonItem]
    optimization: CampaignOptimizationMetrics
    user_breakdown: List[UserPerformanceItem] = []
