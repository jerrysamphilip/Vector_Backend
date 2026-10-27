"""Daily user activity for CRM adoption (BRD v2.0 section 8: active users per week / licensed users).

A user is active on a (UTC) day when they sign in (app/routers/auth_router.py _issue_tokens) or make
an authenticated API request (app/core/auth.py get_current_user). One row per user per day goes to
user_activity_days with INSERT IGNORE on its own short connection, so it never touches the request's
transaction. An in-process cache keeps it to one write per user per day per API process: the first
request of the day pays one small insert, every later one a dictionary lookup.
"""
import logging
import threading
from datetime import date
from typing import Dict, Optional

from sqlalchemy import text

from app.core.clock import utcnow

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_recorded: Dict[str, date] = {}       # user_id -> last UTC day written by this process
_MAX_CACHE = 50_000                   # plenty for one workspace fleet; cleared wholesale when exceeded


def utc_today() -> date:
    return utcnow().date()


def should_record(user_id: str, day: date) -> bool:
    """True the first time (user, day) is seen by this process; marks it as seen."""
    with _lock:
        if _recorded.get(user_id) == day:
            return False
        if len(_recorded) >= _MAX_CACHE:
            _recorded.clear()
        _recorded[user_id] = day
        return True


def forget(user_id: Optional[str] = None) -> None:
    """Drop the cache (tests), or one user's entry."""
    with _lock:
        if user_id is None:
            _recorded.clear()
        else:
            _recorded.pop(user_id, None)


def record(bind, tenant_id: Optional[str], user_id: Optional[str], day: Optional[date] = None) -> None:
    """Mark the user active today. bind: an Engine (or a Session's bind). Never raises: a failed
    write is logged and not retried until the next day (it must never break a request)."""
    if not tenant_id or not user_id:
        return  # PLATFORM_ADMIN has no workspace: not a licensed user
    day = day or utc_today()
    if not should_record(user_id, day):
        return
    try:
        engine = getattr(bind, "engine", bind)
        with engine.begin() as conn:
            conn.execute(text("INSERT IGNORE INTO user_activity_days (tenant_id, user_id, day) VALUES (:t, :u, :d)"),
                         {"t": tenant_id, "u": user_id, "d": day})
    except Exception as exc:  # table missing before the migration, database hiccup, ...
        logger.debug("[Activity] Could not record activity for %s: %s", user_id, exc)


def record_for(db, user) -> None:
    """record() for a loaded User through the request session's engine."""
    if user is None or user.role == "PLATFORM_ADMIN":
        return
    try:
        bind = db.get_bind()
    except Exception as exc:  # never fail a request over activity tracking
        logger.debug("Activity tracking skipped for user %s: no bind (%s)", user.user_id, exc)
        return
    record(bind, user.tenant_id, user.user_id)
