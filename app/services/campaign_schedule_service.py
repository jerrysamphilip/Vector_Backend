# app/services/campaign_schedule_service.py
"""
Manual schedule changes for a campaign's not-yet-sent emails: move one message, or bulk-move
every queued message of one sequence step on one day, snapped into the campaign's send window.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date as date_type, datetime, time as time_type, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, NotFoundError
from app.models.campaign import Campaign
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.utils.business_calendar import get_next_business_day, is_business_day

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_START = time_type(9, 0)
DEFAULT_WINDOW_END = time_type(17, 0)
UNRESCHEDULABLE_STATUSES = ("SENT", "FAILED")
BULK_RESCHEDULABLE_STATUSES = ["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]
MAX_JITTER_MINUTES = 5
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class SnappedTime:
    local: datetime        # aware, campaign timezone
    utc_naive: datetime    # what is stored
    adjusted: bool         # differs from what was requested


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ──────────────────────────────────────────────────────────────────────────────

def campaign_zone(tz_str: str) -> ZoneInfo:
    """The campaign's timezone; UTC when the stored name is unknown or malformed."""
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def snap_to_send_window(requested: datetime, tz: ZoneInfo, win_start: time_type, win_end: time_type) -> SnappedTime:
    """
    Snap a requested time (naive = UTC) to a valid send slot in the campaign timezone:
      - business day, inside the window -> keep the exact minute
      - business day, before the window -> window open that day
      - after the window, or weekend / US holiday -> next business day at window open
    """
    requested_utc = requested if requested.tzinfo is not None else requested.replace(tzinfo=UTC)
    local_requested = requested_utc.astimezone(tz)
    local_time_only = local_requested.time()
    is_biz = is_business_day(local_requested)

    if is_biz and win_start <= local_time_only <= win_end:
        snapped_local = local_requested.replace(second=0, microsecond=0)
    elif is_biz and local_time_only < win_start:
        snapped_local = local_requested.replace(hour=win_start.hour, minute=win_start.minute, second=0, microsecond=0)
    else:
        # After close on a business day -> search from tomorrow; weekend/holiday -> from today
        start_search = local_requested + timedelta(days=1) if is_biz and local_time_only > win_end else local_requested
        snapped_local, skip_reason = get_next_business_day(start_search)
        snapped_local = snapped_local.replace(hour=win_start.hour, minute=win_start.minute, second=0, microsecond=0)
        if skip_reason:
            logger.info(f"[RescheduleDate] Time adjusted: {skip_reason}")

    utc_naive = snapped_local.astimezone(UTC).replace(tzinfo=None)
    return SnappedTime(local=snapped_local, utc_naive=utc_naive, adjusted=utc_naive != requested.replace(tzinfo=None))


