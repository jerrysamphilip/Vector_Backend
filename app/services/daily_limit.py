"""
Daily new-contact limit (BRD v2.0 BR-OV-01).

Each user may start outreach to at most DAILY_NEW_CONTACT_LIMIT new contacts per
day (UTC). A "new contact" send is the first step of a campaign sequence; the
contact counts against its owner, or the campaign's creator when unowned.
Follow-up steps never count and keep sending after the limit is reached. Over
the limit, the first-step email is held and re-queued for the next day; the
user sees how many were held (status_for / GET /sales-reports/daily-limit).
"""
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.campaign import Campaign
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.prospect import Prospect
from app.models.user import User

HELD_CODE = "DAILY_NEW_CONTACT_LIMIT"


def _day_bounds(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def _attributed_user():
    return func.coalesce(Prospect.owner_id, Campaign.created_by)


def sent_today(db: Session, user_id: str, day: Optional[date] = None) -> int:
    start, end = _day_bounds(day or datetime.utcnow().date())
    return db.query(func.count(EmailMessage.message_id)) \
        .join(EmailSequence, EmailSequence.sequence_id == EmailMessage.sequence_id) \
        .join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id) \
        .join(Campaign, Campaign.campaign_id == EmailMessage.campaign_id) \
        .filter(EmailSequence.step_number == 1, EmailMessage.direction == "OUTBOUND",
                EmailMessage.sent_at >= start, EmailMessage.sent_at < end,
                _attributed_user() == user_id).scalar() or 0


def held_count(db: Session, user_id: str) -> int:
    return db.query(func.count(EmailMessage.message_id)) \
        .join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id) \
        .join(Campaign, Campaign.campaign_id == EmailMessage.campaign_id) \
        .filter(EmailMessage.status.in_(("QUEUED", "SCHEDULED")), EmailMessage.last_error_code == HELD_CODE,
                _attributed_user() == user_id).scalar() or 0


def status_for(db: Session, user: User) -> dict:
    limit = settings.DAILY_NEW_CONTACT_LIMIT
    used = sent_today(db, user.user_id)
    held = held_count(db, user.user_id)
    message = None
    if used >= limit:
        message = (f"You have reached today's limit of {limit} new contacts. "
                   + (f"{held} first emails are queued for tomorrow. " if held else "")
                   + "Follow-up emails keep sending.")
    return {"limit": limit, "used_today": used, "remaining": max(limit - used, 0), "held_for_tomorrow": held,
            "reached": used >= limit, "message": message}


def next_day_start() -> datetime:
    return datetime.combine(datetime.utcnow().date() + timedelta(days=1), time.min)


class DailyLimiter:
    """Per-process reservation counter used by the scheduler. Reserving does no awaits,
    so parallel inbox queues on the one event loop can never overshoot the limit."""

    def __init__(self):
        self._counts: Dict[Tuple[str, date], int] = {}

    def reserve(self, db: Session, user_id: str) -> bool:
        key = (user_id, datetime.utcnow().date())
        if key not in self._counts:
            self._counts = {k: v for k, v in self._counts.items() if k[1] == key[1]}  # drop old days
            self._counts[key] = sent_today(db, user_id, key[1])
        if self._counts[key] >= settings.DAILY_NEW_CONTACT_LIMIT:
            return False
        self._counts[key] += 1
        return True

    def release(self, user_id: str) -> None:
        key = (user_id, datetime.utcnow().date())
        if self._counts.get(key):
            self._counts[key] -= 1


def is_first_step(db: Session, email_msg: EmailMessage) -> bool:
    if not email_msg.sequence_id:
        return False
    return db.query(EmailSequence.step_number).filter(
        EmailSequence.sequence_id == email_msg.sequence_id).scalar() == 1


def enrollment_notice(db: Session, user: User, enrolled: int, prospect_ids=None, rejections=None,
                      campaign_id: Optional[str] = None) -> Optional[str]:
    """Tell the user when today's limit means some first emails will wait (BR-OV-01).
    First emails count against the contact's owner (else the campaign's creator), so when the
    enrolled contacts are known the notice checks each owner's quota, not the enrolling user's."""
    if not enrolled:
        return None
    if not prospect_ids:
        st = status_for(db, user)
        if enrolled <= st["remaining"]:
            return None
        return (f"Only {st['remaining']} of today's {st['limit']} new contacts remain for you; the first email to "
                f"the other {enrolled - st['remaining']} will go out on the following day(s). Follow-ups are not limited.")
    rejected = {r.get("prospect_id") for r in (rejections or [])}
    ids = [pid for pid in prospect_ids if pid not in rejected]
    creator = None
    if campaign_id:
        creator = db.query(Campaign.created_by).filter(Campaign.campaign_id == campaign_id).scalar()
    per_owner: Dict[str, int] = {}
    for (owner,) in db.query(Prospect.owner_id).filter(Prospect.prospect_id.in_(ids)).all() if ids else []:
        key = owner or creator or user.user_id
        per_owner[key] = per_owner.get(key, 0) + 1
    limit = settings.DAILY_NEW_CONTACT_LIMIT
    waiting = []
    for owner_id, n in per_owner.items():
        remaining = max(limit - sent_today(db, owner_id), 0)
        if n > remaining:
            who = "you" if owner_id == user.user_id else (
                " ".join(filter(None, db.query(User.first_name, User.last_name)
                                .filter(User.user_id == owner_id).first() or ())) or "the contact owner")
            waiting.append((who, remaining, n - remaining))
    if not waiting:
        return None
    parts = [f"{who} has {rem} of today's {limit} new contacts left, so {over} first email(s) will go out on the "
             f"following day(s)" if who != "you" else
             f"only {rem} of today's {limit} new contacts remain for you, so {over} first email(s) will go out on the "
             f"following day(s)" for who, rem, over in waiting]
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + ". Follow-ups are not limited."
