# app/models/metrics.py
"""
Real-time campaign metrics model.
"""

from sqlalchemy import Column, String, Integer, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models import Base


class CampaignMetricsRealtime(Base):
    """
    Real-time aggregated metrics for a campaign.
    Updated on each email event.
    """
    __tablename__ = "campaign_metrics_realtime"

    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), primary_key=True)
    
    sent_count = Column(Integer, default=0)
    opened_count = Column(Integer, default=0)
    replied_count = Column(Integer, default=0)
    bounced_count = Column(Integer, default=0)
    sender_bounced_count = Column(Integer, default=0)
    positive_replied_count = Column(Integer, default=0)
    ooo_count = Column(Integer, default=0)
    
    # Optimistic locking for concurrent updates
    row_version = Column(Integer, default=0)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    campaign = relationship("Campaign", back_populates="metrics")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<CampaignMetrics sent={self.sent_count} opened={self.opened_count}>"

    @property
    def open_rate(self) -> float:
        """Calculate open rate percentage."""
        if self.sent_count == 0:
            return 0.0
        return (self.opened_count / self.sent_count) * 100

    @property
    def reply_rate(self) -> float:
        """Calculate reply rate percentage."""
        if self.sent_count == 0:
            return 0.0
        return (self.replied_count / self.sent_count) * 100

    @property
    def bounce_rate(self) -> float:
        """Calculate bounce rate percentage."""
        if self.sent_count == 0:
            return 0.0
        return (self.bounced_count / self.sent_count) * 100
