"""
Sending-pipeline alerts (S-11): problems with sending itself, shown in the alert centre next to the
domain / mailbox / campaign alerts and e-mailed to admins under the same alert preferences
(app/services/alert_center_service.py).

  NO_ACTIVE_MAILBOX        an ACTIVE campaign has mail waiting but none of its mailboxes is ACTIVE
  INBOX_LIMIT_REACHED      an ACTIVE mailbox used today's capacity and still has mail queued on it
  SEND_FAILURE_RATE        >= 5 % (warning) / >= 25 % (critical) of the last 24 h's attempts failed
  DELIVERY_EVENTS_MISSING  >= 20 % of the mail sent 15-60 minutes ago has no SES report yet
  SES_UNAVAILABLE          the SES circuit breaker (app/services/send_breaker.py, read only) is open,
                           or mail is being deferred for platform failures; warning for 30 min after
  STUCK_SENDING            mail still in SENDING 30 minutes after it was claimed

The rules and thresholds are pure (app/domain/inbox_sending.py); SQL is in
app/repositories/inbox_sending_alerts.py. The alerts are computed by the ``sending_alerts`` job
(app/jobs.py, every minute: ``run_job``) for the e-mails, and on request for the dashboard. The breaker
state lives in the memory of the process that sends (the worker), so the job, running there, copies
it into platform_flags (``SES_BREAKER_FLAG``) where every process reads it.

This module also supplies the mailbox fields of the email accounts page (F-20): pause reason and
time, today's capacity and what is left of it, and the "Resume sending" action.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.core.clock import utcnow
from app.core.config import settings
from app.core.database import SessionLocal
from app.domain import inbox_sending as rules
from app.domain.inbox_sending import SENDING_ALERT_TYPES
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.repositories.inbox_sending_alerts import PAUSE_EVENT_PREFIX, RESUME_EVENT_SUBJECT, SendingAlertRepository
from app.services import platform_flags, send_breaker
from app.services.send_errors import PLATFORM_CODES

logger = logging.getLogger(__name__)

SES_BREAKER_FLAG = "ses_breaker_open_until"
SOURCE_SENDING = "SENDING"
SOURCE_EMAIL = "EMAIL"
MAX_REASON_CHARS = 160
# Platform failures are recorded by error code, or by the failure kind when SES gave none
_PLATFORM_ERROR_CODES = tuple(PLATFORM_CODES) + ("PLATFORM",)


def _alert(alert_id: str, alert_type: str, severity: str, title: str, message: str,
           created_at: Optional[datetime], *, source: str = SOURCE_SENDING, route: str = "/app/campaigns",
           action_label: str = "Open campaigns", scope_type: str = "TENANT",
           scope_id: Optional[str] = None) -> Dict[str, Any]:
    return {"id": alert_id, "source": source, "severity": severity, "title": title, "message": message,
            "created_at": created_at, "route": route, "action_label": action_label, "alert_type": alert_type,
            "scope_type": scope_type, "scope_id": scope_id}


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ── Breaker state shared across processes ────────────────────────────────────
def record_breaker_state(now: Optional[datetime] = None) -> Optional[datetime]:
    """Copy this process's SES breaker state to platform_flags while it is open (read only on the
    breaker: ``retry_in`` never half-opens it). Returns the open-until time written, if any."""
    retry_in = send_breaker.platform_breaker.retry_in()
    if retry_in <= 0:
        return None
    until = (now or utcnow()) + timedelta(seconds=retry_in)
    platform_flags.set_flag(SES_BREAKER_FLAG, until.isoformat())
    return until


def breaker_open_until(read: Callable[[str], Optional[str]] = platform_flags.get) -> Optional[datetime]:
    try:
        value = read(SES_BREAKER_FLAG)
        return datetime.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None
    except Exception:  # the flag store is unavailable: no alert rather than a broken alert list
        logger.warning("[SendingAlerts] Could not read the SES breaker state", exc_info=True)
        return None


# ── Collectors ────────────────────────────────────────────────────────────────
def _no_active_mailbox(repo: SendingAlertRepository, tenant_id: str, now: datetime) -> List[Dict[str, Any]]:
    return [
        _alert(f"no-active-mailbox:{c.campaign_id}", rules.ALERT_NO_ACTIVE_MAILBOX, rules.SEVERITY_CRITICAL,
               "Campaign has no active mailbox",
               f"{c.campaign_name} has {c.pending} email(s) waiting, but none of its mailboxes is active, so "
               "nothing can be sent. Resume one of its mailboxes or add another one to the campaign.",
               c.updated_at or now, source=SOURCE_EMAIL, route=f"/app/campaigns/{c.campaign_id}",
               action_label="Open campaign")
        for c in repo.campaigns_without_active_mailbox(tenant_id)
    ]


def _inbox_limit(repo: SendingAlertRepository, inboxes: List[SendingInbox], now: datetime) -> List[Dict[str, Any]]:
    active = [i for i in inboxes if (i.status or "").upper() == "ACTIVE"]
    sent = repo.sent_since_by_inbox([i.inbox_id for i in active], day_start(now))
    full = [(i, cap) for i in active
            for cap in [rules.daily_capacity(i.current_daily_limit, i.delay_between_emails)]
            if cap > 0 and rules.remaining_today(cap, sent.get(i.inbox_id, 0)) == 0]
    waiting = repo.pending_by_inbox([i.inbox_id for i, _ in full])
    return [
        _alert(f"inbox-limit:{inbox.inbox_id}:{now.date().isoformat()}", rules.ALERT_INBOX_LIMIT_REACHED,
               rules.SEVERITY_INFO, "Daily sending limit reached",
               f"{inbox.email_address} has sent its {cap} emails for today. {waiting[inbox.inbox_id]} queued "
               "email(s) go out from another mailbox with room left, or after the daily reset.",
               inbox.last_sent_at or now, source=SOURCE_EMAIL, route="/app/email-accounts",
               action_label="Open Email Accounts", scope_type="INBOX", scope_id=inbox.inbox_id)
        for inbox, cap in full if waiting.get(inbox.inbox_id)
    ]


def _failure_rate(repo: SendingAlertRepository, tenant_id: str, now: datetime) -> List[Dict[str, Any]]:
    failed, sent, reason = repo.send_outcomes(tenant_id, now - timedelta(hours=rules.SEND_FAILURE_WINDOW_HOURS))
    severity = rules.send_failure_severity(failed, sent)
    if severity is None:
        return []
    share = 100.0 * failed / (failed + sent)
    latest = (reason or "").strip()[:MAX_REASON_CHARS]
    return [_alert(f"send-failure-rate:{severity}", rules.ALERT_SEND_FAILURE_RATE, severity,
                   "Emails are failing to send",
                   f"{failed} of {failed + sent} emails ({share:.0f}%) failed to send in the last "
                   f"{rules.SEND_FAILURE_WINDOW_HOURS} hours." + (f" Latest reason: {latest}" if latest else ""),
                   now)]


def _delivery_gap(repo: SendingAlertRepository, tenant_id: str, now: datetime) -> List[Dict[str, Any]]:
    confirm = int(getattr(settings, "DELIVERY_CONFIRM_MINUTES", rules.DELIVERY_CONFIRM_MINUTES))
    sample, missing = repo.delivery_gap(tenant_id, now - timedelta(minutes=rules.DELIVERY_GAP_WINDOW_MINUTES),
                                        now - timedelta(minutes=confirm))
    if not rules.delivery_reports_missing(sample, missing):
        return []
    return [_alert("delivery-events-missing", rules.ALERT_DELIVERY_EVENTS_MISSING, rules.SEVERITY_CRITICAL,
                   "Delivery reports are missing",
                   f"{missing} of the {sample} emails sent in the last hour still have no delivery result after "
                   f"{confirm} minutes. Reports from Amazon SES may not be reaching the app, so email "
                   "statuses can be out of date.", now)]


def _ses_unavailable(repo: SendingAlertRepository, tenant_id: str, now: datetime,
                     open_until: Optional[datetime]) -> List[Dict[str, Any]]:
    deferred = repo.platform_deferrals(tenant_id, _PLATFORM_ERROR_CODES,
                                       now - timedelta(minutes=send_breaker.DEFER_MINUTES * 2))
    severity = rules.ses_outage_severity(open_until, deferred, now)
    if severity is None:
        return []
    ongoing = severity == rules.SEVERITY_CRITICAL
    message = ("Amazon SES is refusing or not answering, so sending is paused for a few minutes at a time. "
               if ongoing else "Amazon SES stopped answering for a while; sending has resumed. ")
    message += "Queued emails are retried automatically; no action is needed unless this keeps happening."
    return [_alert(f"ses-unavailable:{(open_until or now).isoformat() if open_until else 'deferrals'}",
                   rules.ALERT_SES_UNAVAILABLE, severity, "Email sending is paused by the provider"
                   if ongoing else "Email sending was paused by the provider", message, open_until or now)]


def _stuck_sending(repo: SendingAlertRepository, tenant_id: str, now: datetime) -> List[Dict[str, Any]]:
    stuck = repo.stuck_sending(tenant_id, now - timedelta(minutes=rules.STUCK_SENDING_MINUTES))
    if not stuck:
        return []
    return [_alert("stuck-sending", rules.ALERT_STUCK_SENDING, rules.SEVERITY_WARNING,
                   "Emails are stuck while sending",
                   f"{stuck} email(s) have been in 'sending' for more than {rules.STUCK_SENDING_MINUTES} minutes. "
                   "The app puts them right automatically; if this keeps coming back, contact your administrator.",
                   now)]


def _safely(db: Any, label: str, collect: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """One failing collector must not take the whole alert list down. A failed query leaves the
    session unusable until it is rolled back; the alert path only reads."""
    try:
        return collect()
    except Exception:
        logger.error("[SendingAlerts] Could not build the %s alerts", label, exc_info=True)
        try:
            db.rollback()
        except Exception:
            logger.warning("[SendingAlerts] Rollback after the %s alerts failed", label, exc_info=True)
        return []


def collect_sending_alerts(db: Any, tenant_id: str, inboxes: Optional[List[SendingInbox]] = None,
                           now: Optional[datetime] = None,
                           open_until: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every sending-pipeline alert of one tenant, in the alert centre's dict shape."""
    now = now or utcnow()
    repo = SendingAlertRepository(db)
    tenant_inboxes = inboxes if inboxes is not None else repo.tenant_inboxes(tenant_id)
    until = open_until if open_until is not None else breaker_open_until()
    return [
        *_safely(db, "no active mailbox", lambda: _no_active_mailbox(repo, tenant_id, now)),
        *_safely(db, "daily limit", lambda: _inbox_limit(repo, tenant_inboxes, now)),
        *_safely(db, "send failures", lambda: _failure_rate(repo, tenant_id, now)),
        *_safely(db, "delivery reports", lambda: _delivery_gap(repo, tenant_id, now)),
        *_safely(db, "SES outage", lambda: _ses_unavailable(repo, tenant_id, now, until)),
        *_safely(db, "stuck sending", lambda: _stuck_sending(repo, tenant_id, now)),
    ]


