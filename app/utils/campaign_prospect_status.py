# app/utils/campaign_prospect_status.py
"""
Central authority for CampaignProspect.status transitions.

CampaignProspect.status doubles as both a display/filter status AND the
sending gate (execution_service.py / email_scheduler_service.py only ever
advance a sequence for prospects with status == "ACTIVE"). Status is written
from many independent, async, out-of-order places — the scheduler's own
COMPLETED write right after a send, an SES bounce webhook that can arrive
seconds-to-minutes later, IMAP reply sync polling every 5 minutes, manual
admin actions — with no coordination between them. Routing every write
through `set_prospect_status()` guarantees a later, less-important event
(e.g. the scheduler marking a prospect COMPLETED) can never silently erase a
more important one that was already recorded (e.g. a bounce or an
unsubscribe), regardless of which order the events actually arrive in.
"""

import logging

logger = logging.getLogger(__name__)

# Higher number = more important / harder to overwrite.
STATUS_PRIORITY = {
    "ACTIVE": 0,
    "OPENED": 1,
    "COMPLETED": 2,
    "PAUSED": 3,
    "RECONNECT_ELIGIBLE": 3,
    "BOUNCED": 4,
    "REPLIED": 5,
    "UNSUBSCRIBED": 6,
}


def set_prospect_status(campaign_prospect, new_status: str, stopped_reason: str = None) -> bool:
    """
    Update `campaign_prospect.status`, refusing to downgrade a higher-priority
    status that's already recorded.

    Returns True if the status was changed (or `stopped_reason` was updated
    on a no-op same-status write), False if the write was skipped because the
    current status already outranks `new_status`.
    """
    if campaign_prospect is None:
        return False

    current = campaign_prospect.status

    if current == new_status:
        if stopped_reason is not None:
            campaign_prospect.stopped_reason = stopped_reason
        return True

    current_priority = STATUS_PRIORITY.get(current, 0)
    new_priority = STATUS_PRIORITY.get(new_status, 0)

    if new_priority < current_priority:
        logger.info(
            f"[ProspectStatus] Skipped downgrade {current!r} -> {new_status!r} "
            f"for prospect {getattr(campaign_prospect, 'prospect_id', '?')} "
            f"in campaign {getattr(campaign_prospect, 'campaign_id', '?')}"
        )
        return False

    campaign_prospect.status = new_status
    if stopped_reason is not None:
        campaign_prospect.stopped_reason = stopped_reason
    return True
