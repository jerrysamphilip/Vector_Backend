# app/models/metrics.py
"""
Real-time campaign metrics model.
"""

from sqlalchemy import Column, String, Integer, ForeignKey
from sqlalchemy.orm import relationship

from app.models import Base
from app.models.types import NOW6, UtcDateTime


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
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    # Relationships
    campaign = relationship("Campaign", back_populates="metrics")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<CampaignMetrics sent={self.sent_count} opened={self.opened_count}>"

    @property
    def open_rate(self) -> float:
        """Open rate percentage over delivered (sent - bounced), as on every screen."""
        whole = max((self.sent_count or 0) - (self.bounced_count or 0), 0)
        return (self.opened_count / whole) * 100 if whole else 0.0

    @property
    def reply_rate(self) -> float:
        """Reply rate percentage over delivered (sent - bounced), as on every screen."""
        whole = max((self.sent_count or 0) - (self.bounced_count or 0), 0)
        return (self.replied_count / whole) * 100 if whole else 0.0

    @property
    def bounce_rate(self) -> float:
        """Calculate bounce rate percentage."""
        if self.sent_count == 0:
            return 0.0
        return (self.bounced_count / self.sent_count) * 100
