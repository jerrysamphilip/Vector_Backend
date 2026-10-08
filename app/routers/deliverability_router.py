
# app/routers/deliverability_router.py
"""
API Router for Deliverability and Reputation Monitoring.
"""

from typing import List
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.core.database import get_db
from app.core.auth import require_role
from app.models.user import User
from app.models.domain_reputation import SendingDomain, ReputationAlert
from app.models.provider_reputation import ExternalReputationMetric, ExternalFeedbackEvent
from app.models.sending_inbox import SendingInbox
from app.models.email_message import EmailMessage
from app.models.campaign import Campaign
from app.schemas.deliverability_schema import (
    SendingDomainResponse,
    ReputationAlertResponse,
    SESStatisticsResponse,
    ProviderIngestRequest,
    ExternalMetricResponse,
    ExternalFeedbackEventResponse,
    DashboardAlertResponse,
    AlertPreferenceOverviewResponse,
    AlertPreferenceBulkUpsertRequest,
    SentEmailLogEntry,
)
from app.services.deliverability_service import deliverability_service
from app.services.provider_ingestion_service import provider_ingestion_service
from app.services.alert_center_service import alert_center_service

# Kartik has changed this: Removed prefix to support explicit REST boundaries
router = APIRouter(tags=["Deliverability"])
logger = logging.getLogger(__name__)


def _tenant_domain_set(db: Session, current_user: User) -> set[str]:
    """Domains visible to the current tenant (derived from connected inboxes)."""
    rows = (
        db.query(SendingInbox.email_address)
        .filter(SendingInbox.tenant_id == current_user.tenant_id)
        .all()
    )
    domains = set()
    for (email,) in rows:
        if email and "@" in email:
            domains.add(email.split("@", 1)[1].lower())
    return domains

def _require_exclusive_domain(db: Session, current_user: User, domain_name: str) -> None:
    """
    sending_domains rows are global (no tenant_id). Only allow a tenant to mutate
    one when it has an inbox on that domain and no other tenant does; otherwise
    the change would affect another tenant. Platform admins may act on any domain.
    """
    if current_user.role == "PLATFORM_ADMIN":
        return
    domain = domain_name.lower()
    if domain not in _tenant_domain_set(db, current_user):
        raise HTTPException(status_code=404, detail="Domain not found")
    shared = (
        db.query(SendingInbox.inbox_id)
        .filter(
            SendingInbox.tenant_id != current_user.tenant_id,
            func.lower(SendingInbox.email_address).like(f"%@{domain}"),
        )
        .first()
    )
    if shared:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This domain is shared with another workspace and can only be changed by a platform admin",
        )

