# app/services/campaign_analytics_service.py
"""
Campaign analytics: aggregates open, click, reply and bounce metrics.

The counter definitions (sent / unique opened / unique clicked / replied / bounced) and their SQL live
in app/repositories/reports_campaign_metrics.py; the names below stay importable from here for the
campaign services that build their own step queries with them.
"""

from typing import Any, Dict, List, Optional

from app.repositories.reports_campaign_metrics import (
    SENT_STATUSES,
    CampaignMetricsRepository,
    SqlAlchemyCampaignMetricsRepository,
    sent_message_filters,
    unique_clickers_expr,
    unique_openers_expr,
    unique_prospects_expr,
)
from app.models.email_message import EmailEvent

__all__ = ["SENT_STATUSES", "CampaignAnalyticsService", "sent_message_filters", "unique_clickers_expr",
           "unique_openers_expr", "unique_prospects_expr"]

# Fallbacks from CampaignProspect statuses: a reply implies the email was opened
OPENED_STATUSES = ("OPENED", "REPLIED")
REPLIED_STATUSES = ("REPLIED",)
# Prospects still being actively sequenced (the same gate the scheduler checks; see
# app/utils/campaign_prospect_status.py for the full status ladder). Everything else (COMPLETED, PAUSED,
# RECONNECT_ELIGIBLE, BOUNCED, REPLIED, UNSUBSCRIBED) has exited.
IN_PROGRESS_STATUSES = ("ACTIVE", "OPENED")

# event type -> metrics key filled from the distinct-message event counts
EVENT_KEYS = {
    EmailEvent.EVENT_REPLY: "replied_count",
    EmailEvent.EVENT_BOUNCE: "bounced_count",
    EmailEvent.EVENT_SENDER_BOUNCE: "sender_bounced_count",
    EmailEvent.EVENT_POSITIVE_REPLY: "positive_replied_count",
    EmailEvent.EVENT_REPLY_OOO: "ooo_count",
    EmailEvent.EVENT_UNSUBSCRIBE: "unsubscribed_count",
}


def _empty_metrics(sent: int, opened: int) -> Dict[str, int]:
    metrics = {"sent_count": sent, "opened_count": opened}
    metrics.update({key: 0 for key in EVENT_KEYS.values()})
    return metrics


class CampaignAnalyticsService:
    """Handles aggregating email event metrics for campaigns."""

    def __init__(self, db: Any = None, repository: Optional[CampaignMetricsRepository] = None) -> None:
        if repository is None:
            if db is None:
                raise ValueError("CampaignAnalyticsService needs a session or a repository")
            repository = SqlAlchemyCampaignMetricsRepository(db)
        self.db = db
        self._repo = repository

    def get_dynamic_metrics(self, campaign_id: str) -> dict:
        """Metrics of one campaign, computed from its events (with the prospect-status fallbacks)."""
        repo = self._repo
        metrics = _empty_metrics(repo.sent_count(campaign_id), repo.unique_openers(campaign_id))
        counts = repo.event_counts(campaign_id)
        for etype, key in EVENT_KEYS.items():
            metrics[key] = counts.get(etype, 0)
        metrics["opened_count"] = max(metrics["opened_count"], repo.prospect_status_count(campaign_id, OPENED_STATUSES))
        metrics["replied_count"] = max(metrics["replied_count"],
                                       repo.prospect_status_count(campaign_id, REPLIED_STATUSES))
        return metrics

    def get_bulk_metrics(self, campaign_ids: List[str]) -> dict:
        """Metrics for several campaigns in a few grouped queries (campaign listings).

        Returns: { campaign_id: { sent_count, opened_count, ... }, ... }
        """
        if not campaign_ids:
            return {}
        repo = self._repo
        sent, openers = repo.bulk_sent(campaign_ids), repo.bulk_openers(campaign_ids)
        metrics_map = {cid: {**_empty_metrics(sent.get(cid, 0), openers.get(cid, 0)), "in_progress_count": 0}
                       for cid in campaign_ids}
        for cid, etype, count in repo.bulk_event_counts(campaign_ids):
            key = EVENT_KEYS.get(etype)
            if cid in metrics_map and key:
                metrics_map[cid][key] = count
        for cid, count in repo.bulk_prospect_status_counts(campaign_ids, OPENED_STATUSES):
            if cid in metrics_map:
                metrics_map[cid]["opened_count"] = max(metrics_map[cid]["opened_count"], count)
        for cid, count in repo.bulk_prospect_status_counts(campaign_ids, REPLIED_STATUSES):
            if cid in metrics_map:
                metrics_map[cid]["replied_count"] = max(metrics_map[cid]["replied_count"], count)
        for cid, count in repo.bulk_prospect_status_counts(campaign_ids, IN_PROGRESS_STATUSES):
            if cid in metrics_map:
                metrics_map[cid]["in_progress_count"] = count
        return metrics_map
