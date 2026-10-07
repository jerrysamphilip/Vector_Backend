"""
Delivery safety: auto-pause on risk (BR-DF-07) and a final status for every
sent email (BR-DF-04).

Auto-pause
    check_campaign_health() runs inside the SES webhook the moment a bounce or
    complaint arrives (and at send time for hard rejections), so a campaign
    crossing the limits is paused within seconds, well inside the one-minute
    target. check_domain_health() does the same for every campaign sending from
    a domain. run_health_cycle() recomputes rolling 24h rates for all domains
    every hour and re-checks active campaigns, catching anything a dropped
    event missed.

Final status
    reconcile_delivery_status() runs every minute. Any email still waiting for
    SES after DELIVERY_CONFIRM_MINUTES gets UNCONFIRMED; a late delivery or
    bounce event still overrides it.
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.campaign import Campaign, CampaignStateEvent
from app.models.domain_reputation import ReputationAlert, SendingDomain
from app.models.email_message import EmailMessage
from app.models.join_tables import campaign_inboxes
from app.models.sending_inbox import SendingInbox

logger = logging.getLogger(__name__)

HARD_BOUNCE = EmailMessage.last_error_code == "BOUNCE_Permanent"
COMPLAINT = EmailMessage.status == "COMPLAINED"
FINAL_STATUSES = ("DELIVERED", "BOUNCED", "COMPLAINED", "REJECTED", "FAILED")


# ── Final status (BR-DF-04) ──────────────────────────────────

def set_final_status(msg: EmailMessage, status: str, when: Optional[datetime] = None) -> None:
    """Record the outcome. A bounce or complaint always wins over delivered/unconfirmed."""
    if msg.final_status in ("BOUNCED", "COMPLAINED") and status in ("DELIVERED", "UNCONFIRMED"):
        return
    msg.final_status = status
    msg.final_status_at = when or datetime.utcnow()


def reconcile_delivery_status(db: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=settings.DELIVERY_CONFIRM_MINUTES)
    base = db.query(EmailMessage).filter(
        EmailMessage.final_status.is_(None), EmailMessage.direction == "OUTBOUND",
        EmailMessage.sent_at.isnot(None),
    )
    # Outcomes already on the row (event arrived before this column existed, or status-only writers)
    n = base.filter(EmailMessage.status.in_(("BOUNCED", "COMPLAINED", "REJECTED", "FAILED"))).update(
        {EmailMessage.final_status: EmailMessage.status, EmailMessage.final_status_at: func.utc_timestamp()},
        synchronize_session=False)
    n += base.filter(EmailMessage.delivered_at.isnot(None)).update(
        {EmailMessage.final_status: "DELIVERED", EmailMessage.final_status_at: EmailMessage.delivered_at},
        synchronize_session=False)
    n += base.filter(EmailMessage.sent_at < cutoff).update(
        {EmailMessage.final_status: "UNCONFIRMED", EmailMessage.final_status_at: func.utc_timestamp()},
        synchronize_session=False)
    db.commit()
    return n


# ── Rates ────────────────────────────────────────────────────

def _rates(db: Session, *criteria) -> dict:
    row = db.query(
        func.count(EmailMessage.message_id),
        func.coalesce(func.sum(case((HARD_BOUNCE, 1), else_=0)), 0),
        func.coalesce(func.sum(case((COMPLAINT, 1), else_=0)), 0),
    ).filter(EmailMessage.direction == "OUTBOUND", EmailMessage.sent_at.isnot(None), *criteria).one()
    sends, bounces, complaints = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    return {
        "sends": sends, "bounces": bounces, "complaints": complaints,
        "bounce_rate": bounces / sends if sends else 0.0,
        "complaint_rate": complaints / sends if sends else 0.0,
    }


def risk_reason(r: dict) -> Optional[str]:
    """Why these numbers are unsafe, or None."""
    if r["sends"] >= settings.AUTO_PAUSE_MIN_SENDS:
        if r["bounce_rate"] >= settings.AUTO_PAUSE_BOUNCE_RATE:
            return (f"hard-bounce rate {r['bounce_rate']:.1%} ({r['bounces']} of {r['sends']} sends) "
                    f"is at or above the {settings.AUTO_PAUSE_BOUNCE_RATE:.1%} limit")
        if r["complaint_rate"] >= settings.AUTO_PAUSE_COMPLAINT_RATE:
            return (f"spam-complaint rate {r['complaint_rate']:.2%} ({r['complaints']} of {r['sends']} sends) "
                    f"is at or above the {settings.AUTO_PAUSE_COMPLAINT_RATE:.2%} limit")
        return None
    if r["bounces"] >= settings.AUTO_PAUSE_MIN_BOUNCES:
        return f"{r['bounces']} hard bounces in the first {r['sends']} sends"
    if r["complaints"] >= settings.AUTO_PAUSE_MIN_COMPLAINTS:
        return f"{r['complaints']} spam complaints in the first {r['sends']} sends"
    return None


# ── Pausing ──────────────────────────────────────────────────

def auto_pause(db: Session, campaign: Campaign, reason: str) -> bool:
    """Pause an active campaign and freeze its queue, exactly like a manual pause."""
    if campaign.status != "ACTIVE":
        return False
    now = datetime.utcnow()
    campaign.status = "PAUSED"
    campaign.auto_paused = True
    campaign.paused_at = now
    campaign.paused_reason = f"Paused automatically: {reason}"[:2000]
    db.query(EmailMessage).filter(
        EmailMessage.campaign_id == campaign.campaign_id,
        EmailMessage.status.in_(["QUEUED", "SCHEDULED"]),
    ).update({"status": "PAUSED_BY_CAMPAIGN"}, synchronize_session=False)
    db.add(CampaignStateEvent(id=str(uuid.uuid4()), campaign_id=campaign.campaign_id,
                              from_state="ACTIVE", to_state="PAUSED", reason=campaign.paused_reason))
    logger.warning(f"[AutoPause] Campaign {campaign.campaign_id} ({campaign.campaign_name}) paused: {reason}")
    return True


def check_campaign_health(db: Session, campaign_id: Optional[str], trigger: str = "") -> bool:
    """Pause this campaign now if its bounce/complaint numbers are unsafe. Commits."""
    if not campaign_id:
        return False
    campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    if not campaign or campaign.status != "ACTIVE":
        return False
    criteria = [EmailMessage.campaign_id == campaign_id]
    if campaign.health_baseline_at:
        criteria.append(EmailMessage.sent_at > campaign.health_baseline_at)
    reason = risk_reason(_rates(db, *criteria))
    if not reason:
        return False
    paused = auto_pause(db, campaign, reason + (f" (after a {trigger})" if trigger else ""))
    db.commit()
    return paused


def campaigns_for_domain(db: Session, domain: str) -> List[Campaign]:
    return (db.query(Campaign)
            .join(campaign_inboxes, campaign_inboxes.c.campaign_id == Campaign.campaign_id)
            .join(SendingInbox, SendingInbox.inbox_id == campaign_inboxes.c.inbox_id)
            .filter(Campaign.status == "ACTIVE", SendingInbox.email_address.like(f"%@{domain}"))
            .distinct().all())


def _domain_rates(db: Session, domain: str, since: datetime) -> dict:
    return _rates(db, EmailMessage.sent_at >= since, EmailMessage.from_email.like(f"%@{domain}"))


def check_domain_health(db: Session, domain: str, trigger: str = "") -> int:
    """
    Recompute a sending domain's 24h health and, if unsafe, pause every active
    campaign sending from it. Returns how many campaigns were paused. Commits.
    """
    if not domain:
        return 0
    domain = domain.lower()
    now = datetime.utcnow()
    r = _domain_rates(db, domain, now - timedelta(hours=24))
    row = db.query(SendingDomain).filter(SendingDomain.domain_name == domain).first()
    if not row:
        row = SendingDomain(domain_name=domain)
        db.add(row)
    row.sends_24h, row.bounce_rate_24h, row.complaint_rate_24h = r["sends"], r["bounce_rate"], r["complaint_rate"]
    row.health_checked_at = now
    # Score from current rates, so it recovers as rates fall (it used to only ever go down)
    penalty = min(100.0, r["bounce_rate"] / max(settings.AUTO_PAUSE_BOUNCE_RATE, 1e-6) * 40
                  + r["complaint_rate"] / max(settings.AUTO_PAUSE_COMPLAINT_RATE, 1e-6) * 40)
    row.current_reputation_score = int(round(100 - penalty))

    reason = risk_reason(r)
    paused = 0
    if reason:
        for campaign in campaigns_for_domain(db, domain):
            # A campaign a user resumed after an auto-pause is judged on its own sends since then
            if campaign.health_baseline_at and campaign.health_baseline_at > now - timedelta(hours=24):
                own = risk_reason(_rates(db, EmailMessage.campaign_id == campaign.campaign_id,
                                         EmailMessage.sent_at > campaign.health_baseline_at))
                if not own:
                    continue
            paused += auto_pause(db, campaign, f"sending domain {domain}: {reason}"
                                 + (f" (after a {trigger})" if trigger else ""))
        if paused:
            db.add(ReputationAlert(domain_name=domain, alert_type="AUTO_PAUSE", severity="CRITICAL",
                                   details=f"{paused} campaign(s) paused: {reason}"))
    db.commit()
    return paused


def on_risk_event(db: Session, campaign_id: Optional[str], sender_domain: Optional[str], trigger: str) -> None:
    """Called by the webhook for every bounce and complaint."""
    try:
        check_campaign_health(db, campaign_id, trigger)
        if sender_domain:
            check_domain_health(db, sender_domain, trigger)
    except Exception as exc:  # never lose the webhook over the guard
        logger.exception(f"[AutoPause] Health check failed: {exc}")
        db.rollback()


def run_health_cycle(db: Session) -> dict:
    """Hourly: refresh every sending domain's 24h health and re-check active campaigns."""
    domains = {d for (d,) in db.query(SendingDomain.domain_name)}
    since = datetime.utcnow() - timedelta(hours=24)
    for (sender,) in db.query(EmailMessage.from_email).filter(
            EmailMessage.sent_at >= since, EmailMessage.from_email.isnot(None)).distinct():
        if "@" in sender:
            domains.add(sender.split("@", 1)[1].lower())
    paused = sum(check_domain_health(db, d, "hourly health check") for d in domains)
    for (campaign_id,) in db.query(Campaign.campaign_id).filter(Campaign.status == "ACTIVE").all():
        paused += check_campaign_health(db, campaign_id, "hourly health check")
    return {"domains": len(domains), "paused": paused}