# Kartik has changed this: Explicit collection path
@router.get("/deliverability/statistics", response_model=SESStatisticsResponse)
def get_deliverability_statistics(
    domain: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Get sending statistics.
    If 'domain' is provided, returns LOCAL aggregated stats for that domain.
    If 'domain' is None, returns GLOBAL AWS SES account stats.
    """
    if domain:
        allowed_domains = _tenant_domain_set(db, current_user)
        if domain.lower() not in allowed_domains:
            raise HTTPException(status_code=404, detail="Domain not found")
        return deliverability_service.get_domain_statistics(db, domain)
    return deliverability_service.get_sending_statistics()

# Kartik has changed this: Explicit collection path
@router.get("/deliverability/domains", response_model=List[SendingDomainResponse])
def list_domains(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List all tracked sending domains with detailed associations."""
    allowed_domains = _tenant_domain_set(db, current_user)
    all_domains = deliverability_service.get_all_domains_enriched(db)
    return [d for d in all_domains if d.get("domain_name", "").lower() in allowed_domains]

# Kartik has changed this: Explicit collection path
@router.get("/deliverability/alerts", response_model=List[ReputationAlertResponse])
def list_alerts(
    resolved: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List reputation alerts (default: unresolved only)."""
    allowed_domains = _tenant_domain_set(db, current_user)
    if not allowed_domains:
        return []
    q = db.query(ReputationAlert)
    q = q.filter(ReputationAlert.domain_name.in_(allowed_domains))
    if not resolved:
        q = q.filter(ReputationAlert.is_resolved == False)
    return q.order_by(ReputationAlert.created_at.desc()).all()


@router.get("/deliverability/dashboard-alerts", response_model=List[DashboardAlertResponse])
def list_dashboard_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Unified dashboard alert feed (domain + inbox + campaign + sync)."""
    return alert_center_service.collect_dashboard_alerts(db, current_user.tenant_id, user_id=current_user.user_id)


@router.get("/deliverability/alert-preferences", response_model=AlertPreferenceOverviewResponse)
def get_alert_preferences(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """List current user's alert preferences (global + per-inbox)."""
    return alert_center_service.list_preferences(db, current_user.tenant_id, current_user.user_id)


@router.put("/deliverability/alert-preferences")
def upsert_alert_preferences(
    payload: AlertPreferenceBulkUpsertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Bulk upsert current user's alert preferences."""
    try:
        alert_center_service.upsert_preferences(
            db,
            current_user.tenant_id,
            current_user.user_id,
            [p.model_dump() for p in payload.preferences],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "updated", "updated": len(payload.preferences)}

# Kartik has changed this: Converted action scan to sub-resource creations
@router.post("/deliverability/domains/{domain_name}/scans", status_code=status.HTTP_202_ACCEPTED)
def scan_domain(
    domain_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Trigger a manual compliance scan (SPF/DKIM)."""
    allowed_domains = _tenant_domain_set(db, current_user)
    if domain_name.lower() not in allowed_domains:
        raise HTTPException(status_code=404, detail="Domain not found")
    # Calls async DNS lookup via service (Synchronous for MVP)
    result = deliverability_service.perform_dns_scan(domain_name, db)
    return {"status": "scan_completed", "results": result}

# Kartik has changed this: Explicit collection path
@router.post("/deliverability/snapshots", status_code=status.HTTP_201_CREATED)
def trigger_snapshot(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Manually trigger a daily health snapshot (for testing)."""
    deliverability_service.create_snapshot(db)
    return {"status": "snapshot_created"}

# Kartik has changed this: Explicit collection path
@router.delete("/deliverability/domains/{domain_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    domain_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Delete a domain and its stats from the system."""
    _require_exclusive_domain(db, current_user, domain_name)
    # Note: If inboxes still exist for this domain, it might be recreated by sync.
    domain = db.query(SendingDomain).filter(SendingDomain.domain_name == domain_name).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    
    db.delete(domain)
    db.commit()
    return None


@router.get("/deliverability/domains/{domain_name}/sent-log", response_model=List[SentEmailLogEntry])
def get_domain_sent_log(
    domain_name: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Recent outbound emails sent from this domain, with send timestamps.
    Surfaces EmailMessage.sent_at so users can confirm whether/when a send happened.
    """
    allowed_domains = _tenant_domain_set(db, current_user)
    if domain_name.lower() not in allowed_domains:
        raise HTTPException(status_code=404, detail="Domain not found")

    rows = (
        db.query(EmailMessage, Campaign.campaign_name)
        .outerjoin(Campaign, EmailMessage.campaign_id == Campaign.campaign_id)
        .filter(
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.from_email.ilike(f"%@{domain_name}"),
            # Only this tenant's messages, even when the domain is shared.
            or_(
                EmailMessage.inbox_id.in_(
                    db.query(SendingInbox.inbox_id).filter(
                        SendingInbox.tenant_id == current_user.tenant_id
                    )
                ),
                Campaign.tenant_id == current_user.tenant_id,
            ),
        )
        .order_by(EmailMessage.sent_at.desc(), EmailMessage.scheduled_at.desc())
        .limit(limit)
        .all()
    )

    return [
        SentEmailLogEntry(
            message_id=msg.message_id,
            subject=msg.subject,
            to_email=msg.to_email,
            from_email=msg.from_email,
            sent_at=msg.sent_at,
            scheduled_at=msg.scheduled_at,
            status=msg.status,
            campaign_name=campaign_name,
        )
        for msg, campaign_name in rows
    ]


@router.get("/deliverability/integrations/status")
def get_integration_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get read-only ingestion integration status and last run per provider."""
    return provider_ingestion_service.get_status(db)


@router.post("/deliverability/integrations/ingest", status_code=status.HTTP_202_ACCEPTED)
def ingest_provider_metrics(
    request: ProviderIngestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "PLATFORM_ADMIN")),
):
    """
    Read-only ingestion endpoint for Google Postmaster / SNDS / JMRP.
    Safe by design: stores metrics/events only, no campaign/send side effects.
    Metrics are stored per domain (not per tenant), so tenants may only ingest
    for a domain they own exclusively.
    """
    _require_exclusive_domain(db, current_user, request.domain_name or "")
    result = provider_ingestion_service.ingest(
        db=db,
        provider=request.provider,
        domain_name=request.domain_name,
        payload=request.payload,
        source=request.source,
        dry_run=request.dry_run,
    )
    if result.get("status") == "FAILED":
        raise HTTPException(status_code=400, detail=result.get("error", "Ingestion failed"))
    return result


@router.get("/deliverability/integrations/metrics", response_model=List[ExternalMetricResponse])
def list_external_metrics(
    provider: str,
    domain_name: str,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List ingested external reputation metrics for a provider/domain."""
    allowed_domains = _tenant_domain_set(db, current_user)
    if domain_name.lower() not in allowed_domains:
        raise HTTPException(status_code=404, detail="Domain not found")
    q = db.query(ExternalReputationMetric).filter(
        ExternalReputationMetric.provider == provider.upper(),
        ExternalReputationMetric.domain_name == domain_name
    ).order_by(ExternalReputationMetric.ingested_at.desc())
    return q.limit(limit).all()


@router.get("/deliverability/integrations/events", response_model=List[ExternalFeedbackEventResponse])
def list_external_events(
    provider: str,
    domain_name: str,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List ingested external feedback events (mainly JMRP-style complaint data)."""
    allowed_domains = _tenant_domain_set(db, current_user)
    if domain_name.lower() not in allowed_domains:
        raise HTTPException(status_code=404, detail="Domain not found")
    q = db.query(ExternalFeedbackEvent).filter(
        ExternalFeedbackEvent.provider == provider.upper(),
        ExternalFeedbackEvent.domain_name == domain_name
    ).order_by(ExternalFeedbackEvent.ingested_at.desc())
    return q.limit(limit).all()