def jittered(base: datetime, jitter_min: int, jitter_sec: int) -> datetime:
    """base + jitter minutes (wrapping the hour within the same day) with the given seconds."""
    base_min = base.minute + jitter_min
    return base.replace(hour=(base.hour + base_min // 60) % 24, minute=base_min % 60, second=jitter_sec)


def describe_adjustment(snapped_local: datetime, tz_str: str) -> str:
    """User-facing explanation of why the time moved (cross-platform, no %-d / %-I)."""
    hour_num = str(snapped_local.hour % 12 or 12)
    am_pm = 'AM' if snapped_local.hour < 12 else 'PM'
    return (
        f"Requested time was outside the safe sending window or fell on a weekend/holiday. "
        f"Emails rescheduled to {snapped_local.strftime('%A')}, {snapped_local.strftime('%B')} "
        f"{snapped_local.day} at {hour_num}:{snapped_local.strftime('%M')} {am_pm} ({tz_str})."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────────

class CampaignScheduleService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def reschedule_message(
        self, campaign_id: str, message_id: str, new_scheduled_at: datetime, reason: Optional[str]
    ) -> Dict[str, Any]:
        """Move one queued/scheduled message to an exact time (no window snapping)."""
        message = self.db.query(EmailMessage).filter(
            EmailMessage.message_id == message_id,
            EmailMessage.campaign_id == campaign_id
        ).first()
        if not message:
            raise NotFoundError("Email message not found")
        if message.status in UNRESCHEDULABLE_STATUSES:
            raise BadRequestError(f"Cannot reschedule a message with status '{message.status}'")

        old_scheduled_at = message.scheduled_at
        message.scheduled_at = new_scheduled_at
        self.db.commit()

        logger.info(
            f"[Reschedule] Message {message_id} rescheduled from {old_scheduled_at} "
            f"to {new_scheduled_at} in campaign {campaign_id}. "
            f"Reason: {reason or 'none'}"
        )
        return {
            "status": "rescheduled",
            "message_id": message_id,
            "campaign_id": campaign_id,
            "old_scheduled_at": old_scheduled_at.isoformat() if old_scheduled_at else None,
            "new_scheduled_at": new_scheduled_at.isoformat(),
            "reason": reason,
        }

    def reschedule_date(
        self,
        campaign: Campaign,
        date: str,
        step_number: int,
        new_scheduled_at: datetime,
        reason: Optional[str],
    ) -> Dict[str, Any]:
        """Bulk-move one step's queued messages on one day into the send window, with 0-5 min jitter
        per message so they do not all queue together."""
        campaign_id = campaign.campaign_id
        try:
            target_date = date_type.fromisoformat(date)
        except ValueError:
            raise BadRequestError("Invalid date format. Use YYYY-MM-DD.")

        tz_str = campaign.campaign_timezone or "UTC"
        win_start = campaign.send_window_start or DEFAULT_WINDOW_START
        win_end = campaign.send_window_end or DEFAULT_WINDOW_END
        snapped = snap_to_send_window(new_scheduled_at, campaign_zone(tz_str), win_start, win_end)

        step_sequence_ids = [
            s.sequence_id for s in self.db.query(EmailSequence).filter(
                EmailSequence.campaign_id == campaign_id,
                EmailSequence.step_number == step_number
            ).all()
        ]
        if not step_sequence_ids:
            raise NotFoundError(f"No sequence found for step {step_number} in this campaign.")

        messages = self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.sequence_id.in_(step_sequence_ids),
            EmailMessage.status.in_(BULK_RESCHEDULABLE_STATUSES),
            func.date(EmailMessage.scheduled_at) == target_date
        ).all()
        if not messages:
            return {
                "status": "no_messages",
                "message": f"No queued messages found for date {date} and step {step_number}.",
                "updated_count": 0,
            }

        updated_ids = []
        for msg in messages:
            msg.scheduled_at = jittered(snapped.utc_naive, random.randint(0, MAX_JITTER_MINUTES), random.randint(0, 59))
            updated_ids.append(msg.message_id)
        self.db.commit()

        logger.info(
            f"[RescheduleDate] {len(updated_ids)} messages rescheduled to "
            f"~{snapped.utc_naive} UTC (tz={tz_str}, window={win_start}-{win_end}) "
            f"for campaign {campaign_id}, date={date}, step={step_number}. "
            f"Time adjusted={snapped.adjusted}."
        )
        return {
            "status": "rescheduled",
            "campaign_id": campaign_id,
            "date": date,
            "step_number": step_number,
            "requested_at": new_scheduled_at.isoformat(),
            "snapped_utc": snapped.utc_naive.isoformat(),
            "snapped_local": snapped.local.strftime("%Y-%m-%d %H:%M") + f" {tz_str}",
            "time_adjusted": snapped.adjusted,
            "adjustment_reason": describe_adjustment(snapped.local, tz_str) if snapped.adjusted else None,
            "timezone": tz_str,
            "send_window": f"{win_start.strftime('%H:%M')}–{win_end.strftime('%H:%M')}",
            "updated_count": len(updated_ids),
            "updated_message_ids": updated_ids,
            "reason": reason,
        }
