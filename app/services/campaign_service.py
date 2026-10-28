# app/services/campaign_service.py
"""
The campaign API's service (ADR 0009, chunk A2): one public method per route of
app/routers/campaign_router.py, each one unit of work (``with self.uow: ... commit``), the campaign
always looked up inside the caller's tenant first (404 for missing and cross-tenant alike).

``CampaignService`` keeps the composite it always was for other callers (CRUD from
CampaignCRUDService plus the launch / enrol / stub proxies to CampaignEmailService, which never
commit); the route methods are the ``*_for`` / verb methods below. Content, schedule, tracking and
template actions live in their own services (campaign_template_service, campaign_schedule_service,
campaign_tracking_service, campaign_query_service); editing after launch in campaign_edit_service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.clock import utcnow
from app.core.concurrency import resolve_expected
from app.core.errors import BadRequestError, NotFoundError
from app.domain.campaigns import DuplicateOverrides
from app.domain.campaigns_edit import inbox_daily_capacity, pacing_warning, window_minutes
from app.models.campaign import Campaign
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignDuplicateRequest,
    CampaignDuplicateResponse,
    CampaignEnrollmentRequest,
    CampaignFilter,
    CampaignListResponse,
    CampaignResponse,
    CampaignStateChange,
    CampaignStatus,
    CampaignUpdate,
    PersonaGap,
)
from app.services.campaign_analytics_service import CampaignAnalyticsService
from app.services.campaign_common import require_campaign
from app.services.campaign_crud_service import CampaignCRUDService
from app.services.campaign_duplicate_service import CampaignDuplicateService
from app.services.campaign_email_service import CampaignEmailService
from app.services.campaign_send_now_service import CampaignSendNowService
from app.services.daily_limit import enrollment_notice
from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
from app.services.send_guards import active_inboxes


def pacing_for(campaign: Campaign, planned_per_day: Dict[Any, int]) -> Optional[dict]:
    """Launch's pacing warning: a day plans more emails than the campaign's mailboxes can send in its window."""
    minutes = window_minutes(campaign.send_window_start, campaign.send_window_end)
    capacities = [inbox_daily_capacity(getattr(i, "current_daily_limit", None),
                                       getattr(i, "delay_between_emails", None), minutes)
                  for i in active_inboxes(campaign)]
    warning = pacing_warning(planned_per_day, capacities)
    return warning.as_dict() if warning else None


def state_change(campaign_id: str, old_status: str, campaign: Campaign, reason: Optional[str] = None,
                 pacing: Optional[dict] = None) -> CampaignStateChange:
    return CampaignStateChange(campaign_id=campaign_id, previous_status=CampaignStatus(old_status),
                               new_status=CampaignStatus(campaign.status), reason=reason, changed_at=utcnow(),
                               row_version=campaign.row_version, pacing_warning=pacing)


def enrolment_report(db: Any, user: Any, count: int, rejections: List[dict], prospect_ids: Any,
                     campaign_id: str) -> Dict[str, Any]:
    return {
        "enrolled_count": count,
        "rejected_count": len(rejections),
        "rejected_summary": summarize(rejections),
        "rejected": rejections[:MAX_REJECTIONS_RETURNED],
        "daily_limit_notice": enrollment_notice(db, user, count, prospect_ids, rejections, campaign_id),
    }


class CampaignService(CampaignCRUDService):
    """
    Composite: CRUD and state management (CampaignCRUDService) plus the launch / enrolment /
    stub-generation proxies to CampaignEmailService, and the route methods of the campaign API.
    """

    def __init__(self, db: Any = None, *, uow: Any = None, **kwargs: Any) -> None:
        super().__init__(db, uow=uow, **kwargs)
        self._email_svc = CampaignEmailService(db, uow=self.uow, audit=kwargs.get("audit"))

    # ── Proxy email-related methods (never commit) ────────────────────────────────────────────

    def launch(self, campaign_id: str, user_id: str) -> Any:
        return self._email_svc.launch(campaign_id, user_id)

    def generate_stub_content(self, campaign_id: str, user_id: str) -> int:
        return self._email_svc.generate_stub_content(campaign_id, user_id)

    def enroll_prospects(self, campaign_id: str, user_id: str, request: CampaignEnrollmentRequest) -> int:
        return self._email_svc.enroll_prospects(campaign_id, user_id, request)

    def enroll_prospects_with_report(self, campaign_id: str, user_id: str,
                                     request: CampaignEnrollmentRequest) -> Tuple[int, List[dict]]:
        return self._email_svc.enroll_prospects_with_report(campaign_id, user_id, request)

    def remove_prospect(self, campaign_id: str, prospect_id: str, user_id: str) -> bool:
        return self._email_svc.remove_prospect(campaign_id, prospect_id, user_id)

    def _substitute_placeholders(self, template, prospect, sender_name=None, cta_link=None, sender_title=None) -> str:
        return self._email_svc.substitute_placeholders(template, prospect, sender_name, cta_link, sender_title)

    def _preschedule_all_emails(self, campaign_id: str) -> int:
        return self._email_svc._preschedule_all_emails(campaign_id)

    # ── routes: campaigns ─────────────────────────────────────────────────────────────────────

    def campaign_for(self, user: Any, campaign_id: str) -> Campaign:
        """The caller's campaign (tenant-scoped); 404 otherwise."""
        return require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)

    def create_campaign(self, user: Any, data: CampaignCreate) -> Optional[CampaignResponse]:
        with self.uow as uow:
            campaign = self.create(tenant_id=user.tenant_id, user_id=user.user_id, data=data)
            uow.commit()
            return self.get_with_details(campaign.campaign_id)

    def list_for(self, user: Any, filters: CampaignFilter) -> CampaignListResponse:
        return self.list_campaigns(user.tenant_id, filters)

    def details_for(self, user: Any, campaign_id: str) -> CampaignResponse:
        campaign = self.campaign_for(user, campaign_id)
        return self._to_response(campaign)

    def update_campaign(self, user: Any, campaign_id: str, data: CampaignUpdate,
                        if_match: Optional[int]) -> Optional[CampaignResponse]:
        """Send the row_version you loaded (expected_version or If-Match, T-3); stale -> 409."""
        with self.uow as uow:
            campaign = self.campaign_for(user, campaign_id)
            uow.claim_version(campaign, resolve_expected(data.expected_version, if_match), "campaign")
            if not self.update(campaign_id, user.user_id, data):
                raise NotFoundError("Campaign not found")
            uow.commit()
            return self.get_with_details(campaign_id)

    def duplicate_campaign(self, user: Any, campaign_id: str, data: CampaignDuplicateRequest) -> CampaignDuplicateResponse:
        opts = DuplicateOverrides(
            campaign_name=data.campaign_name, start_date=data.start_date, end_date=data.end_date,
            send_window_start=data.send_window_start, send_window_end=data.send_window_end,
            campaign_timezone=data.campaign_timezone, inbox_ids=data.inbox_ids,
            list_ids=tuple(data.list_ids or ()), prospect_ids=tuple(data.prospect_ids or ()),
            skip_source_prospects=data.skip_source_prospects, copy_automation_rules=data.copy_automation_rules,
            copy_attachments=data.copy_attachments,
        )
        result = CampaignDuplicateService(self.uow.session).duplicate(user.tenant_id, user.user_id, campaign_id, opts)
        enrollment = None
        if result.enrollment is not None:
            count, rejections = result.enrollment["enrolled_count"], result.enrollment["rejections"]
            enrollment = {
                "enrolled_count": count, "rejected_count": len(rejections), "rejected_summary": summarize(rejections),
                "rejected": rejections[:MAX_REJECTIONS_RETURNED],
                "daily_limit_notice": enrollment_notice(self.uow.session, user, count, result.enrolled_prospect_ids,
                                                        [], result.campaign_id),
            }
        return CampaignDuplicateResponse(
            campaign=self._to_response(self.campaign_for(user, result.campaign_id)), source_campaign_id=result.source_campaign_id,
            copied=result.copied, skipped_inboxes=result.skipped_inboxes, enrollment=enrollment,
            persona_gaps=[PersonaGap(persona_type=g.persona_type, prospect_count=g.prospect_count,
                                     missing_steps=list(g.missing_steps)) for g in result.persona_gaps],
        )

    def delete_campaign(self, user: Any, campaign_id: str) -> None:
        with self.uow as uow:
            self.campaign_for(user, campaign_id)
            if not self.delete(campaign_id, user.user_id):
                raise NotFoundError("Campaign not found")
            uow.commit()

    # ── routes: contacts ──────────────────────────────────────────────────────────────────────

    def enrolled_for(self, user: Any, campaign_id: str, include_removed: bool) -> List[Any]:
        self.campaign_for(user, campaign_id)
        return self.get_enrolled_prospects(campaign_id, include_removed=include_removed)

    def enroll(self, user: Any, campaign_id: str, request: CampaignEnrollmentRequest) -> Dict[str, Any]:
        with self.uow as uow:
            self.campaign_for(user, campaign_id)
            try:
                count, rejections = self.enroll_prospects_with_report(campaign_id, user.user_id, request)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
            uow.commit()
            return enrolment_report(uow.session, user, count, rejections, request.prospect_ids, campaign_id)

    def remove_contact(self, user: Any, campaign_id: str, prospect_id: str) -> Dict[str, bool]:
        with self.uow as uow:
            self.campaign_for(user, campaign_id)
            if not self.remove_prospect(campaign_id, prospect_id, user.user_id):
                raise NotFoundError("Prospect not enrolled in this campaign")
            uow.commit()
            return {"removed": True}

    def stub_content(self, user: Any, campaign_id: str) -> Dict[str, int]:
        with self.uow as uow:
            self.campaign_for(user, campaign_id)
            try:
                count = self.generate_stub_content(campaign_id, user.user_id)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
            uow.commit()
            return {"generated_count": count}

    # ── routes: state ─────────────────────────────────────────────────────────────────────────

    def launch_campaign(self, user: Any, campaign_id: str, expected: Optional[int]) -> CampaignStateChange:
        """DRAFT -> ACTIVE; the answer carries ``pacing_warning`` when the plan outruns the mailboxes."""
        with self.uow as uow:
            current = self.campaign_for(user, campaign_id)
            old_status = current.status
            uow.claim_version(current, expected, "campaign")
            campaign = self.launch(campaign_id, user.user_id)
            pacing = pacing_for(campaign, uow.messages.planned_per_day(campaign_id))
            uow.commit()
            return state_change(campaign_id, old_status, campaign, pacing=pacing)

    def pause_campaign(self, user: Any, campaign_id: str, reason: Optional[str],
                       expected: Optional[int]) -> CampaignStateChange:
        with self.uow as uow:
            current = self.campaign_for(user, campaign_id)
            old_status = current.status
            uow.claim_version(current, expected, "campaign")
            campaign = self.pause(campaign_id, user.user_id, reason)
            uow.commit()
            return state_change(campaign_id, old_status, campaign, reason=reason)

    def resume_campaign(self, user: Any, campaign_id: str, expected: Optional[int]) -> CampaignStateChange:
        with self.uow as uow:
            current = self.campaign_for(user, campaign_id)
            old_status = current.status
            uow.claim_version(current, expected, "campaign")
            campaign = self.resume(campaign_id, user.user_id)
            uow.commit()
            return state_change(campaign_id, old_status, campaign)

    # ── routes: metrics, audit, send now ──────────────────────────────────────────────────────

    def metrics_for(self, user: Any, campaign_id: str) -> Dict[str, Any]:
        self.campaign_for(user, campaign_id)
        return self.metrics.get_metrics_dict(campaign_id)

    def audit_trail(self, user: Any, campaign_id: str, limit: int) -> List[Dict[str, Any]]:
        self.campaign_for(user, campaign_id)
        return [{"log_id": log.log_id, "action": log.action, "user_id": log.user_id, "created_at": log.created_at}
                for log in self.audit.get_campaign_history(campaign_id, limit)]

    async def send_now(self, user: Any, campaign_id: str, limit: int, cursor: Optional[str],
                       since: Optional[datetime]) -> Dict[str, Any]:
        campaign = self.campaign_for(user, campaign_id)
        return await CampaignSendNowService(self.uow.session).send_now(campaign, limit=limit, cursor=cursor,
                                                                       since=since)


__all__ = ["CampaignService", "CampaignCRUDService", "CampaignEmailService", "CampaignAnalyticsService",
           "pacing_for", "state_change"]
