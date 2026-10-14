# app/services/campaign_analytics_service.py
"""
Campaign analytics: aggregates open, click, reply and bounce metrics.
"""

from sqlalchemy.orm import Session
from sqlalchemy import ColumnElement, func
from typing import List

from app.models.campaign import CampaignProspect
from app.models.email_message import EmailMessage, EmailEvent


# One definition of the campaign counters, shared by every screen (campaign
# list/detail, step analytics, /metrics, dashboard, global analytics,
# comparison):
#   sent   = outbound campaign messages that actually left (status SENT, or
#            BOUNCED / COMPLAINED after a send), inbound replies excluded;
#   opened = UNIQUE prospects with an open event in the campaign;
#   clicked = UNIQUE prospects with a click event in the campaign (so a
#            contact clicking two links counts once; click rate = clicked /
#            opened);
#   replied / bounced = distinct messages with that event.
SENT_STATUSES = ("SENT", "BOUNCED", "COMPLAINED")


def sent_message_filters() -> List[ColumnElement[bool]]:
    """SQL conditions selecting the EmailMessage rows that count as 'sent'."""
    return [
        EmailMessage.status.in_(SENT_STATUSES),
        EmailMessage.sent_at.isnot(None),
        func.coalesce(EmailMessage.direction, "OUTBOUND") != "INBOUND",
    ]


def unique_prospects_expr() -> ColumnElement[int]:
    """Unique (campaign, prospect) pairs over EmailEvent x EmailMessage rows.

    Filter the joined rows on one event type (open, click): within one
    campaign this is the number of distinct prospects with that event; across
    campaigns it is the sum of those per-campaign numbers.
    """
    return func.count(func.distinct(
        func.concat(EmailMessage.campaign_id, ":", EmailMessage.prospect_id)
    ))


# Opened / clicked both count unique prospects (see the definition above).
unique_openers_expr = unique_prospects_expr
unique_clickers_expr = unique_prospects_expr


