"""
Shared plumbing of the campaign services (ADR 0009, chunk A2): the service base that owns a
``CampaignsUnitOfWork``, the tenant-scoped campaign lookups, mailbox validation and state events.

A campaign service is built either from the request session (``Service(db)`` / ``Service.for_session(db)``,
the production path) or from a unit of work (``Service(uow=fake)``, unit tests). Route-facing methods open
``with self.uow:`` and commit; helpers other aggregates call with their own session (e.g.
``CampaignEmailService(db).enroll_prospects_with_report``) never commit: the caller's transaction does.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from app.core.errors import BadRequestError, NotFoundError
from app.models.campaign import Campaign
from app.repositories.campaigns import CampaignsRepository, CampaignsUnitOfWork
from app.services import webhooks
from app.services.audit_service import AuditService

CAMPAIGN_NOT_FOUND = "Campaign not found"


class CampaignServiceBase:
    def __init__(self, db: Any = None, *, uow: Any = None, audit: Any = None) -> None:
        if uow is None:
            if db is None:
                raise ValueError(f"{type(self).__name__} needs a session or a unit of work")
            uow = CampaignsUnitOfWork.for_session(db)
        self.uow = uow
        self.db = db
        self._audit = audit

    @classmethod
    def for_session(cls, db: Any) -> Any:
        return cls(db)

    @property
    def audit(self) -> AuditService:
        if self._audit is None:
            self._audit = AuditService(self.uow.session)
        return self._audit

    def log(self, campaign: Campaign, user_id: str, action: str, entity_type: str = AuditService.ENTITY_CAMPAIGN,
            entity_id: Optional[str] = None) -> None:
        self.audit.log_action(tenant_id=campaign.tenant_id, user_id=user_id, action=action,
                              entity_type=entity_type, entity_id=entity_id or campaign.campaign_id)


def require_campaign(repo: CampaignsRepository, tenant_id: str, campaign_id: str, lock: bool = False) -> Campaign:
    """The tenant's campaign (row-locked when ``lock``); 404 for missing and cross-tenant alike."""
    campaign = repo.lock(tenant_id, campaign_id) if lock else repo.get(tenant_id, campaign_id)
    if campaign is None:
        raise NotFoundError(CAMPAIGN_NOT_FOUND)
    return campaign


def validate_inboxes(repo: CampaignsRepository, tenant_id: str, inbox_ids: Optional[Sequence[str]]) -> list[Any]:
    """
    The selected mailboxes, checked for campaign use: in this tenant, ACTIVE, and not on a domain the
    tenant's sending_domains mark as blacklisted. ValueError with the reason otherwise.
    """
    unique_ids = list(dict.fromkeys(inbox_ids or []))
    if not unique_ids:
        return []
    inboxes = repo.inboxes(tenant_id, unique_ids)
    if len(inboxes) != len(unique_ids):
        found = {i.inbox_id for i in inboxes}
        missing = [i for i in unique_ids if i not in found]
        raise ValueError(f"Invalid inbox selection. Not found in tenant: {', '.join(missing)}")
    inactive = [i.email_address for i in inboxes if (i.status or "").upper() != "ACTIVE"]
    if inactive:
        raise ValueError(f"Selected inboxes are not ACTIVE: {', '.join(inactive)}")
    domains = {i.email_address.split("@", 1)[1].lower() for i in inboxes if i.email_address and "@" in i.email_address}
    blocked = repo.blacklisted_domains(tenant_id, domains)
    if blocked:
        raise ValueError(f"Selected inbox domains are blocked for sending: {', '.join(sorted(set(blocked)))}")
    return inboxes


def validate_inboxes_or_400(repo: CampaignsRepository, tenant_id: str, inbox_ids: Optional[Sequence[str]]) -> list[Any]:
    try:
        return validate_inboxes(repo, tenant_id, inbox_ids)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def record_state_change(uow: Any, campaign_id: str, from_state: Optional[str], to_state: str,
                        reason: Optional[str] = None, notify: bool = True) -> None:
    """A campaign state event, plus the campaign.* outbound webhook (BR-SF-20)."""
    uow.campaigns.add_state_event(campaign_id, from_state, to_state, reason)
    if notify:
        webhooks.campaign_state_changed(uow.session, campaign_id, from_state, to_state, reason)
