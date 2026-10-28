# app/services/campaign_crud_service.py
"""
Campaign CRUD service: create, read, update, delete, state transitions, sequence configuration and
the enrolled-contact list. SQL lives in app/repositories/campaigns*.py; none of these methods commits
(app/services/campaign_service.py's route methods own the unit of work).
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import Any, Callable, Dict, List, Optional

from app.core.clock import utcnow
from app.domain.campaigns import CampaignListFilter, StepMessage, is_removed, lead_timing, total_pages
from app.domain.paging import Paging
from app.models.campaign import Campaign
from app.models.email_sequence import EmailSequence
from app.models.sending_inbox import SendingInbox
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignFilter,
    CampaignListItem,
    CampaignListResponse,
    CampaignMetrics,
    CampaignResponse,
    CampaignStatus,
    CampaignUpdate,
)
from app.schemas.sequence_schema import SequenceStepCreate
from app.services.audit_service import AuditService
from app.services.campaign_analytics_service import CampaignAnalyticsService
from app.services.campaign_common import CampaignServiceBase, record_state_change, validate_inboxes
from app.services.email_scheduler_service import rebase_overdue_on_resume
from app.services.inbox_rotation import InboxRotation
from app.services.metrics_service import MetricsService
from app.services.send_guards import require_active_inbox
from app.services.sequence_stop import stops_sequence
from app.utils.email_utils import normalize_cta_link

# Unsent emails a mailbox change re-binds (FAILED too: a retry goes from the new mailbox)
REBIND_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN", "FAILED")
EMPTY_METRICS = {
    "sent_count": 0, "opened_count": 0, "replied_count": 0, "bounced_count": 0, "sender_bounced_count": 0,
    "positive_replied_count": 0, "ooo_count": 0, "unsubscribed_count": 0, "in_progress_count": 0,
}


def _step_stopper(status: Optional[str]) -> Callable[[StepMessage], bool]:
    """Whether a contact with this enrolment status must not get a given step (sequence_stop.py)."""
    def stopped(message: StepMessage) -> bool:
        return stops_sequence(message, status)
    return stopped


def inbox_view(inbox: Any) -> Dict[str, Any]:
    """A campaign's sending inbox as the campaign page shows it."""
    return {
        "inbox_id": inbox.inbox_id,
        "email_address": inbox.email_address,
        "provider": getattr(inbox, "provider", "SMTP"),
        "status": inbox.status,
        "daily_limit": getattr(inbox, "daily_limit", 50),
        "last_sync_at": inbox.last_sync_at.isoformat() if getattr(inbox, "last_sync_at", None) else None,
        "imap_host": getattr(inbox, "imap_host", None),
        "imap_port": getattr(inbox, "imap_port", 993),
        "imap_username": getattr(inbox, "imap_username", None),
    }


def rebind_plan(pending: List[Any], sent_newest_first: List[Any], inboxes: List[Any]) -> Dict[str, Any]:
    """
    {message_id: mailbox (None = no sender)} for unsent emails after the campaign's mailboxes changed. A
    contact keeps the mailbox an email of theirs already went from when it is still selected (sticky
    sender); everyone else is shared out by the weighted rotation (S-18, inbox_rotation.py).
    """
    active = sorted([i for i in inboxes if (i.status or "").upper() == "ACTIVE"],
                    key=lambda i: (i.email_address or "").lower())
    if not active:
        # No sender until the campaign has an active inbox again (never the platform address)
        return {m.message_id: None for m in pending}
    by_id = {i.inbox_id: i for i in active}
    by_email = {(i.email_address or "").strip().lower(): i for i in active if i.email_address}
    sticky: Dict[str, Any] = {}
    for sent in sent_newest_first:
        if sent.prospect_id in sticky:
            continue
        chosen = by_id.get(sent.inbox_id) if sent.inbox_id else None
        if not chosen and sent.from_email:
            chosen = by_email.get(sent.from_email.strip().lower())
        if chosen:
            sticky[sent.prospect_id] = chosen
    rotation = InboxRotation(active)
    plan: Dict[str, Any] = {}
    for message in pending:
        if message.prospect_id not in sticky:
            sticky[message.prospect_id] = rotation.take()
        plan[message.message_id] = sticky[message.prospect_id]
    return plan


