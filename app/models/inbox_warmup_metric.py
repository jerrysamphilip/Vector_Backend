"""
Daily warmup summary metrics per inbox.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class InboxWarmupMetric(Base):
    __tablename__ = "inbox_warmup_metrics"

    metric_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    inbox_id: Mapped[str] = mapped_column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=False)
    metric_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD

    planned_sends: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    actual_sends: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    received_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    open_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    reply_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    saved_from_spam_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    bounce_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    complaint_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    reputation_score: Mapped[Optional[float]] = mapped_column(Float, default=65.0, nullable=True)
    pool_tier: Mapped[Optional[str]] = mapped_column(String(50), default="FOUNDATION", nullable=True)
    issue_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)

    inbox = relationship("SendingInbox", back_populates="warmup_metrics")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