# ── Mailbox status for the email accounts page (F-20) ─────────────────────────
def mailbox_status(db: Any, tenant_id: str, inboxes: List[SendingInbox],
                   now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """inbox id -> paused_reason, paused_at, daily_capacity, sent_today, remaining_today (3 queries)."""
    now = now or utcnow()
    repo = SendingAlertRepository(db)
    ids = [i.inbox_id for i in inboxes]
    sent = repo.sent_since_by_inbox(ids, day_start(now))
    paused = [i.inbox_id for i in inboxes if (i.status or "").upper() == "PAUSED"]
    reasons = repo.last_pause_events(tenant_id, paused)
    status: Dict[str, Dict[str, Any]] = {}
    for inbox in inboxes:
        capacity = rules.daily_capacity(inbox.current_daily_limit, inbox.delay_between_emails)
        info = reasons.get(inbox.inbox_id)
        is_paused = inbox.inbox_id in paused
        status[inbox.inbox_id] = {
            "paused_reason": (info.reason if info else "Paused by a user") if is_paused else None,
            "paused_at": info.paused_at if (is_paused and info) else None,
            "daily_capacity": capacity,
            "sent_today": sent.get(inbox.inbox_id, 0),
            "remaining_today": 0 if is_paused else rules.remaining_today(capacity, sent.get(inbox.inbox_id, 0)),
        }
    return status


class InboxNotFound(LookupError):
    pass


def resume_inbox(db: Any, user: User, inbox_id: str) -> SendingInbox:
    """Set a PAUSED mailbox ACTIVE again and log it in its activity (who resumed it). Idempotent for an
    ACTIVE mailbox. InboxNotFound for another tenant's mailbox. Commits."""
    repo = SendingAlertRepository(db)
    inbox = repo.get_inbox(user.tenant_id, inbox_id, lock=True)
    if inbox is None:
        raise InboxNotFound("Inbox not found")
    if (inbox.status or "").upper() == "PAUSED":
        inbox.status = "ACTIVE"
        repo.add_inbox_event(inbox, RESUME_EVENT_SUBJECT,
                             f"Sending resumed by {user.email or user.user_id}.", "MAILBOX_RESUMED", utcnow())
    db.commit()
    db.refresh(inbox)
    return inbox


# ── Job ──────────────────────────────────────────────────────────────────────
def run_job(session_factory: Callable[[], Any] = SessionLocal) -> Dict[str, List[str]]:
    """The sending_alerts job (blocking part): record the breaker state and compute every tenant's
    sending alerts. Returns the tenants that have some, whose admins the job then e-mails (alert
    centre e-mails limited to SENDING_ALERT_TYPES, under each admin's preferences and cooldowns)."""
    record_breaker_state()
    until = breaker_open_until()
    with_alerts: List[str] = []
    with session_factory() as db:
        for tenant_id in SendingAlertRepository(db).tenant_ids():
            if collect_sending_alerts(db, tenant_id, open_until=until):
                with_alerts.append(tenant_id)
            db.rollback()  # read only: end each tenant's read transaction
    return {"tenants_with_alerts": with_alerts}


__all__ = ["PAUSE_EVENT_PREFIX", "SENDING_ALERT_TYPES", "collect_sending_alerts", "mailbox_status", "resume_inbox", "run_job"]
