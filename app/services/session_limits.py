"""Session limits on refresh: absolute cap, idle timeout, refresh-token reuse detection (ADR 0007).

A session is one sign-in. Every refresh token it issues shares a ``session_id`` (the rotation family)
and carries ``session_started_at`` and ``last_activity_at`` forward through rotation. At POST
/api/auth/refresh:

- absolute cap: refused once ``now - session_started_at >= SESSION_ABSOLUTE_HOURS``;
- idle timeout: refused once ``now - last_activity_at > SESSION_IDLE_MINUTES``. Only the user counts
  as activity: the SPA reports the seconds since the last pointer / keyboard / visibility event in
  the ``X-User-Activity`` header, and ``last_activity_at`` only moves forward when that header is sent
  (background polling refreshes without it);
- reuse: presenting a token that was already rotated or revoked revokes every token of its family
  (the standard defence against a stolen, replayed refresh token). A token rotated less than
  REFRESH_REUSE_GRACE_SECONDS ago is a benign race (two tabs refreshing at once): refused, nothing
  revoked, the browser's cookies (already holding the winner's tokens) are left alone.

The limits are checked when the access token is renewed, so they bite within one access-token
lifetime (ACCESS_TOKEN_EXPIRE_MINUTES): an idle browser keeps its current access token until it expires.

The decision functions are pure (unit tested in tests/unit/test_session_limits.py); ``begin_refresh``
applies them to the database.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.models.refresh_token import RefreshToken

logger = logging.getLogger(__name__)

ACTIVITY_HEADER = "X-User-Activity"
# A reported idle time longer than this is nonsense; ignore it rather than overflow timedelta
_MAX_REPORTED_IDLE_SECONDS = 10 * 365 * 24 * 3600

REASON_ROTATED = "rotated"
REASON_LOGOUT = "logout"
REASON_REUSE = "reuse"
REASON_IDLE = "idle"
REASON_ABSOLUTE = "absolute"

CODE_INVALID = "REFRESH_TOKEN_INVALID"
CODE_SUPERSEDED = "REFRESH_TOKEN_SUPERSEDED"
CODE_REUSED = "REFRESH_TOKEN_REUSED"
CODE_IDLE = "SESSION_IDLE_TIMEOUT"
CODE_ABSOLUTE = "SESSION_EXPIRED"


class Presented(str, Enum):
    ACTIVE = "active"   # not revoked: may be exchanged
    RACE = "race"       # rotated within the grace window: refuse quietly
    REUSE = "reuse"     # revoked earlier (or for another reason): revoke the family


class SessionRefused(Exception):
    """The refresh is refused (401). ``clear_cookies`` is False only for the benign two-tab race."""

    def __init__(self, code: str, message: str, clear_cookies: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.clear_cookies = clear_cookies

    @property
    def detail(self) -> dict | str:
        # Invalid / expired keeps its historical plain-text body; the new outcomes carry a code
        if self.code == CODE_INVALID:
            return self.message
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class SessionLineage:
    """What a rotated refresh token inherits from the one it replaces."""
    session_id: str
    session_started_at: datetime
    last_activity_at: datetime


# ── Pure decision logic ──

def parse_activity_header(value: str | None) -> int | None:
    """Seconds since the user's last interaction, from X-User-Activity; None when absent or malformed."""
    if value is None:
        return None
    text = value.strip()
    if not text.isdigit():
        return None
    seconds = int(text)
    return seconds if seconds <= _MAX_REPORTED_IDLE_SECONDS else None


def effective_last_activity(stored: datetime, now: datetime, reported_idle_seconds: int | None) -> datetime:
    """The session's last activity after this request: the reported interaction time when it is newer
    than the stored one (never in the future, never moving backwards); unchanged without a report."""
    if reported_idle_seconds is None:
        return stored
    reported = now - timedelta(seconds=reported_idle_seconds)
    return min(now, max(stored, reported))


def limit_exceeded(*, now: datetime, session_started_at: datetime, last_activity_at: datetime,
                   absolute: timedelta, idle: timedelta) -> str | None:
    """REASON_ABSOLUTE / REASON_IDLE when the session is over, None while it may continue."""
    if now - session_started_at >= absolute:
        return REASON_ABSOLUTE
    if now - last_activity_at > idle:
        return REASON_IDLE
    return None


