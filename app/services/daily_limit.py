"""
Daily new-contact limit (BRD v2.0 BR-OV-01).

Each user may start outreach to at most DAILY_NEW_CONTACT_LIMIT new contacts per
day (UTC). A "new contact" send is the first step of a campaign sequence; the
contact counts against its owner, or the campaign's creator when unowned.
Follow-up steps never count and keep sending after the limit is reached. Over
the limit, the first-step email is held and re-queued for the next day; the
user sees how many were held (status_for / GET /sales-reports/daily-limit).

The senders (the worker's scheduler and send-now in every API replica) reserve a slot before each
first-step send through DailyLimiter, whose per-user, per-day counter is shared by every process
(DEC-09): a ``dnc:<day>:<user>`` counter in platform_flags, seeded on first use of the day with the
first steps already sent that day (email_messages), then raised by one atomic conditional UPDATE per
reservation, so N processes together never pass the limit. A send that does not go out releases its slot.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, Optional, Tuple

from app.core.clock import utcnow
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.email_message import EmailMessage
from app.models.user import User
from app.repositories.daily_limit import Session, daily_limit_repo
from app.repositories.platform_flags import PlatformFlagRepository

logger = logging.getLogger(__name__)

HELD_CODE = "DAILY_NEW_CONTACT_LIMIT"


def _day_bounds(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def sent_today(db: Session, user_id: str, day: Optional[date] = None) -> int:
    start, end = _day_bounds(day or utcnow().date())
    return daily_limit_repo(db).first_steps_sent(user_id, start, end)


def held_count(db: Session, user_id: str) -> int:
    return daily_limit_repo(db).held(user_id, HELD_CODE)


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
    return datetime.combine(utcnow().date() + timedelta(days=1), time.min)


COUNTER_PREFIX = "dnc:"


def counter_name(user_id: str, day: date) -> str:
    """The shared counter of one user's day (sorts by day, so old days can be range-deleted)."""
    return f"{COUNTER_PREFIX}{day.isoformat()}:{user_id}"


class DailyLimiter:
    """Reservations against the daily new-contact limit, shared by every process (module docstring).

    If the shared counter cannot be used (database error), it falls back to counting in this process
    (seeded from the sends of the day, as before DEC-09) and logs an ERROR: degraded to a per-process
    limit, never unlimited."""

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory
        self._counts: Dict[Tuple[str, date], int] = {}  # fallback only
        self._purged_day: Optional[date] = None

    def reserve(self, db: Session, user_id: str) -> bool:
        day = utcnow().date()
        try:
            return self._reserve_shared(db, user_id, day)
        except Exception as exc:  # noqa: BLE001 - degrade to the per-process counter, loudly
            logger.error("Daily new-contact limit: shared counter unavailable (%s); counting in this "
                         "process only", type(exc).__name__)
            return self._reserve_local(db, user_id, day)

    def _reserve_shared(self, db: Session, user_id: str, day: date) -> bool:
        name = counter_name(user_id, day)
        with self._session_factory() as s:
            flags = PlatformFlagRepository(s)
            if flags.get(name) is None:
                flags.ensure_counter(name, sent_today(db, user_id, day))
            ok = flags.increment_below(name, settings.DAILY_NEW_CONTACT_LIMIT)
            if self._purged_day != day:  # first reservation of the day here: drop counters before yesterday
                flags.remove_range(COUNTER_PREFIX, counter_name("", day - timedelta(days=1)))
            s.commit()
        self._purged_day = day
        return ok

    def _reserve_local(self, db: Session, user_id: str, day: date) -> bool:
        key = (user_id, day)
        if key not in self._counts:
            self._counts = {k: v for k, v in self._counts.items() if k[1] == day}  # drop old days
            self._counts[key] = sent_today(db, user_id, day)
        if self._counts[key] >= settings.DAILY_NEW_CONTACT_LIMIT:
            return False
        self._counts[key] += 1
        return True

    def release(self, user_id: str) -> None:
        day = utcnow().date()
        key = (user_id, day)
        if self._counts.get(key):  # reserved through the fallback
            self._counts[key] -= 1
            return
        try:
            with self._session_factory() as s:
                PlatformFlagRepository(s).adjust(counter_name(user_id, day), -1)
                s.commit()
        except Exception as exc:  # noqa: BLE001 - the slot stays used: fewer sends today, never more
            logger.error("Daily new-contact limit: could not release a slot for %s (%s)", user_id,
                         type(exc).__name__)


def is_first_step(db: Session, email_msg: EmailMessage) -> bool:
    if not email_msg.sequence_id:
        return False
    return daily_limit_repo(db).step_number(email_msg.sequence_id) == 1


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
    repo = daily_limit_repo(db)
    creator = repo.campaign_creator(campaign_id) if campaign_id else None
    per_owner: Dict[str, int] = {}
    for owner in repo.owners(ids):
        key = owner or creator or user.user_id
        per_owner[key] = per_owner.get(key, 0) + 1
    limit = settings.DAILY_NEW_CONTACT_LIMIT
    waiting = []
    for owner_id, n in per_owner.items():
        remaining = max(limit - sent_today(db, owner_id), 0)
        if n > remaining:
            who = "you" if owner_id == user.user_id else (
                " ".join(filter(None, repo.user_name_parts(owner_id))) or "the contact owner")
            waiting.append((who, remaining, n - remaining))
    if not waiting:
        return None
    parts = [f"{who} has {rem} of today's {limit} new contacts left, so {over} first email(s) will go out on the "
             f"following day(s)" if who != "you" else
             f"only {rem} of today's {limit} new contacts remain for you, so {over} first email(s) will go out on the "
             f"following day(s)" for who, rem, over in waiting]
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + ". Follow-ups are not limited."