class CampaignCRUDService(CampaignServiceBase):
    """
    Core campaign CRUD operations and simple state transitions (pause, resume, complete). Launch is
    delegated to CampaignEmailService.
    """

    def __init__(self, db: Any = None, *, uow: Any = None, audit: Any = None, metrics: Any = None,
                 analytics: Any = None) -> None:
        super().__init__(db, uow=uow, audit=audit)
        self._metrics = metrics
        self._analytics = analytics

    @property
    def metrics(self) -> MetricsService:
        if self._metrics is None:
            self._metrics = MetricsService(self.uow.session)
        return self._metrics

    @property
    def analytics(self) -> CampaignAnalyticsService:
        if self._analytics is None:
            self._analytics = CampaignAnalyticsService(self.uow.session)
        return self._analytics

    # =============================
    # CRUD OPERATIONS
    # =============================

    def create(self, tenant_id: str, user_id: str, data: CampaignCreate) -> Campaign:
        """Create a DRAFT campaign with its mailboxes and metrics row."""
        campaign = Campaign(
            campaign_id=str(uuid.uuid4()), tenant_id=tenant_id, campaign_name=data.campaign_name,
            sender_name=data.sender_name, sender_title=data.sender_title, cta_link=normalize_cta_link(data.cta_link),
            status=CampaignStatus.DRAFT.value, created_by=user_id,
            send_window_start=data.send_window_start or time(9, 0), send_window_end=data.send_window_end or time(17, 0),
            respect_timezone=data.respect_timezone, campaign_timezone=data.campaign_timezone,
            sending_mode=data.sending_mode, min_gap_minutes=data.min_gap_minutes, batch_size=data.batch_size,
            batch_gap_minutes=data.batch_gap_minutes, daily_batch_size=data.daily_batch_size,
            start_date=data.start_date, end_date=data.end_date,
            wait_business_days=bool(getattr(data, "wait_business_days", False)),
        )
        repo = self.uow.campaigns
        repo.add(tenant_id, campaign)
        if data.inbox_ids:
            campaign.inboxes = self._validate_and_load_inboxes(tenant_id, data.inbox_ids)
        repo.flush()
        self.metrics.get_or_create_metrics(campaign.campaign_id)
        self.log(campaign, user_id, AuditService.ACTION_CREATE)
        return campaign

    def get(self, campaign_id: str) -> Optional[Campaign]:
        """A campaign by id (the caller scoped it to the tenant)."""
        return self.uow.campaigns.by_id(campaign_id)

    def get_with_details(self, campaign_id: str) -> Optional[CampaignResponse]:
        campaign = self.get(campaign_id)
        return self._to_response(campaign) if campaign else None

    def update(self, campaign_id: str, user_id: str, data: CampaignUpdate) -> Optional[Campaign]:
        """Apply the fields sent; an explicit null clears the clearable ones (S-23, CampaignUpdate.changes)."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        for name, value in data.changes().items():
            if name == "cta_link":
                value = normalize_cta_link(value) if value else None
            setattr(campaign, name, value)
        if campaign.start_date and campaign.end_date and campaign.end_date < campaign.start_date:
            raise ValueError("end_date must be on or after start_date")
        if data.inbox_ids is not None:
            campaign.inboxes = self._validate_and_load_inboxes(campaign.tenant_id, data.inbox_ids)
            self._rebind_pending_messages_for_campaign(campaign)
        self.uow.campaigns.flush()
        self.log(campaign, user_id, AuditService.ACTION_UPDATE)
        return campaign

    def delete(self, campaign_id: str, user_id: str) -> bool:
        """Delete a campaign and all related data (hard delete with cascade)."""
        campaign = self.get(campaign_id)
        if not campaign:
            return False
        self.log(campaign, user_id, AuditService.ACTION_DELETE)
        self.uow.campaigns.delete_cascade(campaign)
        return True

    # =============================
    # CONFIGURATION
    # =============================

    def add_sequence_step(self, campaign_id: str, data: SequenceStepCreate) -> EmailSequence:
        """Add a step to the campaign sequence."""
        if not self.get(campaign_id):
            raise ValueError("Campaign not found")
        content = self.uow.content
        if content.step_count(campaign_id) >= 7:
            raise ValueError("Maximum 7 steps allowed per campaign")
        if content.step(campaign_id, data.step_number):
            raise ValueError(f"Step {data.step_number} already exists")
        step = EmailSequence(
            sequence_id=str(uuid.uuid4()), campaign_id=campaign_id, step_number=data.step_number,
            wait_days=data.wait_days, stop_on_reply=data.stop_on_reply, stop_on_bounce=data.stop_on_bounce,
            send_start_hour=data.send_start_hour, send_end_hour=data.send_end_hour,
        )
        content.add(step)
        content.flush()
        return step

    # =============================
    # STATE MANAGEMENT
    # =============================

    def pause(self, campaign_id: str, user_id: str, reason: Optional[str] = None) -> Optional[Campaign]:
        """Pause an active campaign and freeze its queued emails."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        if campaign.status != CampaignStatus.ACTIVE.value:
            raise ValueError(f"Cannot pause campaign with status: {campaign.status}")
        old_status = campaign.status
        campaign.status = CampaignStatus.PAUSED.value
        campaign.paused_reason = reason
        campaign.paused_at = utcnow()
        campaign.auto_paused = False
        # Freeze all queued emails so the scheduler doesn't pick them up
        self.uow.messages.set_status(campaign_id, ["QUEUED", "SCHEDULED"], "PAUSED_BY_CAMPAIGN")
        record_state_change(self.uow, campaign_id, old_status, campaign.status, reason)
        self.log(campaign, user_id, AuditService.ACTION_PAUSE)
        self.uow.campaigns.flush()
        return campaign

    def resume(self, campaign_id: str, user_id: str) -> Optional[Campaign]:
        """Resume a paused campaign and restore its frozen emails."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        if campaign.status != CampaignStatus.PAUSED.value:
            raise ValueError(f"Cannot resume campaign with status: {campaign.status}")
        # Campaign mail only goes from the campaign's own mailboxes, never the platform address
        require_active_inbox(campaign, "resume")
        old_status = campaign.status
        campaign.status = CampaignStatus.ACTIVE.value
        if campaign.auto_paused:
            # The user has reviewed the auto-pause: judge health on sends from now on (BR-DF-07)
            campaign.health_baseline_at = utcnow()
        campaign.auto_paused = False
        campaign.paused_reason = None
        campaign.paused_at = None
        # Re-spread whatever fell due during the pause instead of releasing it all at once, keeping each
        # contact's step spacing (B02)
        rebase_overdue_on_resume(self.uow.session, campaign)
        self.uow.messages.set_status(campaign_id, ["PAUSED_BY_CAMPAIGN"], "QUEUED")
        record_state_change(self.uow, campaign_id, old_status, campaign.status)
        self.log(campaign, user_id, AuditService.ACTION_RESUME)
        self.uow.campaigns.flush()
        return campaign

    def complete(self, campaign_id: str) -> Optional[Campaign]:
        """Mark a campaign as completed."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        old_status = campaign.status
        campaign.status = CampaignStatus.COMPLETED.value
        record_state_change(self.uow, campaign_id, old_status, campaign.status)
        self.uow.campaigns.flush()
        return campaign

    # =============================
    # FILTERING & SEARCH
    # =============================

    def list_campaigns(self, tenant_id: str, filters: CampaignFilter) -> CampaignListResponse:
        """The tenant's campaigns, newest first, with bulk-loaded metrics, contact counts and creators."""
        flt = CampaignListFilter(status=filters.status.value if filters.status else None,
                                 created_by=filters.created_by, campaign_name=filters.campaign_name)
        page = self.uow.campaigns.page(tenant_id, flt, Paging(filters.page, filters.page_size))
        campaigns = list(page.items)
        ids = [c.campaign_id for c in campaigns]
        metrics_map = self.analytics.get_bulk_metrics(ids)
        # Contacts the user removed are not counted (S-21)
        prospect_counts = self.uow.campaigns.prospect_counts(ids)
        creators = self.uow.campaigns.creator_names(c.created_by for c in campaigns)
        items = [self._list_item(c, metrics_map.get(c.campaign_id, EMPTY_METRICS), creators.get(c.created_by),
                                 prospect_counts.get(c.campaign_id, 0)) for c in campaigns]
        return CampaignListResponse(items=items, total=page.total, page=filters.page, page_size=filters.page_size,
                                    total_pages=total_pages(page.total, filters.page_size))

    @staticmethod
    def _list_item(c: Campaign, metrics: Dict[str, int], creator: Optional[str], prospects: int) -> CampaignListItem:
        return CampaignListItem(
            campaign_id=c.campaign_id, campaign_name=c.campaign_name, status=CampaignStatus(c.status),
            row_version=c.row_version or 1, created_by=c.created_by, creator_name=creator,
            sent_count=metrics["sent_count"], opened_count=metrics["opened_count"],
            replied_count=metrics["replied_count"], bounced_count=metrics["bounced_count"],
            sender_bounced_count=metrics["sender_bounced_count"],
            positive_replied_count=metrics["positive_replied_count"], ooo_count=metrics["ooo_count"],
            unsubscribed_count=metrics["unsubscribed_count"], in_progress_count=metrics["in_progress_count"],
            prospect_count=prospects, created_at=c.created_at, updated_at=c.updated_at,
        )

    # =============================
    # PROSPECTS
    # =============================

    def get_enrolled_prospects(self, campaign_id: str, include_removed: bool = False) -> List:
        """
        Enrolled prospects of a campaign with their details and lead-list timing columns (F-12):
        ``prospect.timing`` (app/domain/campaigns.LeadTiming) and ``prospect.removed``. Contacts the user
        removed are left out unless ``include_removed`` (S-21).
        """
        repo = self.uow.campaigns
        enrolled = [(cp, p) for cp, p in repo.enrolments_with_contacts(campaign_id)
                    if include_removed or not is_removed(cp.status, cp.stopped_reason)]
        prospect_ids = [p.prospect_id for _, p in enrolled]
        # Sticky sender: the mailbox (else snapshot From) a sent email of theirs went from
        inbox_map = repo.sent_from(campaign_id, prospect_ids)
        notes_map = repo.latest_list_notes(prospect_ids)
        timing = self._lead_timing(campaign_id, enrolled)
        results = []
        for cp, prospect in enrolled:
            prospect.status = cp.status  # campaign-specific status
            prospect.timing = timing.get(prospect.prospect_id)
            prospect.removed = is_removed(cp.status, cp.stopped_reason)
            prospect.assigned_inbox = inbox_map.get(prospect.prospect_id)
            note = notes_map.get(prospect.prospect_id, {})
            prospect.notes = note.get("notes")
            prospect.notes_list_id = note.get("list_id")
            results.append(prospect)
        return results

    def _lead_timing(self, campaign_id: str, enrolled: List) -> Dict:
        """{prospect_id: LeadTiming} for the enrolments shown (one query per fact, not per contact)."""
        campaign = self.get(campaign_id)
        if campaign is None or not enrolled:
            return {}
        repo = self.uow.campaigns
        ids = [cp.prospect_id for cp, _ in enrolled]
        messages = repo.step_messages(campaign.tenant_id, campaign_id, ids)
        ooo = repo.ooo_prospects(campaign.tenant_id, campaign_id, ids)
        steps_total = repo.step_count(campaign.tenant_id, campaign_id)
        sending = campaign.status == CampaignStatus.ACTIVE.value
        return {
            cp.prospect_id: lead_timing(
                messages.get(cp.prospect_id, []), steps_total=steps_total, stopped_reason=cp.stopped_reason,
                ooo=cp.prospect_id in ooo, sending=sending, step_stopped=_step_stopper(cp.status))
            for cp, _ in enrolled
        }

    # =============================
    # HELPER METHODS
    # =============================

    def validate_and_load_inboxes(self, tenant_id: str, inbox_ids: List[str]) -> List[SendingInbox]:
        """Public name of _validate_and_load_inboxes (used by campaign duplication)."""
        return self._validate_and_load_inboxes(tenant_id, inbox_ids)

    def _validate_and_load_inboxes(self, tenant_id: str, inbox_ids: List[str]) -> List[SendingInbox]:
        """The selected mailboxes: in the tenant, ACTIVE, domain not blacklisted (ValueError otherwise)."""
        return validate_inboxes(self.uow.campaigns, tenant_id, inbox_ids)

    def _rebind_pending_messages_for_campaign(self, campaign: Campaign) -> int:
        """
        Rebind unsent campaign emails to the currently selected mailboxes (weighted rotation, sticky
        sender). Returns how many emails changed mailbox.
        """
        messages = self.uow.messages
        pending = messages.unsent(campaign.campaign_id, REBIND_STATUSES)
        if not pending:
            return 0
        plan = rebind_plan(pending, messages.sent_newest_first(campaign.campaign_id), list(campaign.inboxes or []))
        changed = 0
        for message in pending:
            inbox = plan[message.message_id]
            inbox_id, from_email = (inbox.inbox_id, inbox.email_address) if inbox else (None, None)
            if (message.inbox_id, message.from_email) != (inbox_id, from_email):
                changed += 1
            message.inbox_id, message.from_email = inbox_id, from_email
        messages.flush()
        return changed

    def _get_creator_name(self, user_id: str) -> Optional[str]:
        return self.uow.campaigns.creator_full_name(user_id)

    def _get_sequence_count(self, campaign_id: str) -> int:
        return self.uow.content.step_count(campaign_id)

    def _get_prospect_count(self, campaign_id: str) -> int:
        """Number of enrolled prospects, not counting the ones the user removed (S-21)."""
        return self.uow.campaigns.prospect_counts([campaign_id]).get(campaign_id, 0)

    def _to_response(self, campaign: Campaign) -> CampaignResponse:
        """Convert Campaign to full response."""
        metrics = self.analytics.get_dynamic_metrics(campaign.campaign_id)
        return CampaignResponse(
            campaign_id=campaign.campaign_id, tenant_id=campaign.tenant_id, campaign_name=campaign.campaign_name,
            sender_name=campaign.sender_name, sender_title=campaign.sender_title, cta_link=campaign.cta_link,
            status=CampaignStatus(campaign.status), row_version=campaign.row_version or 1,
            paused_reason=campaign.paused_reason, paused_at=campaign.paused_at,
            auto_paused=bool(campaign.auto_paused), created_by=campaign.created_by,
            creator_name=self._get_creator_name(campaign.created_by),
            send_window_start=campaign.send_window_start, send_window_end=campaign.send_window_end,
            respect_timezone=campaign.respect_timezone, campaign_timezone=campaign.campaign_timezone or "UTC",
            sending_mode=campaign.sending_mode or "spread", min_gap_minutes=int(campaign.min_gap_minutes or 2),
            batch_size=campaign.batch_size, batch_gap_minutes=int(campaign.batch_gap_minutes or 30),
            unsubscribe_mode=campaign.unsubscribe_mode or "plain", daily_batch_size=campaign.daily_batch_size,
            start_date=campaign.start_date, end_date=campaign.end_date,
            created_at=campaign.created_at, updated_at=campaign.updated_at,
            metrics=CampaignMetrics(
                sent_count=metrics["sent_count"], opened_count=metrics["opened_count"],
                replied_count=metrics["replied_count"], bounced_count=metrics["bounced_count"],
                sender_bounced_count=metrics.get("sender_bounced_count", 0),
                positive_replied_count=metrics.get("positive_replied_count", 0),
                ooo_count=metrics.get("ooo_count", 0), unsubscribed_count=metrics.get("unsubscribed_count", 0),
            ),
            sequence_count=self._get_sequence_count(campaign.campaign_id),
            prospect_count=self._get_prospect_count(campaign.campaign_id),
            sending_inboxes=[inbox_view(inbox) for inbox in (campaign.inboxes or [])],
            source_campaign_id=campaign.source_campaign_id,
            wait_business_days=bool(campaign.wait_business_days),
        )
