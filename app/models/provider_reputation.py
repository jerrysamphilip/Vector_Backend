"""
Read-only ingestion models for mailbox-provider reputation data.
Phase 1 stores external metrics/events without changing send behavior.
"""

from datetime import datetime
from typing import Any, Optional
import uuid
from sqlalchemy import String, Float, Text, JSON, Integer, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

# Every row belongs to the tenant that ingested it (tenant_id NULL = legacy, invisible to tenants).


class ExternalReputationMetric(Base):
    """Aggregated metric row from Google Postmaster/SNDS/JMRP feeds."""
    __tablename__ = "external_reputation_metrics"
    __table_args__ = (Index("ix_ext_metrics_tenant_domain", "tenant_id", "domain_name"),)

    metric_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # GOOGLE_POSTMASTER / SNDS / JMRP
    domain_name: Mapped[str] = mapped_column(String(255), nullable=False)
    metric_date: Mapped[str] = mapped_column(String(20), nullable=False)  # YYYY-MM-DD

    spam_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    complaint_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bounce_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reputation_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delivery_error_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    source: Mapped[Optional[str]] = mapped_column(String(50), default="MANUAL", nullable=True)  # MANUAL / FEED_URL
    raw_payload: Mapped[Any] = mapped_column(JSON, nullable=True)
    ingested_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)


class ExternalFeedbackEvent(Base):
    """Raw feedback-loop style event (mainly JMRP complaint signals)."""
    __tablename__ = "external_feedback_events"
    __table_args__ = (Index("ix_ext_events_tenant_domain", "tenant_id", "domain_name"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # JMRP / SNDS / GOOGLE_POSTMASTER
    domain_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, default="COMPLAINT")
    event_time: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    recipient_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Store hashed/obfuscated recipient ID
    raw_payload: Mapped[Any] = mapped_column(JSON, nullable=True)
    ingested_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)


class ExternalIngestionRun(Base):
    """Audit log for ingestion executions."""
    __tablename__ = "external_ingestion_runs"
    __table_args__ = (Index("ix_ext_runs_tenant_provider", "tenant_id", "provider", "started_at"),)

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    domain_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="MANUAL")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SUCCESS")  # SUCCESS / FAILED / SKIPPED
    records_ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
