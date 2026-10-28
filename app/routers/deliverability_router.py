# app/routers/deliverability_router.py
"""
API Router for Deliverability and Reputation Monitoring.

Layered (ADR 0009): each handler makes one call to ``SendingDomainService``
(app/services/deliverability_service.py), injected through the ``domain_service`` provider so tests
swap it with ``app.dependency_overrides``. Everything is limited to the caller's tenant.
"""

from typing import Any, List

from fastapi import APIRouter, Depends, Query, status

from app.core.auth import require_role
from app.core.database import get_db
from app.schemas.deliverability_schema import (
    AlertPreferenceBulkUpsertRequest,
    AlertPreferenceOverviewResponse,
    DashboardAlertResponse,
    DomainCreate,
    ExternalFeedbackEventResponse,
    ExternalMetricResponse,
    ProviderIngestRequest,
    ReputationAlertResponse,
    SendingDomainResponse,
    SentEmailLogEntry,
    SESStatisticsResponse,
)
from app.services.deliverability_service import SendingDomainService

# No router prefix: every route spells out its full path
router = APIRouter(tags=["Deliverability"])

tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")


def domain_service(db=Depends(get_db)) -> SendingDomainService:
    return SendingDomainService.for_session(db)


Domains = Depends(domain_service)


@router.get("/deliverability/statistics", response_model=SESStatisticsResponse)
def get_deliverability_statistics(
    domain: str = None,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """
    Sending statistics for the caller's tenant, from the local database.
    With 'domain': the tenant's mail from that domain. Without: all of the tenant's mail.
    (Account-wide AWS SES figures cover every tenant: GET /deliverability/statistics/account,
    platform admins only.)
    """
    return service.statistics(current_user, domain)


@router.get("/deliverability/statistics/account", response_model=SESStatisticsResponse)
def get_account_statistics(
    service: SendingDomainService = Domains,
    current_user: Any = Depends(require_role("PLATFORM_ADMIN")),
):
    """Account-wide AWS SES sending statistics (every tenant's mail): platform admins only."""
    return service.account_statistics()

@router.get("/deliverability/domains", response_model=List[SendingDomainResponse])
def list_domains(
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """List the tenant's sending domains with detailed associations."""
    return service.domains(current_user)

@router.get("/deliverability/alerts", response_model=List[ReputationAlertResponse])
def list_alerts(
    resolved: bool = False,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """List the tenant's reputation alerts (default: unresolved only)."""
    return service.reputation_alerts(current_user, resolved)


@router.get("/deliverability/dashboard-alerts", response_model=List[DashboardAlertResponse])
def list_dashboard_alerts(
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """Unified dashboard alert feed (domain + inbox + campaign + sync)."""
    return service.dashboard_alerts(current_user)


@router.get("/deliverability/alert-preferences", response_model=AlertPreferenceOverviewResponse)
def get_alert_preferences(
    service: SendingDomainService = Domains,
    current_user: Any = Depends(admin_user),
):
    """List current user's alert preferences (global + per-inbox)."""
    return service.alert_preferences(current_user)


@router.put("/deliverability/alert-preferences")
def upsert_alert_preferences(
    payload: AlertPreferenceBulkUpsertRequest,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(admin_user),
):
    """Bulk upsert current user's alert preferences."""
    return service.save_alert_preferences(current_user, [p.model_dump() for p in payload.preferences])

@router.post("/deliverability/domains/{domain_name}/scans", status_code=status.HTTP_202_ACCEPTED)
def scan_domain(
    domain_name: str,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(admin_user),
):
    """Trigger a manual compliance scan (SPF/DKIM) of the tenant's domain."""
    return service.scan(current_user, domain_name)

@router.post("/deliverability/snapshots", status_code=status.HTTP_201_CREATED)
def trigger_snapshot(
    service: SendingDomainService = Domains,
    current_user: Any = Depends(admin_user),
):
    """Manually trigger a health snapshot of the tenant's domains (for testing)."""
    return service.snapshot(current_user)

@router.delete("/deliverability/domains/{domain_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    domain_name: str,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(admin_user),
):
    """
    Delete the tenant's record of a domain and the tenant's stats for it.
    Other tenants' records for the same domain name are untouched. The SES domain
    identity in AWS is account-level and is never deleted here.
    """
    # Note: If inboxes still exist for this domain, it might be recreated by the health check.
    service.delete(current_user, domain_name)
    return None


@router.post("/deliverability/domains", status_code=status.HTTP_201_CREATED)
def add_domain(
    payload: DomainCreate,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(admin_user),
):
    """
    Add a sending domain to the workspace (admins). Validates the hostname, refuses a
    duplicate within the workspace (another workspace may add the same name), starts SES
    DKIM verification when SES is reachable, and returns the DNS records to publish.
    Nothing is verified here: "Run Diagnostics" (POST .../scans) checks SPF/DKIM/DMARC.
    """
    return service.add(current_user, payload.domain_name)


@router.get("/deliverability/domains/{domain_name}/dns-records")
def get_domain_dns_records(
    domain_name: str,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """DNS records to publish for this workspace domain (guidance only) and its last checked statuses."""
    return service.dns_records(current_user, domain_name)


@router.get("/deliverability/domains/{domain_name}/sent-log", response_model=List[SentEmailLogEntry])
def get_domain_sent_log(
    domain_name: str,
    limit: int = 20,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """
    Recent outbound emails sent from this domain, with send timestamps.
    Surfaces EmailMessage.sent_at so users can confirm whether/when a send happened.
    """
    return service.sent_log(current_user, domain_name, limit)


@router.get("/deliverability/integrations/status")
def get_integration_status(
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """Get read-only ingestion integration status and the tenant's last run per provider."""
    return service.integration_status(current_user)


@router.post("/deliverability/integrations/ingest", status_code=status.HTTP_202_ACCEPTED)
def ingest_provider_metrics(
    request: ProviderIngestRequest,
    service: SendingDomainService = Domains,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "PLATFORM_ADMIN")),
):
    """
    Read-only ingestion endpoint for Google Postmaster / SNDS / JMRP.
    Safe by design: stores metrics/events only, no campaign/send side effects.
    Data is stored for the caller's tenant (its own record of the domain). A platform
    admin ingests for every tenant that has a record of the domain.
    """
    return service.ingest(current_user, request.provider, request.domain_name, request.payload, request.source,
                          request.dry_run)


@router.get("/deliverability/integrations/metrics", response_model=List[ExternalMetricResponse])
def list_external_metrics(
    provider: str,
    domain_name: str,
    limit: int = Query(100, ge=1, le=1000),
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """List ingested external reputation metrics for a provider/domain."""
    return service.external_metrics(current_user, provider, domain_name, limit)


@router.get("/deliverability/integrations/events", response_model=List[ExternalFeedbackEventResponse])
def list_external_events(
    provider: str,
    domain_name: str,
    limit: int = Query(100, ge=1, le=1000),
    service: SendingDomainService = Domains,
    current_user: Any = Depends(tenant_user),
):
    """List ingested external feedback events (mainly JMRP-style complaint data)."""
    return service.external_events(current_user, provider, domain_name, limit)
