
# app/models/domain_reputation.py
"""
Models for tracking Sending Domain Health, Reputation, and Deliverability Alerts.
"""

from datetime import datetime
from typing import Any, Optional
from sqlalchemy import String, Integer, Boolean, Float, Text, ForeignKey, JSON, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id

class SendingDomain(Base):
    """
    Configuration and health status of a sending domain, owned by one tenant.

    Two tenants can send from the same domain name: each gets its own row, and every
    status/health/reputation figure on it is computed from that tenant's sends only.
    Rows with tenant_id NULL are legacy records no tenant could be matched to; they are
    kept but never shown to (or acted on for) any tenant.
    """
    __tablename__ = "sending_domains"
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain_name", name="uq_sending_domains_tenant_domain"),
        Index("ix_sending_domains_domain_name", "domain_name"),
    )

    domain_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id, nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    domain_name: Mapped[str] = mapped_column(String(255), nullable=False)
    
    # Authentication Status
    spf_status: Mapped[Optional[str]] = mapped_column(String(50), default="UNKNOWN", nullable=True)  # PASS, FAIL, UNKNOWN
    dkim_status: Mapped[Optional[str]] = mapped_column(String(50), default="UNKNOWN", nullable=True)
    dmarc_status: Mapped[Optional[str]] = mapped_column(String(50), default="UNKNOWN", nullable=True)
    
    # Reputation Metrics
    current_reputation_score: Mapped[Optional[int]] = mapped_column(Integer, default=100, nullable=True) # 0 to 100
    safety_threshold: Mapped[Optional[int]] = mapped_column(Integer, default=70, nullable=True) # Pause campaigns if score drops below this
    
    # Deliverability Status
    is_blacklisted: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, nullable=True)
    warmup_status: Mapped[Optional[str]] = mapped_column(String(50), default="COLD", nullable=True) # COLD, WARMING, ACTIVE, PAUSED
    
    # AWS SES v2 Specific Metrics
    ses_reputation_status: Mapped[Optional[str]] = mapped_column(String(50), default="UNKNOWN", nullable=True) # HEALTHY, UNHEALTHY, PENDING, UNKNOWN
    sesv2_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, nullable=True) # Virtual Deliverability Manager status
    account_reputation_score: Mapped[Optional[float]] = mapped_column(Float, default=1.0, nullable=True) # 0.0 to 1.0 (AWS Reputation)

    # Rolling 24h health, recomputed hourly and on every bounce/complaint (BR-DF-07)
    sends_24h: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default="0", nullable=True)
    bounce_rate_24h: Mapped[Optional[float]] = mapped_column(Float, default=0.0, server_default="0", nullable=True)
    complaint_rate_24h: Mapped[Optional[float]] = mapped_column(Float, default=0.0, server_default="0", nullable=True)
    health_checked_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    
    # Metadata
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)

    # Relationships
    snapshots = relationship("DomainHealthSnapshot", back_populates="domain", cascade="all, delete-orphan")
    alerts = relationship("ReputationAlert", back_populates="domain", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<SendingDomain {self.domain_name} tenant={self.tenant_id} (Score: {self.current_reputation_score})>"


class DomainHealthSnapshot(Base):
    """
    Daily/Hourly snapshot of domain health metrics for historical trending.
    """
    __tablename__ = "domain_health_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id, nullable=False)
    domain_id: Mapped[str] = mapped_column(String(36), ForeignKey("sending_domains.domain_id", ondelete="CASCADE"),
                       nullable=False, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True, index=True)
    domain_name: Mapped[str] = mapped_column(String(255), nullable=False)  # denormalised for display
    
    snapshot_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    
    # Key Metrics at time of snapshot
    reputation_score: Mapped[int] = mapped_column(Integer, nullable=False)
    bounce_rate_24h: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    complaint_rate_24h: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    open_rate_24h: Mapped[Optional[float]] = mapped_column(Float, default=0.0, nullable=True)
    
    # Detailed raw data
    raw_metrics: Mapped[Any] = mapped_column(JSON, nullable=True) # {"sent": 100, "bounced": 2, ...}

    # Relationships
    domain = relationship("SendingDomain", back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<HealthSnapshot {self.domain_name} @ {self.snapshot_at}>"


class ReputationAlert(Base):
    """
    Audit log of reputation alerts (e.g., Bounce Spike, Blacklist detected).
    """
    __tablename__ = "reputation_alerts"

    alert_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id, nullable=False)
    domain_id: Mapped[str] = mapped_column(String(36), ForeignKey("sending_domains.domain_id", ondelete="CASCADE"),
                       nullable=False, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True, index=True)
    domain_name: Mapped[str] = mapped_column(String(255), nullable=False)  # denormalised for display
    
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False) # BOUNCE_SPIKE, REPUTATION_DROP, BLACKLIST
    severity: Mapped[Optional[str]] = mapped_column(String(20), default="WARNING", nullable=True) # INFO, WARNING, CRITICAL
    
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_resolved: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, nullable=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # Relationships
    domain = relationship("SendingDomain", back_populates="alerts")

    def __repr__(self) -> str:
        return f"<ReputationAlert {self.alert_type} for {self.domain_name}>"
