"""
Per-step stop rules (BR-DF-05, item 20).

Every sequence step carries stop_on_reply and stop_on_bounce (both default True). When a contact
replies or bounces, each of their remaining steps is stopped only if that step's own flag says so;
a step with the flag off still goes out. An unsubscribe, or the contact being removed from the
campaign (enrollment PAUSED), always stops every step.

stops_sequence() is the rule (pure, unit-tested); stop_pending_steps() below is the ONE entry point
that applies it to the queued steps of one contact in one campaign (for the reply handler in IMAP
sync, the SES bounce webhook and anything else that stops a contact; email_scheduler_service
re-exports it), and the scheduler and "send now" check it before every send. Messages that belong to no step (manual unified-inbox replies) are never
stopped by these rules.
"""
from __future__ import annotations

from typing import Any, Optional

from app.repositories.sending import Session, SqlAlchemySendingQueueRepository

# Enrollment status -> the step flag that decides whether the step is stopped
FLAG_FOR_STATUS = {"REPLIED": "stop_on_reply", "BOUNCED": "stop_on_bounce"}
# Enrollment statuses that stop every step whatever its flags
ALWAYS_STOP = frozenset({"UNSUBSCRIBED", "PAUSED"})
PENDING = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")


def stops_sequence(step: Any, status: Optional[str]) -> bool:
    """Whether a contact whose enrollment is ``status`` must not get ``step`` (None: no step known)."""
    status = (status or "").upper()
    if status in ALWAYS_STOP:
        return True
    flag = FLAG_FOR_STATUS.get(status)
    if flag is None:
        return False
    if step is None:
        return True  # unknown step: keep the safe default (stop)
    value = getattr(step, flag, None)
    return True if value is None else bool(value)


def stop_reason(status: str) -> str:
    return f"Stopped: contact {(status or '').lower()}"


def stop_pending_steps(db: Session, campaign_id: str, prospect_id: str, status: str,
                       failure_reason: Optional[str] = None) -> int:
    """
    Cancel this contact's queued step emails in this campaign that ``status`` (REPLIED, BOUNCED,
    UNSUBSCRIBED, PAUSED) stops under each step's own stop_on_reply / stop_on_bounce flags
    (stops_sequence). Emails with no step (manual replies) and mail already sent are left alone.
    Returns how many were cancelled. Does not commit (the caller's transaction: IMAP sync, the SES
    webhook, the scheduler).
    """
    queue = SqlAlchemySendingQueueRepository(db)
    ids = [message_id for message_id, step in queue.stoppable_steps(campaign_id, prospect_id, PENDING)
           if stops_sequence(step, status)]
    if not ids:
        return 0
    return queue.cancel(ids, failure_reason or stop_reason(status), PENDING)
