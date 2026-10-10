
# app/routers/deliverability_router.py
"""
API Router for Deliverability and Reputation Monitoring.
"""

from typing import List
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_role
from app.models.user import User
from app.models.domain_reputation import SendingDomain, ReputationAlert
from app.models.provider_reputation import ExternalReputationMetric, ExternalFeedbackEvent
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
from app.services.domain_tenancy import get_tenant_domain, tenant_has_domain, tenant_message_criterion

# Kartik has changed this: Removed prefix to support explicit REST boundaries
router = APIRouter(tags=["Deliverability"])
logger = logging.getLogger(__name__)


def _require_tenant_domain(db: Session, current_user: User, domain_name: str) -> str:
    """404 unless the caller's tenant uses this domain. Returns the normalised name."""
    domain = (domain_name or "").strip().lower()
    if not domain or not tenant_has_domain(db, current_user.tenant_id, domain):
        raise HTTPException(status_code=404, detail="Domain not found")
    return domain


# Kartik has changed this: Explicit collection path
@router.get("/deliverability/statistics", response_model=SESStatisticsResponse)
def get_deliverability_statistics(
    domain: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """
    Sending statistics for the caller's tenant, from the local database.
    With 'domain': the tenant's mail from that domain. Without: all of the tenant's mail.
    (Account-wide AWS SES figures cover every tenant: GET /deliverability/statistics/account,
    platform admins only.)
    """
    if domain:
        domain = _require_tenant_domain(db, current_user, domain)
    return deliverability_service.get_domain_statistics(db, domain=domain or None,
                                                        tenant_id=current_user.tenant_id)


@router.get("/deliverability/statistics/account", response_model=SESStatisticsResponse)
def get_account_statistics(
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
):
    """Account-wide AWS SES sending statistics (every tenant's mail): platform admins only."""
    return deliverability_service.get_sending_statistics()

# Kartik has changed this: Explicit collection path
@router.get("/deliverability/domains", response_model=List[SendingDomainResponse])
def list_domains(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List the tenant's sending domains with detailed associations."""
    return deliverability_service.get_all_domains_enriched(db, current_user.tenant_id)

# Kartik has changed this: Explicit collection path
@router.get("/deliverability/alerts", response_model=List[ReputationAlertResponse])
def list_alerts(
    resolved: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List the tenant's reputation alerts (default: unresolved only)."""
    q = db.query(ReputationAlert).filter(ReputationAlert.tenant_id == current_user.tenant_id)
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
    """Trigger a manual compliance scan (SPF/DKIM) of the tenant's domain."""
    domain_name = _require_tenant_domain(db, current_user, domain_name)
    # Calls async DNS lookup via service (Synchronous for MVP)
    result = deliverability_service.perform_dns_scan(domain_name, db, tenant_id=current_user.tenant_id)
    return {"status": "scan_completed", "results": result}

# Kartik has changed this: Explicit collection path
@router.post("/deliverability/snapshots", status_code=status.HTTP_201_CREATED)
def trigger_snapshot(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Manually trigger a health snapshot of the tenant's domains (for testing)."""
    deliverability_service.create_snapshot(db, tenant_id=current_user.tenant_id)
    return {"status": "snapshot_created"}

# Kartik has changed this: Explicit collection path
@router.delete("/deliverability/domains/{domain_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    domain_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """
    Delete the tenant's record of a domain and the tenant's stats for it.
    Other tenants' records for the same domain name are untouched. The SES domain
    identity in AWS is account-level and is never deleted here.
    """
    # Note: If inboxes still exist for this domain, it might be recreated by the health check.
    domain = get_tenant_domain(db, current_user.tenant_id, domain_name)
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    for model in (ExternalReputationMetric, ExternalFeedbackEvent):
        db.query(model).filter(model.tenant_id == current_user.tenant_id,
                               model.domain_name == domain.domain_name).delete(synchronize_session=False)
    db.delete(domain)  # snapshots and alerts go with it
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
    domain_name = _require_tenant_domain(db, current_user, domain_name)
    limit = max(1, min(limit, 500))

    rows = (
        db.query(EmailMessage, Campaign.campaign_name)
        .outerjoin(Campaign, EmailMessage.campaign_id == Campaign.campaign_id)
        .filter(
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.from_email.ilike(f"%@{domain_name}"),
            # Only this tenant's messages, even when another tenant uses the same domain.
            tenant_message_criterion(current_user.tenant_id),
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
    """Get read-only ingestion integration status and the tenant's last run per provider."""
    return provider_ingestion_service.get_status(db, tenant_id=current_user.tenant_id)


@router.post("/deliverability/integrations/ingest", status_code=status.HTTP_202_ACCEPTED)
def ingest_provider_metrics(
    request: ProviderIngestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "PLATFORM_ADMIN")),
):
    """
    Read-only ingestion endpoint for Google Postmaster / SNDS / JMRP.
    Safe by design: stores metrics/events only, no campaign/send side effects.
    Data is stored for the caller's tenant (its own record of the domain). A platform
    admin ingests for every tenant that has a record of the domain.
    """
    if current_user.role == "PLATFORM_ADMIN":
        domain_name = (request.domain_name or "").strip().lower()
        tenant_ids = [t for (t,) in db.query(SendingDomain.tenant_id).filter(
            SendingDomain.domain_name == domain_name, SendingDomain.tenant_id.isnot(None)).distinct()]
        if not tenant_ids:
            raise HTTPException(status_code=404, detail="Domain not found")
    else:
        domain_name = _require_tenant_domain(db, current_user, request.domain_name or "")
        tenant_ids = [current_user.tenant_id]

    result = None
    for tenant_id in tenant_ids:
        result = provider_ingestion_service.ingest(
            db=db,
            provider=request.provider,
            domain_name=domain_name,
            payload=request.payload,
            source=request.source,
            dry_run=request.dry_run,
            tenant_id=tenant_id,
        )
        if result.get("status") == "FAILED":
            raise HTTPException(status_code=400, detail=result.get("error", "Ingestion failed"))
    if len(tenant_ids) > 1:
        result = {**result, "tenants": len(tenant_ids)}
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
    domain_name = _require_tenant_domain(db, current_user, domain_name)
    q = db.query(ExternalReputationMetric).filter(
        ExternalReputationMetric.tenant_id == current_user.tenant_id,
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
    domain_name = _require_tenant_domain(db, current_user, domain_name)
    q = db.query(ExternalFeedbackEvent).filter(
        ExternalFeedbackEvent.tenant_id == current_user.tenant_id,
        ExternalFeedbackEvent.provider == provider.upper(),
        ExternalFeedbackEvent.domain_name == domain_name
    ).order_by(ExternalFeedbackEvent.ingested_at.desc())
    return q.limit(limit).all()
