# app/models/campaign.py
"""
Campaign models including state events and prospect enrollments.
"""

from sqlalchemy import Column, String, Boolean, Integer, Date, Time, TIMESTAMP, ForeignKey, Text, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class Campaign(Base):
    """
    Email campaign with send windows and timezone support.
    """
    __tablename__ = "campaigns"

    campaign_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    
    campaign_name = Column(String(255), nullable=False)
    campaign_description = Column(Text, nullable=True)  # Rich context for AI email generation
    status = Column(String(50), default="DRAFT")  # DRAFT / ACTIVE / PAUSED / COMPLETED
    
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    sender_name = Column(String(100), nullable=True)  # For {{your_name}} token
    sender_title = Column(String(150), nullable=True)  # For {{signature_block}} token — role/title line
    cta_link = Column(String(500), nullable=True)  # For {{calendar_link}} - Calendly/booking URL
    unsubscribe_mode = Column(String(10), default="plain")  # 'plain' or 'html' — controls unsubscribe footer style

    # Send Window Settings
    send_window_start = Column(Time, nullable=True)
    send_window_end = Column(Time, nullable=True)
    respect_timezone = Column(Boolean, default=True)
    campaign_timezone = Column(String(50), default="UTC")

    # Sending Schedule Mode
    # 'spread'  — evenly distribute across send window
    # 'random'  — random time per email within window
    # 'batch'   — send in groups with gap between batches
    sending_mode = Column(String(20), default="spread")
    # random mode: minimum gap between any two emails.
    # spread mode: caps how far apart emails get spaced — without this, spread
    # always stretches evenly across the *entire* remaining send window
    # regardless of list size, so a short list in a long window sends far
    # slower than necessary. 0 = no cap (send as fast as the window/pacing
    # elsewhere allows).
    min_gap_minutes = Column(Integer, default=2)
    batch_size = Column(Integer, nullable=True)         # batch mode: emails per batch
    batch_gap_minutes = Column(Integer, default=30)     # batch mode: minutes between batch starts

    # Daily batch throttle: max prospects to start emailing each calendar day.
    # E.g. 100 → day 1 sends to prospects 1-100, day 2 sends to 101-200, etc.
    daily_batch_size = Column(Integer, nullable=True)

    # Campaign Duration
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    tenant = relationship("Tenant", back_populates="campaigns")
    creator = relationship("User", back_populates="created_campaigns")
    state_events = relationship("CampaignStateEvent", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    prospects = relationship("CampaignProspect", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    sequences = relationship("EmailSequence", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    templates = relationship("EmailTemplate", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    messages = relationship("EmailMessage", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    metrics = relationship("CampaignMetricsRealtime", back_populates="campaign", uselist=False, cascade="all, delete-orphan")
    group_memberships = relationship("CampaignGroupMember", back_populates="campaign", lazy="dynamic")
    automation_rules = relationship("AutomationRule", back_populates="campaign", lazy="dynamic", cascade="all, delete-orphan")
    inboxes = relationship("SendingInbox", secondary="campaign_inboxes", back_populates="campaigns")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<Campaign {self.campaign_name} ({self.status})>"


class CampaignStateEvent(Base):
    """
    Audit trail for campaign state transitions.
    """
    __tablename__ = "campaign_state_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    
    from_state = Column(String(50), nullable=True)
    to_state = Column(String(50), nullable=False)
    reason = Column(Text, nullable=True)
    
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    campaign = relationship("Campaign", back_populates="state_events")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<CampaignStateEvent {self.from_state} -> {self.to_state}>"


class CampaignProspect(Base):
    """
    Prospect enrollment in a campaign.
    Tracks current step and scheduling for the sequencing engine.
    """
    __tablename__ = "campaign_prospects"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)

    current_step = Column(Integer, default=1)
    next_scheduled_at = Column(TIMESTAMP, nullable=True)

    status = Column(String(50), default="ACTIVE")  # ACTIVE / OPENED / COMPLETED / PAUSED / RECONNECT_ELIGIBLE / BOUNCED / REPLIED / UNSUBSCRIBED — see app/utils/campaign_prospect_status.py for the write-priority rules
    stopped_reason = Column(String(100), nullable=True)

    enrolled_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    campaign = relationship("Campaign", back_populates="prospects")
    prospect = relationship("Prospect", back_populates="campaign_enrollments")

    __table_args__ = (
        UniqueConstraint("campaign_id", "prospect_id", name="uq_campaign_prospect"),
        CheckConstraint("current_step BETWEEN 1 AND 7", name="ck_step_range"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<CampaignProspect campaign={self.campaign_id} step={self.current_step}>"
