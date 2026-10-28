"""
Delivery safety: auto-pause on risk (BR-DF-07) and a final status for every
sent email (BR-DF-04).

Auto-pause
    check_campaign_health() runs inside the SES webhook the moment a bounce or
    complaint arrives (and at send time for hard rejections), so a campaign
    crossing the limits is paused within seconds, well inside the one-minute
    target. check_domain_health() does the same for a tenant's campaigns sending
    from a domain, counting only that tenant's mail (sending domains are
    tenant-owned: another tenant on the same domain is neither counted nor
    paused). run_health_cycle() recomputes rolling 24h rates for every
    (tenant, domain) every hour and re-checks active campaigns, catching
    anything a dropped event missed.

Final status
    reconcile_delivery_status() runs every minute. Any email still waiting for
    SES after DELIVERY_CONFIRM_MINUTES gets UNCONFIRMED; a late delivery or
    bounce event still overrides it.

These are ``fn(db, ...)`` helpers used inside the caller's transaction (the SES webhook, the scheduler,
the jobs); the rules are pure (app/domain/deliverability.py) and the SQL is in
app/repositories/deliverability_safety.py.
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.clock import utcnow
from app.core.config import settings
from app.domain import deliverability as rules
from app.domain.deliverability import domain_of
from app.models.campaign import Campaign, CampaignStateEvent
from app.models.domain_reputation import ReputationAlert
from app.models.email_message import EmailMessage
from app.repositories.deliverability import borrow as domains_of
from app.repositories.deliverability_safety import COMPLAINT, HARD_BOUNCE, SafetyRepository, Session
from app.services import notifications, webhooks
from app.services.domain_tenancy import get_or_create_tenant_domain

logger = logging.getLogger(__name__)

__all__ = ["COMPLAINT", "FINAL_STATUSES", "HARD_BOUNCE", "auto_pause", "auto_pause_recipients",
           "campaigns_for_domain", "check_campaign_health", "check_domain_health", "notify_auto_pause",
           "on_risk_event", "reconcile_delivery_status", "risk_reason", "run_health_cycle", "set_final_status"]

FINAL_STATUSES = ("DELIVERED", "BOUNCED", "COMPLAINED", "REJECTED", "FAILED")


def _limits() -> rules.RiskLimits:
    return rules.RiskLimits(
        min_sends=settings.AUTO_PAUSE_MIN_SENDS, bounce_rate=settings.AUTO_PAUSE_BOUNCE_RATE,
        complaint_rate=settings.AUTO_PAUSE_COMPLAINT_RATE, min_bounces=settings.AUTO_PAUSE_MIN_BOUNCES,
        min_complaints=settings.AUTO_PAUSE_MIN_COMPLAINTS)


# ── Final status (BR-DF-04) ──────────────────────────────────

def set_final_status(msg: EmailMessage, status: str, when: Optional[datetime] = None) -> None:
    """Record the outcome. A bounce or complaint always wins over delivered/unconfirmed."""
    if msg.final_status in ("BOUNCED", "COMPLAINED") and status in ("DELIVERED", "UNCONFIRMED"):
        return
    msg.final_status = status
    msg.final_status_at = when or utcnow()


def reconcile_delivery_status(db: Session) -> int:
    n = SafetyRepository(db).reconcile_final_status(utcnow() - timedelta(minutes=settings.DELIVERY_CONFIRM_MINUTES))
    db.commit()
    return n


# ── Rates ────────────────────────────────────────────────────

def risk_reason(r: Dict[str, Any]) -> Optional[str]:
    """Why these numbers are unsafe, or None."""
    return rules.risk_reason(r, _limits())


def _campaign_rates(db: Session, campaign_id: str, sent_after: Optional[datetime] = None) -> Dict[str, Any]:
    return rules.rates(*SafetyRepository(db).campaign_outcomes(campaign_id, sent_after))


def _domain_rates(db: Session, domain: str, tenant_id: str, since: datetime) -> Dict[str, Any]:
    """24h rates for one tenant's mail from this domain (other tenants on the domain don't count)."""
    return rules.rates(*SafetyRepository(db).domain_outcomes(tenant_id, domain, since))


# ── Pausing ──────────────────────────────────────────────────

def auto_pause(db: Session, campaign: Campaign, reason: str) -> bool:
    """Pause an active campaign and freeze its queue, exactly like a manual pause."""
    if campaign.status != "ACTIVE":
        return False
    repo = SafetyRepository(db)
    campaign.status = "PAUSED"
    campaign.auto_paused = True
    campaign.paused_at = utcnow()
    campaign.paused_reason = f"Paused automatically: {reason}"[:2000]
    repo.freeze_queue(campaign.campaign_id)
    repo.add(CampaignStateEvent(id=str(uuid.uuid4()), campaign_id=campaign.campaign_id,
                                from_state="ACTIVE", to_state="PAUSED", reason=campaign.paused_reason))
    # Outbound webhooks (BR-SF-20)
    webhooks.campaign_state_changed(db, campaign.campaign_id, "ACTIVE", "PAUSED", campaign.paused_reason)
    logger.warning(f"[AutoPause] Campaign {campaign.campaign_id} ({campaign.campaign_name}) paused: {reason}")
    notify_auto_pause(db, campaign, reason)
    return True


def auto_pause_recipients(db: Session, campaign: Campaign) -> List[str]:
    """The campaign owner and the workspace's active admins."""
    admins = SafetyRepository(db).active_admin_ids(campaign.tenant_id)
    return [u for u in dict.fromkeys([campaign.created_by, *admins]) if u]


