"""
Campaign mail goes out only from the campaign's own active mailboxes (item 12).

There is no platform-address fallback: SENDER_EMAIL is for system mail (invitations, password
resets, notifications) and warm-up only. A campaign with no ACTIVE mailbox cannot be launched,
resumed or "sent now", and the scheduler leaves its queued mail waiting until a mailbox is active.
"""
from __future__ import annotations

from typing import Any, List

from app.core.errors import BadRequestError

NO_INBOX_RETRY_MINUTES = 60


def active_inboxes(campaign: Any) -> List[Any]:
    """The campaign's ACTIVE sending inboxes, ordered by address (the rotation order)."""
    return sorted(
        [i for i in (getattr(campaign, "inboxes", None) or []) if (i.status or "").upper() == "ACTIVE"],
        key=lambda inbox: (inbox.email_address or "").lower(),
    )


def require_active_inbox(campaign: Any, action: str) -> None:
    """400 when the campaign has no ACTIVE sending inbox to send from."""
    if not active_inboxes(campaign):
        raise BadRequestError(
            f"Cannot {action} this campaign: it has no active sending inbox. Connect an inbox (or "
            f"resume a paused one) and select it for the campaign, then try again.",
            code="NO_ACTIVE_INBOX",
        )
