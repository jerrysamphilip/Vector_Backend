"""
Daily warmup summary metrics per inbox.
"""

import uuid

from sqlalchemy import Column, String, Integer, Float, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import Base


class InboxWarmupMetric(Base):
    __tablename__ = "inbox_warmup_metrics"

    metric_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    inbox_id = Column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=False)
    metric_date = Column(String(10), nullable=False)  # YYYY-MM-DD

    planned_sends = Column(Integer, default=0)
    actual_sends = Column(Integer, default=0)
    received_count = Column(Integer, default=0)
    open_count = Column(Integer, default=0)
    reply_count = Column(Integer, default=0)
    saved_from_spam_count = Column(Integer, default=0)
    bounce_count = Column(Integer, default=0)
    complaint_count = Column(Integer, default=0)
    reputation_score = Column(Float, default=65.0)
    pool_tier = Column(String(50), default="FOUNDATION")
    issue_code = Column(String(100), nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    inbox = relationship("SendingInbox", back_populates="warmup_metrics")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
