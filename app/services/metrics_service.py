# app/services/metrics_service.py
"""
Campaign metrics for GET /campaigns/{id}/metrics, computed from messages and events.
"""

from typing import Any

from app.domain.reports import delivered


class MetricsService:
    """Campaign metrics (read only)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def get_metrics_dict(self, campaign_id: str) -> dict:
        """Get metrics as a dictionary for API response.

        Computed from messages/events with the same definitions as the campaign
        detail page (CampaignAnalyticsService) so every screen agrees. (There is no
        counter table: campaign_metrics_realtime was dropped in 0036.)
        """
        from app.services.campaign_analytics_service import CampaignAnalyticsService

        m = CampaignAnalyticsService(self.db).get_dynamic_metrics(campaign_id)
        sent = m["sent_count"]
        # open / reply rates over delivered (sent - bounced), bounce rate over sent (app.domain.reports.delivered)
        base = delivered(sent, m["bounced_count"])

        def rate(n, whole=base) -> float:
            return (n / whole) * 100 if whole else 0.0

        return {
            "sent_count": sent,
            "opened_count": m["opened_count"],
            "replied_count": m["replied_count"],
            "bounced_count": m["bounced_count"],
            "sender_bounced_count": m["sender_bounced_count"],
            "positive_replied_count": m["positive_replied_count"],
            "ooo_count": m["ooo_count"],
            "open_rate": rate(m["opened_count"]),
            "reply_rate": rate(m["replied_count"]),
            "bounce_rate": rate(m["bounced_count"], sent),
        }
