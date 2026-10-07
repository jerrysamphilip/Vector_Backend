# app/services/metrics_service.py
"""
Real-time metrics service for campaign dashboard.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
import uuid

from app.models.metrics import CampaignMetricsRealtime
from app.models.email_message import EmailEvent


class MetricsService:
    """Service for campaign metrics management."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_or_create_metrics(self, campaign_id: str) -> CampaignMetricsRealtime:
        """
        Get existing metrics or create new ones for a campaign.
        
        Args:
            campaign_id: Campaign ID
        
        Returns:
            CampaignMetricsRealtime instance
        """
        metrics = self.db.query(CampaignMetricsRealtime).filter(
            CampaignMetricsRealtime.campaign_id == campaign_id
        ).first()
        
        if not metrics:
            metrics = CampaignMetricsRealtime(
                campaign_id=campaign_id,
                sent_count=0,
                opened_count=0,
                replied_count=0,
                bounced_count=0,
                row_version=0,
            )
            self.db.add(metrics)
            self.db.flush()
        
        return metrics
    
    def increment_sent(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment sent count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.sent_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics
    
    def increment_opened(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment opened count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.opened_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics
    
    def increment_replied(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment replied count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.replied_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics
    
    def increment_bounced(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment bounced count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.bounced_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics

    def increment_sender_bounced(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment sender-attributable bounced count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.sender_bounced_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics

    def increment_positive_replied(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment AI-classified positive-intent reply count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.positive_replied_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics

    def increment_ooo(self, campaign_id: str, count: int = 1) -> CampaignMetricsRealtime:
        """Increment out-of-office/auto-response reply count."""
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.ooo_count += count
        metrics.row_version += 1
        self.db.flush()
        return metrics

    def process_event(self, campaign_id: str, event_type: int) -> CampaignMetricsRealtime:
        """
        Process an email event and update metrics.
        
        Args:
            campaign_id: Campaign ID
            event_type: Event type from EmailEvent constants
        
        Returns:
            Updated metrics
        """
        if event_type == EmailEvent.EVENT_SENT:
            return self.increment_sent(campaign_id)
        elif event_type == EmailEvent.EVENT_OPEN:
            return self.increment_opened(campaign_id)
        elif event_type == EmailEvent.EVENT_REPLY:
            return self.increment_replied(campaign_id)
        elif event_type == EmailEvent.EVENT_BOUNCE:
            return self.increment_bounced(campaign_id)
        elif event_type == EmailEvent.EVENT_SENDER_BOUNCE:
            return self.increment_sender_bounced(campaign_id)
        elif event_type == EmailEvent.EVENT_POSITIVE_REPLY:
            return self.increment_positive_replied(campaign_id)
        elif event_type == EmailEvent.EVENT_REPLY_OOO:
            return self.increment_ooo(campaign_id)

        return self.get_or_create_metrics(campaign_id)

    def get_metrics_dict(self, campaign_id: str) -> dict:
        """Get metrics as a dictionary for API response."""
        metrics = self.get_or_create_metrics(campaign_id)

        return {
            "sent_count": metrics.sent_count,
            "opened_count": metrics.opened_count,
            "replied_count": metrics.replied_count,
            "bounced_count": metrics.bounced_count,
            "sender_bounced_count": metrics.sender_bounced_count,
            "positive_replied_count": metrics.positive_replied_count,
            "ooo_count": metrics.ooo_count,
            "open_rate": metrics.open_rate,
            "reply_rate": metrics.reply_rate,
            "bounce_rate": metrics.bounce_rate,
        }

    def sync_campaign_metrics(self, campaign_id: str):
        """
        Force sync metrics from raw message and event data.
        Use this to fix out-of-sync or historical metrics.
        """
        from app.models.email_message import EmailMessage, EmailEvent
        from app.models.campaign import CampaignProspect

        # 1. Total Sent
        sent_count = self.db.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status == "SENT"
        ).scalar() or 0

        # 2. Total Opened (Unique messages opened)
        opened_count = self.db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_OPEN
        ).scalar() or 0
        
        # Robust Open Check: Any prospect in REPLIED or OPENED status must have been opened
        opened_status_count = self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status.in_(["OPENED", "REPLIED"])
        ).scalar() or 0
        opened_count = max(opened_count, opened_status_count)

        # 3. Total Replied (Unique messages replied)
        replied_count = self.db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_REPLY
        ).scalar() or 0
        
        # Robust Reply Check: Any prospect in REPLIED status
        replied_status_count = self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status == "REPLIED"
        ).scalar() or 0
        replied_count = max(replied_count, replied_status_count)

        # 4. Total Bounced
        bounced_count = self.db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_BOUNCE
        ).scalar() or 0

        # 5. Sender-attributable bounces, positive-intent replies, OOO replies
        sender_bounced_count = self.db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_SENDER_BOUNCE
        ).scalar() or 0

        positive_replied_count = self.db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_POSITIVE_REPLY
        ).scalar() or 0

        ooo_count = self.db.query(func.count(func.distinct(EmailEvent.message_id))).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_REPLY_OOO
        ).scalar() or 0

        # Update table
        metrics = self.get_or_create_metrics(campaign_id)
        metrics.sent_count = sent_count
        metrics.opened_count = opened_count
        metrics.replied_count = replied_count
        metrics.bounced_count = bounced_count
        metrics.sender_bounced_count = sender_bounced_count
        metrics.positive_replied_count = positive_replied_count
        metrics.ooo_count = ooo_count
        metrics.row_version += 1
        
        self.db.add(metrics)
        self.db.flush()
        
        return metrics