def classify_presented(*, revoked_at: datetime | None, revoked_reason: str | None, now: datetime,
                       grace: timedelta) -> Presented:
    if revoked_at is None:
        return Presented.ACTIVE
    if revoked_reason == REASON_ROTATED and now - revoked_at <= grace:
        return Presented.RACE
    return Presented.REUSE


def refresh_expiry(*, now: datetime, session_started_at: datetime, refresh_ttl: timedelta,
                   absolute: timedelta) -> datetime:
    """A refresh token never outlives its session's absolute cap."""
    return min(now + refresh_ttl, session_started_at + absolute)


def idle_message(idle: timedelta) -> str:
    minutes = int(idle.total_seconds() // 60)
    return f"Signed out after {minutes} minute{'s' if minutes != 1 else ''} of inactivity"


# ── Settings ──

def absolute_limit() -> timedelta:
    return timedelta(hours=settings.SESSION_ABSOLUTE_HOURS)


def idle_limit() -> timedelta:
    return timedelta(minutes=settings.SESSION_IDLE_MINUTES)


def reuse_grace() -> timedelta:
    return timedelta(seconds=settings.REFRESH_REUSE_GRACE_SECONDS)


def refresh_ttl() -> timedelta:
    return timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


# ── Database ──

def new_lineage(now: datetime) -> SessionLineage:
    """A fresh sign-in: a new family, started and last active now."""
    return SessionLineage(session_id=str(uuid.uuid4()), session_started_at=now, last_activity_at=now)


def revoke_family(db: Session, session_id: str, now: datetime, reason: str) -> int:
    """Revoke every still-active token of a session. Does not commit."""
    return db.query(RefreshToken).filter(
        RefreshToken.session_id == session_id,
        RefreshToken.revoked_at.is_(None),
    ).update({RefreshToken.revoked_at: now, RefreshToken.revoked_reason: reason}, synchronize_session=False)


def begin_refresh(db: Session, token_hash: str, activity_header: str | None,
                  now: datetime | None = None) -> tuple[RefreshToken, SessionLineage]:
    """Validate a presented refresh token against reuse, expiry and the session limits.

    Returns the (locked) token and the lineage its replacement inherits; the caller checks the user,
    calls ``mark_rotated`` and issues the new pair. Raises SessionRefused (after committing any
    revocation it made) otherwise."""
    now = now or utcnow()
    rt = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).with_for_update().first()
    if rt is None:
        raise SessionRefused(CODE_INVALID, "Invalid or expired refresh token")

    session_id = rt.session_id or rt.token_id
    presented = classify_presented(revoked_at=rt.revoked_at, revoked_reason=rt.revoked_reason,
                                   now=now, grace=reuse_grace())
    if presented is Presented.RACE:
        db.rollback()  # release the row lock
        raise SessionRefused(CODE_SUPERSEDED, "Refresh token already exchanged", clear_cookies=False)
    if presented is Presented.REUSE:
        revoked = revoke_family(db, session_id, now, REASON_REUSE)
        db.commit()
        logger.warning("Refresh token reuse: user=%s session=%s token=%s (revoked at %s, reason=%s); "
                       "%d active token(s) of the session revoked", rt.user_id, session_id, rt.token_id,
                       rt.revoked_at, rt.revoked_reason, revoked)
        raise SessionRefused(CODE_REUSED, "Session revoked, please sign in again")

    started = rt.session_started_at or rt.created_at or now
    stored_activity = rt.last_activity_at or rt.created_at or now
    last_activity = effective_last_activity(stored_activity, now, parse_activity_header(activity_header))
    reason = limit_exceeded(now=now, session_started_at=started, last_activity_at=last_activity,
                            absolute=absolute_limit(), idle=idle_limit())
    if reason is not None:
        rt.revoked_at = now
        rt.revoked_reason = reason
        db.commit()
        if reason == REASON_IDLE:
            raise SessionRefused(CODE_IDLE, idle_message(idle_limit()))
        raise SessionRefused(CODE_ABSOLUTE, "Session expired, please sign in again")

    if rt.expires_at < now:
        db.rollback()
        raise SessionRefused(CODE_INVALID, "Invalid or expired refresh token")

    return rt, SessionLineage(session_id=session_id, session_started_at=started, last_activity_at=last_activity)


def mark_rotated(rt: RefreshToken, now: datetime | None = None) -> None:
    rt.revoked_at = now or utcnow()
    rt.revoked_reason = REASON_ROTATED
