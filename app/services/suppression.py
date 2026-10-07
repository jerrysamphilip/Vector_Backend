"""
One rule for who must not be emailed (BR-DF-08).

Enrollment, the send-time check and every unsubscribe source (link click,
one-click header, spam complaint, hard bounce, manual change on the contact)
go through here, so a contact who unsubscribes is excluded from every campaign
in the workspace at once, not just the one they clicked from.

A suppression is "active" while it has no expiry or the expiry is in the
future. Enrollment and sending use the same test. After a voluntary
unsubscribe expires the contact still stays unsubscribed (consent_status) until
someone deliberately re-opts them in, so expiry alone never restarts email.
"""
import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional, Set

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CampaignProspect, EmailMessage, GlobalUnsubscribe, Prospect
from app.utils.campaign_prospect_status import set_prospect_status

logger = logging.getLogger(__name__)

PENDING = ("QUEUED", "SCHEDULED")


def active_clause(now: Optional[datetime] = None):
    now = now or datetime.utcnow()
    return or_(GlobalUnsubscribe.suppression_expires_at.is_(None), GlobalUnsubscribe.suppression_expires_at > now)


def suppressed_emails(db: Session, tenant_id: str, emails: Iterable[str]) -> Set[str]:
    """Lower-cased addresses from `emails` that have an active suppression."""
    wanted = {(e or "").strip().lower() for e in emails if e}
    if not wanted:
        return set()
    return {(e or "").strip().lower() for (e,) in db.query(GlobalUnsubscribe.email).filter(
        GlobalUnsubscribe.tenant_id == tenant_id, func.lower(GlobalUnsubscribe.email).in_(wanted), active_clause())}


def active_suppression(db: Session, tenant_id: str, email: str) -> Optional[GlobalUnsubscribe]:
    if not email:
        return None
    return db.query(GlobalUnsubscribe).filter(
        GlobalUnsubscribe.tenant_id == tenant_id,
        func.lower(GlobalUnsubscribe.email) == email.strip().lower(),
        active_clause(),
    ).first()


def blocked_reason(db: Session, prospect: Prospect) -> Optional[str]:
    """Why this contact must not be sent to right now, or None. Used at send time."""
    entry = active_suppression(db, prospect.tenant_id, prospect.email)
    if entry:
        return f"Suppressed: {entry.reason or 'unsubscribed'}"
    if prospect.consent_status == "UNSUBSCRIBED":
        return "Prospect unsubscribed"
    return None


def voluntary_expiry(now: datetime) -> Optional[datetime]:
    """When a voluntary unsubscribe stops blocking enrollment; None = never (setting 0)."""
    if settings.UNSUBSCRIBE_SUPPRESSION_HOURS > 0:
        return now + timedelta(hours=settings.UNSUBSCRIBE_SUPPRESSION_HOURS)
    if settings.UNSUBSCRIBE_SUPPRESSION_DAYS > 0:
        return now + timedelta(days=settings.UNSUBSCRIBE_SUPPRESSION_DAYS)
    return None


def suppress(
    db: Session,
    tenant_id: str,
    email: str,
    reason: str,
    kind: str = "UNSUBSCRIBE",
    source: Optional[str] = None,
) -> dict:
    """
    Record a suppression and apply it everywhere in the workspace, now.

    kind: UNSUBSCRIBE (voluntary, may expire per settings), COMPLAINT or
    HARD_BOUNCE (permanent). Every contact with this address is marked
    unsubscribed (complaints and unsubscribes) and every pending email to it,
    in any campaign, is cancelled. Caller commits.
    """
    email_l = (email or "").strip().lower()
    if not email_l or not tenant_id:
        return {"cancelled": 0, "contacts": 0}
    now = datetime.utcnow()
    expires = voluntary_expiry(now) if kind == "UNSUBSCRIBE" else None

    entry = db.query(GlobalUnsubscribe).filter(
        GlobalUnsubscribe.tenant_id == tenant_id, func.lower(GlobalUnsubscribe.email) == email_l).first()
    if entry:
        # Never shorten an existing block: a complaint or bounce stays permanent
        if entry.suppression_expires_at is not None and (expires is None or expires > entry.suppression_expires_at):
            entry.suppression_expires_at = expires
        entry.reason = reason[:255]
        entry.unsubscribed_at = now
    else:
        db.add(GlobalUnsubscribe(tenant_id=tenant_id, email=email_l, unsubscribed_at=now,
                                 suppression_expires_at=expires, reason=reason[:255]))

    contacts = db.query(Prospect).filter(Prospect.tenant_id == tenant_id, func.lower(Prospect.email) == email_l).all()
    ids = [p.prospect_id for p in contacts]
    if kind in ("UNSUBSCRIBE", "COMPLAINT"):
        for p in contacts:
            if p.consent_status != "UNSUBSCRIBED":
                p.consent_status = "UNSUBSCRIBED"
                p.consent_timestamp = now
                p.consent_source = source or kind.lower()

    cancelled = 0
    if ids:
        cancelled = db.query(EmailMessage).filter(
            EmailMessage.prospect_id.in_(ids), EmailMessage.direction == "OUTBOUND",
            EmailMessage.status.in_(PENDING),
        ).update({EmailMessage.status: "CANCELLED", EmailMessage.failure_reason: f"Suppressed: {reason}"[:1000]},
                 synchronize_session=False)
        cp_status = "BOUNCED" if kind == "HARD_BOUNCE" else "UNSUBSCRIBED"
        for cp in db.query(CampaignProspect).filter(CampaignProspect.prospect_id.in_(ids)):
            if cp.status in ("ACTIVE", "OPENED", "PAUSED", "RECONNECT_ELIGIBLE"):
                set_prospect_status(cp, cp_status, stopped_reason=reason[:100])
    logger.info("[Suppression] %s %s: %d contact(s), %d pending email(s) cancelled", kind, email_l, len(ids), cancelled)
    return {"cancelled": cancelled, "contacts": len(ids)}


def lift(db: Session, tenant_id: str, email: str) -> None:
    """Remove a suppression (contact re-subscribed by a user). Caller commits."""
    db.query(GlobalUnsubscribe).filter(
        GlobalUnsubscribe.tenant_id == tenant_id,
        func.lower(GlobalUnsubscribe.email) == (email or "").strip().lower(),
    ).delete(synchronize_session=False)
