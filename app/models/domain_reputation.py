
# app/models/domain_reputation.py
"""
Models for tracking Sending Domain Health, Reputation, and Deliverability Alerts.
"""

from sqlalchemy import Column, String, Integer, Boolean, Float, Text, TIMESTAMP, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base

class SendingDomain(Base):
    """
    Configuration and health status of a sending domain.
    """
    __tablename__ = "sending_domains"

    domain_name = Column(String(255), primary_key=True)
    
    # Authentication Status
    spf_status = Column(String(50), default="UNKNOWN")  # PASS, FAIL, UNKNOWN
    dkim_status = Column(String(50), default="UNKNOWN")
    dmarc_status = Column(String(50), default="UNKNOWN")
    
    # Reputation Metrics
    current_reputation_score = Column(Integer, default=100) # 0 to 100
    safety_threshold = Column(Integer, default=70) # Pause campaigns if score drops below this
    
    # Deliverability Status
    is_blacklisted = Column(Boolean, default=False)
    warmup_status = Column(String(50), default="COLD") # COLD, WARMING, ACTIVE, PAUSED
    
    # AWS SES v2 Specific Metrics
    ses_reputation_status = Column(String(50), default="UNKNOWN") # HEALTHY, UNHEALTHY, PENDING, UNKNOWN
    sesv2_enabled = Column(Boolean, default=False) # Virtual Deliverability Manager status
    account_reputation_score = Column(Float, default=1.0) # 0.0 to 1.0 (AWS Reputation)

    # Rolling 24h health, recomputed hourly and on every bounce/complaint (BR-DF-07)
    sends_24h = Column(Integer, default=0)
    bounce_rate_24h = Column(Float, default=0.0)
    complaint_rate_24h = Column(Float, default=0.0)
    health_checked_at = Column(TIMESTAMP, nullable=True)
    
    # Metadata
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    snapshots = relationship("DomainHealthSnapshot", back_populates="domain", cascade="all, delete-orphan")
    alerts = relationship("ReputationAlert", back_populates="domain", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<SendingDomain {self.domain_name} (Score: {self.current_reputation_score})>"


class DomainHealthSnapshot(Base):
    """
    Daily/Hourly snapshot of domain health metrics for historical trending.
    """
    __tablename__ = "domain_health_snapshots"

    snapshot_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    domain_name = Column(String(255), ForeignKey("sending_domains.domain_name"), nullable=False)
    
    snapshot_at = Column(TIMESTAMP, server_default=func.now())
    
    # Key Metrics at time of snapshot
    reputation_score = Column(Integer, nullable=False)
    bounce_rate_24h = Column(Float, default=0.0)
    complaint_rate_24h = Column(Float, default=0.0)
    open_rate_24h = Column(Float, default=0.0)
    
    # Detailed raw data
    raw_metrics = Column(JSON, nullable=True) # {"sent": 100, "bounced": 2, ...}

    # Relationships
    domain = relationship("SendingDomain", back_populates="snapshots")

    def __repr__(self):
        return f"<HealthSnapshot {self.domain_name} @ {self.snapshot_at}>"


class ReputationAlert(Base):
    """
    Audit log of reputation alerts (e.g., Bounce Spike, Blacklist detected).
    """
    __tablename__ = "reputation_alerts"

    alert_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    domain_name = Column(String(255), ForeignKey("sending_domains.domain_name"), nullable=False)
    
    alert_type = Column(String(50), nullable=False) # BOUNCE_SPIKE, REPUTATION_DROP, BLACKLIST
    severity = Column(String(20), default="WARNING") # INFO, WARNING, CRITICAL
    
    details = Column(Text, nullable=True)
    is_resolved = Column(Boolean, default=False)
    resolution_notes = Column(Text, nullable=True)
    
    created_at = Column(TIMESTAMP, server_default=func.now())
    resolved_at = Column(TIMESTAMP, nullable=True)

    # Relationships
    domain = relationship("SendingDomain", back_populates="alerts")

    def __repr__(self):
        return f"<ReputationAlert {self.alert_type} for {self.domain_name}>"
