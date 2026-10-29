# app/models/campaign.py
"""
Campaign models including state events and prospect enrollments.
"""

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.status_checks import status_check
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class Campaign(Base):
    """
    Email campaign with send windows and timezone support.
    """
    __tablename__ = "campaigns"

    campaign_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    campaign_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Rich context for AI email generation
    campaign_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="DRAFT")  # DRAFT / ACTIVE / PAUSED / COMPLETED
    # Why the campaign is paused; auto_paused marks a pause by the deliverability guard (BR-DF-07)
    paused_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paused_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    auto_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # Health checks only count sends after this (set when a user resumes after an auto-pause)
    health_baseline_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    sender_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # For {{your_name}} token
    # For {{signature_block}} token — role/title line
    sender_title: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    # For {{calendar_link}} - Calendly/booking URL
    cta_link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 'plain' or 'html' — controls unsubscribe footer style
    unsubscribe_mode: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="plain")

    # Send Window Settings
    send_window_start: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    send_window_end: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    respect_timezone: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True)
    campaign_timezone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="UTC")
    # Wait days between steps count business days (Mon-Fri, no US holidays) instead of calendar days
    # (F-04; alembic 0027_campaign_edit)
    wait_business_days: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    # Sending Schedule Mode
    # 'spread'  — evenly distribute across send window
    # 'random'  — random time per email within window
    # 'batch'   — send in groups with gap between batches
    sending_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default="spread")
    # random mode: minimum gap between any two emails.
    # spread mode: caps how far apart emails get spaced — without this, spread
    # always stretches evenly across the *entire* remaining send window
    # regardless of list size, so a short list in a long window sends far
    # slower than necessary. 0 = no cap (send as fast as the window/pacing
    # elsewhere allows).
    min_gap_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=2)
    batch_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # batch mode: emails per batch
    # batch mode: minutes between batch starts
    batch_gap_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=30)

    # Daily batch throttle: max prospects to start emailing each calendar day.
    # E.g. 100 → day 1 sends to prospects 1-100, day 2 sends to 101-200, etc.
    daily_batch_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Campaign Duration
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)
    # T-3, app/core/concurrency.py
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # The campaign this one was duplicated from (POST /campaigns/{id}/duplicates); NULL for an
    # original. SET NULL keeps the copy when its source is deleted (alembic 0022_campaign_source).
    source_campaign_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("campaigns.campaign_id", ondelete="SET NULL"), nullable=True, index=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="campaigns")
    creator = relationship("User", back_populates="created_campaigns")
    state_events = relationship("CampaignStateEvent", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    prospects = relationship("CampaignProspect", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    sequences = relationship("EmailSequence", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    templates = relationship("EmailTemplate", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    messages = relationship("EmailMessage", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    group_memberships = relationship("CampaignGroupMember", back_populates="campaign", lazy="dynamic")
    automation_rules = relationship("AutomationRule", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    inboxes = relationship("SendingInbox", secondary="campaign_inboxes", back_populates="campaigns")

    __table_args__ = (
        status_check("campaigns", "status"),  # D5, 0029_status_checks
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<Campaign {self.campaign_name} ({self.status})>"


class CampaignStateEvent(Base):
    """
    Audit trail for campaign state transitions.
    """
    __tablename__ = "campaign_state_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    # The campaign's tenant (D3, 0028_child_tenant_ids); filled from the campaign by app/models/tenant_fill.py
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)

    from_state: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    to_state: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    # Relationships
    campaign = relationship("Campaign", back_populates="state_events")

    __table_args__ = (
        Index("ix_campaign_state_events_tenant", "tenant_id", "campaign_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<CampaignStateEvent {self.from_state} -> {self.to_state}>"


class CampaignProspect(Base):
    """
    Prospect enrollment in a campaign.
    Tracks current step and scheduling for the sequencing engine.
    """
    __tablename__ = "campaign_prospects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    prospect_id: Mapped[str] = mapped_column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    # The campaign's tenant (D3, 0028_child_tenant_ids); filled from the campaign by app/models/tenant_fill.py
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)

    current_step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=1)
    next_scheduled_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # ACTIVE / OPENED / COMPLETED / PAUSED / RECONNECT_ELIGIBLE / BOUNCED / REPLIED / UNSUBSCRIBED — see
    # app/utils/campaign_prospect_status.py for the write-priority rules
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="ACTIVE")
    stopped_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    enrolled_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    # Relationships
    campaign = relationship("Campaign", back_populates="prospects")
    prospect = relationship("Prospect", back_populates="campaign_enrollments")

    __table_args__ = (
        UniqueConstraint("campaign_id", "prospect_id", name="uq_campaign_prospect"),
        CheckConstraint("current_step BETWEEN 1 AND 7", name="ck_step_range"),
        Index("ix_campaign_prospects_tenant_prospect", "tenant_id", "prospect_id"),
        status_check("campaign_prospects", "status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<CampaignProspect campaign={self.campaign_id} step={self.current_step}>"