def notify_auto_pause(db: Session, campaign: Campaign, reason: str) -> int:
    """Tell the owner and admins that a campaign was paused automatically, and why (E2E-J2-09). Never raises."""
    try:
        name = campaign.campaign_name or "Campaign"
        return notifications.notify(db, campaign.tenant_id, auto_pause_recipients(db, campaign), "CAMPAIGN_AUTO_PAUSED",
                                    f"Campaign paused automatically: {name}",
                                    f"Sending was paused to protect deliverability: {reason}. "
                                    "Review the campaign before resuming it.",
                                    f"/app/campaigns/{campaign.campaign_id}")
    except Exception as exc:  # a notification problem never stops the pause
        logger.warning(f"[AutoPause] Could not notify about campaign {campaign.campaign_id}: {exc}")
        return 0


def check_campaign_health(db: Session, campaign_id: Optional[str], trigger: str = "") -> bool:
    """Pause this campaign now if its bounce/complaint numbers are unsafe. Commits."""
    if not campaign_id:
        return False
    campaign = SafetyRepository(db).get_campaign(campaign_id)
    if not campaign or campaign.status != "ACTIVE":
        return False
    reason = risk_reason(_campaign_rates(db, campaign_id, campaign.health_baseline_at or None))
    if not reason:
        return False
    paused = auto_pause(db, campaign, reason + (f" (after a {trigger})" if trigger else ""))
    db.commit()
    return paused


def campaigns_for_domain(db: Session, domain: str, tenant_id: str) -> List[Campaign]:
    """The tenant's active campaigns sending from its mailboxes on this domain."""
    return domains_of(db).active_campaigns_on_domain(tenant_id, domain)


def _judged_on_own_sends(db: Session, campaign: Campaign, now: datetime) -> bool:
    """A campaign a user resumed after an auto-pause (in the last 24 h) whose own sends since then are safe."""
    baseline = campaign.health_baseline_at
    if not baseline or baseline <= now - timedelta(hours=24):
        return False
    return not risk_reason(_campaign_rates(db, campaign.campaign_id, baseline))


def _pause_domain_campaigns(db: Session, domain: str, tenant_id: str, reason: str, trigger: str,
                            now: datetime) -> int:
    paused = 0
    for campaign in campaigns_for_domain(db, domain, tenant_id):
        if _judged_on_own_sends(db, campaign, now):
            continue
        paused += auto_pause(db, campaign, f"sending domain {domain}: {reason}"
                             + (f" (after a {trigger})" if trigger else ""))
    return paused


def check_domain_health(db: Session, domain: str, trigger: str = "", tenant_id: Optional[str] = None) -> int:
    """
    Recompute one tenant's 24h health on a sending domain and, if unsafe, pause that
    tenant's active campaigns sending from it. Returns how many campaigns were paused. Commits.
    """
    if not domain or not tenant_id:
        return 0
    domain = domain.lower()
    now = utcnow()
    r = _domain_rates(db, domain, tenant_id, now - timedelta(hours=24))
    row = get_or_create_tenant_domain(db, tenant_id, domain)
    row.sends_24h, row.bounce_rate_24h, row.complaint_rate_24h = r["sends"], r["bounce_rate"], r["complaint_rate"]
    row.health_checked_at = now
    # Score from current rates, so it recovers as rates fall (it used to only ever go down)
    row.current_reputation_score = rules.health_score(r, _limits())

    reason = risk_reason(r)
    paused = _pause_domain_campaigns(db, domain, tenant_id, reason, trigger, now) if reason else 0
    if paused:
        SafetyRepository(db).add(ReputationAlert(domain_id=row.domain_id, tenant_id=tenant_id, domain_name=domain,
                                                 alert_type="AUTO_PAUSE", severity="CRITICAL",
                                                 details=f"{paused} campaign(s) paused: {reason}"))
    db.commit()
    return paused


def on_risk_event(db: Session, campaign_id: Optional[str], sender_domain: Optional[str], trigger: str,
                  tenant_id: Optional[str] = None) -> None:
    """Called by the webhook for every bounce and complaint. tenant_id: the sending tenant."""
    try:
        check_campaign_health(db, campaign_id, trigger)
        if not sender_domain:
            return
        if not tenant_id and campaign_id:
            tenant_id = SafetyRepository(db).campaign_tenant(campaign_id)
        if tenant_id:
            check_domain_health(db, sender_domain, trigger, tenant_id=tenant_id)
        else:
            # Unattributable: the hourly cycle still covers every tenant on the domain
            logger.info(f"[AutoPause] {trigger} on {sender_domain} has no sending tenant; domain check skipped")
    except Exception as exc:  # never lose the webhook over the guard
        logger.exception(f"[AutoPause] Health check failed: {exc}")
        db.rollback()


def run_health_cycle(db: Session) -> Dict[str, int]:
    """Hourly: refresh every (tenant, sending domain) 24h health and re-check active campaigns."""
    pairs = domains_of(db).tenant_domain_pairs()
    for sender, tenant_id in SafetyRepository(db).recent_senders(utcnow() - timedelta(hours=24)):
        domain = domain_of(sender)
        if domain and tenant_id:
            pairs.add((tenant_id, domain))
    paused = sum(check_domain_health(db, d, "hourly health check", tenant_id=t) for t, d in sorted(pairs))
    for campaign_id in SafetyRepository(db).active_campaign_ids():
        paused += check_campaign_health(db, campaign_id, "hourly health check")
    return {"domains": len(pairs), "paused": paused}