class CampaignAnalyticsService:
    """Handles aggregating email event metrics for campaigns."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_dynamic_metrics(self, campaign_id: str) -> dict:
        """
        Calculate metrics dynamically from events.
        Optimized to use fewer queries for remote database performance.
        """
        sent_count = self.db.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.campaign_id == campaign_id,
            *sent_message_filters(),
        ).scalar() or 0

        unique_openers = self.db.query(unique_openers_expr()).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type == EmailEvent.EVENT_OPEN,
        ).scalar() or 0

        # Get counts of unique message_ids for Open, Reply, Bounce events in one query
        results = self.db.query(
            EmailEvent.event_type,
            func.count(func.distinct(EmailEvent.message_id))
        ).join(
            EmailMessage, EmailEvent.message_id == EmailMessage.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_type.in_([
                EmailEvent.EVENT_OPEN,
                EmailEvent.EVENT_REPLY,
                EmailEvent.EVENT_BOUNCE,
                EmailEvent.EVENT_SENDER_BOUNCE,
                EmailEvent.EVENT_POSITIVE_REPLY,
                EmailEvent.EVENT_REPLY_OOO,
                EmailEvent.EVENT_UNSUBSCRIBE
            ])
        ).group_by(EmailEvent.event_type).all()

        event_counts = {r[0]: r[1] for r in results}

        opened_count = unique_openers
        replied_count = event_counts.get(EmailEvent.EVENT_REPLY, 0)
        bounced_count = event_counts.get(EmailEvent.EVENT_BOUNCE, 0)
        sender_bounced_count = event_counts.get(EmailEvent.EVENT_SENDER_BOUNCE, 0)
        positive_replied_count = event_counts.get(EmailEvent.EVENT_POSITIVE_REPLY, 0)
        ooo_count = event_counts.get(EmailEvent.EVENT_REPLY_OOO, 0)
        unsubscribed_count = event_counts.get(EmailEvent.EVENT_UNSUBSCRIBE, 0)

        # Robust fallbacks from CampaignProspect statuses
        opened_prospect_count = self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status.in_(["OPENED", "REPLIED"])
        ).scalar() or 0
        opened_count = max(opened_count, opened_prospect_count)

        replied_prospect_count = self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status == "REPLIED"
        ).scalar() or 0
        replied_count = max(replied_count, replied_prospect_count)

        return {
            "sent_count": sent_count,
            "opened_count": opened_count,
            "replied_count": replied_count,
            "bounced_count": bounced_count,
            "sender_bounced_count": sender_bounced_count,
            "positive_replied_count": positive_replied_count,
            "ooo_count": ooo_count,
            "unsubscribed_count": unsubscribed_count,
        }

    def get_bulk_metrics(self, campaign_ids: List[str]) -> dict:
        """
        Fetch metrics for multiple campaigns in bulk.
        Reduces database round-trips for listings.

        Returns: { campaign_id: { sent_count, opened_count, ... }, ... }
        """
        if not campaign_ids:
            return {}

        # 1. Bulk Sent Counts (see sent_message_filters)
        sent_results = self.db.query(
            EmailMessage.campaign_id,
            func.count(EmailMessage.message_id)
        ).filter(
            EmailMessage.campaign_id.in_(campaign_ids),
            *sent_message_filters(),
        ).group_by(EmailMessage.campaign_id).all()

        sent_map = {r[0]: r[1] for r in sent_results}

        openers_map = dict(self.db.query(
            EmailMessage.campaign_id,
            unique_openers_expr()
        ).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).filter(
            EmailMessage.campaign_id.in_(campaign_ids),
            EmailEvent.event_type == EmailEvent.EVENT_OPEN
        ).group_by(EmailMessage.campaign_id).all())

        # 2. Bulk Event Counts (Open, Reply, Bounce etc)
        event_results = self.db.query(
            EmailMessage.campaign_id,
            EmailEvent.event_type,
            func.count(func.distinct(EmailEvent.message_id))
        ).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).filter(
            EmailMessage.campaign_id.in_(campaign_ids),
            EmailEvent.event_type.in_([
                EmailEvent.EVENT_OPEN,
                EmailEvent.EVENT_REPLY,
                EmailEvent.EVENT_BOUNCE,
                EmailEvent.EVENT_SENDER_BOUNCE,
                EmailEvent.EVENT_POSITIVE_REPLY,
                EmailEvent.EVENT_REPLY_OOO,
                EmailEvent.EVENT_UNSUBSCRIBE
            ])
        ).group_by(EmailMessage.campaign_id, EmailEvent.event_type).all()

        # Initialize map with 0s for all requested IDs
        metrics_map = {cid: {
            "sent_count": sent_map.get(cid, 0),
            "opened_count": openers_map.get(cid, 0),
            "replied_count": 0,
            "bounced_count": 0,
            "sender_bounced_count": 0,
            "positive_replied_count": 0,
            "ooo_count": 0,
            "unsubscribed_count": 0,
            "in_progress_count": 0,
        } for cid in campaign_ids}

        # Fill in event counts
        for cid, etype, count in event_results:
            if cid in metrics_map:
                if etype == EmailEvent.EVENT_REPLY:
                    metrics_map[cid]["replied_count"] = count
                elif etype == EmailEvent.EVENT_BOUNCE:
                    metrics_map[cid]["bounced_count"] = count
                elif etype == EmailEvent.EVENT_SENDER_BOUNCE:
                    metrics_map[cid]["sender_bounced_count"] = count
                elif etype == EmailEvent.EVENT_POSITIVE_REPLY:
                    metrics_map[cid]["positive_replied_count"] = count
                elif etype == EmailEvent.EVENT_REPLY_OOO:
                    metrics_map[cid]["ooo_count"] = count
                elif etype == EmailEvent.EVENT_UNSUBSCRIBE:
                    metrics_map[cid]["unsubscribed_count"] = count

        # 3. Bulk Robust Fallbacks (from CampaignProspect statuses)
        opened_status_results = self.db.query(
            CampaignProspect.campaign_id,
            func.count(CampaignProspect.id)
        ).filter(
            CampaignProspect.campaign_id.in_(campaign_ids),
            CampaignProspect.status.in_(["OPENED", "REPLIED"])
        ).group_by(CampaignProspect.campaign_id).all()

        for cid, count in opened_status_results:
            if cid in metrics_map:
                metrics_map[cid]["opened_count"] = max(metrics_map[cid]["opened_count"], count)

        replied_status_results = self.db.query(
            CampaignProspect.campaign_id,
            func.count(CampaignProspect.id)
        ).filter(
            CampaignProspect.campaign_id.in_(campaign_ids),
            CampaignProspect.status == "REPLIED"
        ).group_by(CampaignProspect.campaign_id).all()

        for cid, count in replied_status_results:
            if cid in metrics_map:
                metrics_map[cid]["replied_count"] = max(metrics_map[cid]["replied_count"], count)

        # 4. In-progress: prospects still being actively sequenced (the same
        # gate the scheduler checks — see app/utils/campaign_prospect_status.py
        # for the full status ladder). Everything else (COMPLETED, PAUSED,
        # RECONNECT_ELIGIBLE, BOUNCED, REPLIED, UNSUBSCRIBED) has exited.
        in_progress_results = self.db.query(
            CampaignProspect.campaign_id,
            func.count(CampaignProspect.id)
        ).filter(
            CampaignProspect.campaign_id.in_(campaign_ids),
            CampaignProspect.status.in_(["ACTIVE", "OPENED"])
        ).group_by(CampaignProspect.campaign_id).all()

        for cid, count in in_progress_results:
            if cid in metrics_map:
                metrics_map[cid]["in_progress_count"] = count

        return metrics_map
