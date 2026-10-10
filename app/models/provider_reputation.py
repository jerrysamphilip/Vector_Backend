"""
Read-only ingestion models for mailbox-provider reputation data.
Phase 1 stores external metrics/events without changing send behavior.
"""

import uuid
from sqlalchemy import Column, String, Float, Text, TIMESTAMP, JSON, Integer, ForeignKey, Index
from sqlalchemy.sql import func

from app.models.base import Base

# Every row belongs to the tenant that ingested it (tenant_id NULL = legacy, invisible to tenants).


class ExternalReputationMetric(Base):
    """Aggregated metric row from Google Postmaster/SNDS/JMRP feeds."""
    __tablename__ = "external_reputation_metrics"
    __table_args__ = (Index("ix_ext_metrics_tenant_domain", "tenant_id", "domain_name"),)

    metric_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider = Column(String(50), nullable=False)  # GOOGLE_POSTMASTER / SNDS / JMRP
    domain_name = Column(String(255), nullable=False)
    metric_date = Column(String(20), nullable=False)  # YYYY-MM-DD

    spam_rate = Column(Float, nullable=True)
    complaint_rate = Column(Float, nullable=True)
    bounce_rate = Column(Float, nullable=True)
    reputation_score = Column(Float, nullable=True)
    delivery_error_rate = Column(Float, nullable=True)

    source = Column(String(50), default="MANUAL")  # MANUAL / FEED_URL
    raw_payload = Column(JSON, nullable=True)
    ingested_at = Column(TIMESTAMP, server_default=func.now())


class ExternalFeedbackEvent(Base):
    """Raw feedback-loop style event (mainly JMRP complaint signals)."""
    __tablename__ = "external_feedback_events"
    __table_args__ = (Index("ix_ext_events_tenant_domain", "tenant_id", "domain_name"),)

    event_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider = Column(String(50), nullable=False)  # JMRP / SNDS / GOOGLE_POSTMASTER
    domain_name = Column(String(255), nullable=False)
    event_type = Column(String(50), nullable=False, default="COMPLAINT")
    event_time = Column(TIMESTAMP, nullable=True)

    recipient_hash = Column(String(255), nullable=True)  # Store hashed/obfuscated recipient ID
    raw_payload = Column(JSON, nullable=True)
    ingested_at = Column(TIMESTAMP, server_default=func.now())


class ExternalIngestionRun(Base):
    """Audit log for ingestion executions."""
    __tablename__ = "external_ingestion_runs"
    __table_args__ = (Index("ix_ext_runs_tenant_provider", "tenant_id", "provider", "started_at"),)

    run_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)
    provider = Column(String(50), nullable=False)
    domain_name = Column(String(255), nullable=True)
    source = Column(String(50), nullable=False, default="MANUAL")
    status = Column(String(20), nullable=False, default="SUCCESS")  # SUCCESS / FAILED / SKIPPED
    records_ingested = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(TIMESTAMP, server_default=func.now())
    completed_at = Column(TIMESTAMP, nullable=True)
