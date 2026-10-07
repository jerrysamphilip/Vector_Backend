# app/services/campaign_crud_service.py
"""
Campaign CRUD service: create, read, update, delete, state transitions,
sequence configuration, and prospect listing.
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional, Tuple, Set, Dict
from datetime import datetime, timedelta, time
import uuid

from app.core.config import settings

from app.models.campaign import Campaign, CampaignStateEvent, CampaignProspect
from app.models.user import User
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectListMember
from app.models.metrics import CampaignMetricsRealtime
from app.models.email_sequence import EmailSequence
from app.models.email_message import EmailMessage, EmailEvent
from app.models.email_template import EmailTemplate
from app.models.sending_inbox import SendingInbox
from app.models.domain_reputation import SendingDomain
from app.schemas.campaign_schema import (
    CampaignCreate,
    CampaignUpdate,
    CampaignFilter,
    CampaignStatus,
    CampaignResponse,
    CampaignListItem,
    CampaignListResponse,
    CampaignMetrics,
    CampaignEnrollmentRequest,
)
from app.schemas.sequence_schema import SequenceStepCreate, SequenceStepResponse
from app.services.audit_service import AuditService
from app.services.metrics_service import MetricsService
from app.services.campaign_analytics_service import CampaignAnalyticsService
from app.utils.email_utils import normalize_cta_link


class CampaignCRUDService:
    """
    Handles core campaign CRUD operations and simple state transitions
    (pause, resume, complete). Launch is delegated to CampaignEmailService.
    """

    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)
        self.metrics = MetricsService(db)
        self.analytics = CampaignAnalyticsService(db)

    # =============================
    # CRUD OPERATIONS
    # =============================

    def create(
        self,
        tenant_id: str,
        user_id: str,
        data: CampaignCreate
    ) -> Campaign:
        """
        Create a new campaign.
        
        Args:
            tenant_id: Tenant ID
            user_id: Creating user ID
            data: Campaign creation data
        
        Returns:
            Created Campaign
        """
        campaign = Campaign(
            campaign_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            campaign_name=data.campaign_name,
            sender_name=data.sender_name,
            sender_title=data.sender_title,
            cta_link=normalize_cta_link(data.cta_link),
            status=CampaignStatus.DRAFT.value,
            created_by=user_id,
            send_window_start=data.send_window_start or time(9, 0),
            send_window_end=data.send_window_end or time(17, 0),
            respect_timezone=data.respect_timezone,
            campaign_timezone=data.campaign_timezone,
            sending_mode=data.sending_mode,
            min_gap_minutes=data.min_gap_minutes,
            batch_size=data.batch_size,
            batch_gap_minutes=data.batch_gap_minutes,
            daily_batch_size=data.daily_batch_size,
            start_date=data.start_date,
            end_date=data.end_date,
        )
        
        self.db.add(campaign)
        
        # Handle Inboxes
        if data.inbox_ids:
            campaign.inboxes = self._validate_and_load_inboxes(tenant_id, data.inbox_ids)
            
        self.db.flush()
        
        # Initialize metrics
        self.metrics.get_or_create_metrics(campaign.campaign_id)
        
        # Log creation
        self.audit.log_action(
            tenant_id=tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_CREATE,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign.campaign_id,
        )
        
        return campaign
    
    def get(self, campaign_id: str) -> Optional[Campaign]:
        """Get a campaign by ID."""
        return self.db.query(Campaign).filter(
            Campaign.campaign_id == campaign_id
        ).first()

    def get_with_details(self, campaign_id: str) -> Optional[CampaignResponse]:
        """Get a campaign with full details including metrics."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        
        return self._to_response(campaign)
    def update(
        self,
        campaign_id: str,
        user_id: str,
        data: CampaignUpdate
    ) -> Optional[Campaign]:
        """
        Update a campaign.
        
        Args:
            campaign_id: Campaign ID
            user_id: User making the update
            data: Update data
        
        Returns:
            Updated Campaign or None if not found
        """
        campaign = self.get(campaign_id)
        if not campaign:
            return None
        
        # Update fields if provided
        if data.campaign_name is not None:
            campaign.campaign_name = data.campaign_name
        if data.sender_name is not None:
            campaign.sender_name = data.sender_name
        if data.sender_title is not None:
            campaign.sender_title = data.sender_title
        if data.cta_link is not None:
            campaign.cta_link = normalize_cta_link(data.cta_link)
        if data.send_window_start is not None:
            campaign.send_window_start = data.send_window_start
        if data.send_window_end is not None:
            campaign.send_window_end = data.send_window_end
        if data.respect_timezone is not None:
            campaign.respect_timezone = data.respect_timezone
        if data.campaign_timezone is not None:
            campaign.campaign_timezone = data.campaign_timezone
        if data.start_date is not None:
            campaign.start_date = data.start_date
        if data.end_date is not None:
            campaign.end_date = data.end_date
        if data.sending_mode is not None:
            campaign.sending_mode = data.sending_mode
        if data.min_gap_minutes is not None:
            campaign.min_gap_minutes = data.min_gap_minutes
        if data.batch_size is not None:
            campaign.batch_size = data.batch_size
        if data.batch_gap_minutes is not None:
            campaign.batch_gap_minutes = data.batch_gap_minutes
        if getattr(data, 'unsubscribe_mode', None) is not None:
            campaign.unsubscribe_mode = data.unsubscribe_mode
        if data.daily_batch_size is not None:
            campaign.daily_batch_size = data.daily_batch_size

        # Update Inboxes
        if data.inbox_ids is not None:
            campaign.inboxes = self._validate_and_load_inboxes(campaign.tenant_id, data.inbox_ids)
            self._rebind_pending_messages_for_campaign(campaign)
        
        self.db.flush()
        
        # Log update
        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_UPDATE,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )
        
        return campaign
    
    def delete(self, campaign_id: str, user_id: str) -> bool:
        """
        Delete a campaign and all related data (hard delete with cascade).
        
        Args:
            campaign_id: Campaign ID
            user_id: User deleting the campaign
        
        Returns:
            True if deleted, False if not found
        """
        campaign = self.get(campaign_id)
        if not campaign:
            return False
        
        # Log before delete
        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_DELETE,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )
        
        # Cascade delete related records (order matters - children first)
        
        # 0. Delete email events (children of email messages)
        self.db.query(EmailEvent).filter(
            EmailEvent.message_id.in_(
                self.db.query(EmailMessage.message_id).filter(
                    EmailMessage.campaign_id == campaign_id
                )
            )
        ).delete(synchronize_session=False)

        # 1. Delete email messages
        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id
        ).delete(synchronize_session=False)
        
        # 2a. Delete email template versions (must be before templates)
        from app.models.email_template import EmailTemplateVersion
        template_ids = [t[0] for t in self.db.query(EmailTemplate.template_id).filter(
            EmailTemplate.campaign_id == campaign_id
        ).all()]
        if template_ids:
            self.db.query(EmailTemplateVersion).filter(
                EmailTemplateVersion.template_id.in_(template_ids)
            ).delete(synchronize_session=False)
        
        # 2b. Delete email templates
        self.db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id
        ).delete()
        
        # 3. Delete email sequences
        self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).delete()
        
        # 4. Delete campaign prospects (enrollments)
        self.db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id
        ).delete()
        
        # 5. Delete campaign metrics
        self.db.query(CampaignMetricsRealtime).filter(
            CampaignMetricsRealtime.campaign_id == campaign_id
        ).delete()
        
        # 6. Delete campaign state events
        self.db.query(CampaignStateEvent).filter(
            CampaignStateEvent.campaign_id == campaign_id
        ).delete()
        
        # 7. Delete campaign group memberships
        from app.models.campaign_group import CampaignGroupMember
        self.db.query(CampaignGroupMember).filter(
            CampaignGroupMember.campaign_id == campaign_id
        ).delete()
        
        # 8. Delete automation rules
        from app.models.automation_rule import AutomationRule
        self.db.query(AutomationRule).filter(
            AutomationRule.campaign_id == campaign_id
        ).delete()
        
        # 9. Finally delete the campaign itself
        self.db.delete(campaign)
        self.db.flush()
        return True
    
    # =============================
    # CONFIGURATION
    # =============================
    
    def add_sequence_step(
        self,
        campaign_id: str,
        data: SequenceStepCreate
    ) -> EmailSequence:
        """Add a step to the campaign sequence."""
        campaign = self.get(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")

        count = self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar()

        if count >= 7:
            raise ValueError("Maximum 7 steps allowed per campaign")

        existing = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == data.step_number
        ).first()

        if existing:
            raise ValueError(f"Step {data.step_number} already exists")

        step = EmailSequence(
            sequence_id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            step_number=data.step_number,
            wait_days=data.wait_days,
            stop_on_reply=data.stop_on_reply,
            stop_on_bounce=data.stop_on_bounce,
            send_start_hour=data.send_start_hour,
            send_end_hour=data.send_end_hour,
        )
        self.db.add(step)
        self.db.flush()
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
        campaign.paused_at = datetime.utcnow()
        campaign.auto_paused = False

        # Freeze all queued emails so the scheduler doesn't pick them up
        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status.in_(["QUEUED", "SCHEDULED"])
        ).update({"status": "PAUSED_BY_CAMPAIGN"}, synchronize_session=False)

        self._record_state_change(campaign_id, old_status, campaign.status, reason)

        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_PAUSE,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )

        self.db.flush()
        return campaign

    def resume(self, campaign_id: str, user_id: str) -> Optional[Campaign]:
        """Resume a paused campaign and restore its frozen emails."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None

        if campaign.status != CampaignStatus.PAUSED.value:
            raise ValueError(f"Cannot resume campaign with status: {campaign.status}")

        old_status = campaign.status
        campaign.status = CampaignStatus.ACTIVE.value
        if campaign.auto_paused:
            # The user has reviewed the auto-pause: judge health on sends from now on (BR-DF-07)
            campaign.health_baseline_at = datetime.utcnow()
        campaign.auto_paused = False
        campaign.paused_reason = None
        campaign.paused_at = None

        # Restore frozen emails
        self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status == "PAUSED_BY_CAMPAIGN"
        ).update({"status": "QUEUED"}, synchronize_session=False)

        self._record_state_change(campaign_id, old_status, campaign.status)

        self.audit.log_action(
            tenant_id=campaign.tenant_id,
            user_id=user_id,
            action=AuditService.ACTION_RESUME,
            entity_type=AuditService.ENTITY_CAMPAIGN,
            entity_id=campaign_id,
        )

        self.db.flush()
        return campaign

    def complete(self, campaign_id: str) -> Optional[Campaign]:
        """Mark a campaign as completed."""
        campaign = self.get(campaign_id)
        if not campaign:
            return None

        old_status = campaign.status
        campaign.status = CampaignStatus.COMPLETED.value

        self._record_state_change(campaign_id, old_status, campaign.status)
        self.db.flush()

        return campaign
    
    # =============================
    # FILTERING & SEARCH
    # =============================
    def list_campaigns(
        self,
        tenant_id: str,
        filters: CampaignFilter
    ) -> CampaignListResponse:
        """
        List campaigns with filtering and pagination.
        
        Args:
            tenant_id: Tenant ID
            filters: Filter and pagination options
        
        Returns:
            Paginated CampaignListResponse
        """
        query = self.db.query(Campaign).filter(
            Campaign.tenant_id == tenant_id
        )
        
        # Apply filters
        if filters.status:
            query = query.filter(Campaign.status == filters.status.value)
        
        if filters.created_by:
            query = query.filter(Campaign.created_by == filters.created_by)
        
        if filters.campaign_name:
            query = query.filter(
                Campaign.campaign_name.ilike(f"%{filters.campaign_name}%")
            )
        
        # Get total count
        total = query.count()
        
        # Apply pagination
        offset = (filters.page - 1) * filters.page_size
        campaigns = query.order_by(Campaign.created_at.desc()).offset(offset).limit(filters.page_size).all()
        
        # 1. Bulk fetch metrics
        campaign_ids = [c.campaign_id for c in campaigns]
        metrics_map = self.analytics.get_bulk_metrics(campaign_ids)
        
        # 1b. Bulk prospect counts per campaign
        prospect_count_rows = self.db.query(
            CampaignProspect.campaign_id,
            func.count(CampaignProspect.id)
        ).filter(
            CampaignProspect.campaign_id.in_(campaign_ids)
        ).group_by(CampaignProspect.campaign_id).all()
        prospect_count_map = {r[0]: r[1] for r in prospect_count_rows}
        
        # 2. Bulk fetch creator names
        user_ids = list(set(c.created_by for c in campaigns))
        users = self.db.query(User.user_id, User.first_name, User.last_name).filter(User.user_id.in_(user_ids)).all()
        user_map = {u[0]: f"{u[1]} {u[2]}" for u in users}

        # 3. Convert to list items using bulk data
        items = []
        for c in campaigns:
            metrics = metrics_map.get(c.campaign_id, {
                "sent_count": 0, "opened_count": 0, "replied_count": 0,
                "bounced_count": 0, "sender_bounced_count": 0,
                "positive_replied_count": 0, "ooo_count": 0,
                "unsubscribed_count": 0, "in_progress_count": 0,
            })

            items.append(CampaignListItem(
                campaign_id=c.campaign_id,
                campaign_name=c.campaign_name,
                status=CampaignStatus(c.status),
                created_by=c.created_by,
                creator_name=user_map.get(c.created_by),
                sent_count=metrics["sent_count"],
                opened_count=metrics["opened_count"],
                replied_count=metrics["replied_count"],
                bounced_count=metrics["bounced_count"],
                sender_bounced_count=metrics["sender_bounced_count"],
                positive_replied_count=metrics["positive_replied_count"],
                ooo_count=metrics["ooo_count"],
                unsubscribed_count=metrics["unsubscribed_count"],
                in_progress_count=metrics["in_progress_count"],
                prospect_count=prospect_count_map.get(c.campaign_id, 0),
                created_at=c.created_at,
                updated_at=c.updated_at,
            ))
        
        # Calculate total pages
        total_pages = (total + filters.page_size - 1) // filters.page_size
        
        return CampaignListResponse(
            items=items,
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            total_pages=total_pages,
        )

    # =============================
    # PROSPECTS
    # =============================

    def get_enrolled_prospects(self, campaign_id: str) -> List:
        """Get all enrolled prospects for a campaign with their details."""
        # Query campaign_prospects and join with prospects table for full details
        enrolled = (
            self.db.query(CampaignProspect, Prospect)
            .join(Prospect, CampaignProspect.prospect_id == Prospect.prospect_id)
            .filter(CampaignProspect.campaign_id == campaign_id)
            .all()
        )
        
        # Return with combined data
        results = []
        
        # Batch lookup assigned inboxes (Sticky Sender)
        prospect_ids = [p.prospect_id for cp, p in enrolled]
        inbox_map = {}
        notes_map = {}
        if prospect_ids:
            from app.models.email_message import EmailMessage
            from app.models.sending_inbox import SendingInbox
            from app.models.prospect_list import ProspectListMember
            
            # Find the first SENT message for each prospect in this campaign
            messages = (
                self.db.query(EmailMessage.prospect_id, EmailMessage.from_email, SendingInbox.email_address)
                .outerjoin(SendingInbox, EmailMessage.inbox_id == SendingInbox.inbox_id)
                .filter(
                    EmailMessage.campaign_id == campaign_id,
                    EmailMessage.prospect_id.in_(prospect_ids),
                    EmailMessage.status == "SENT"
                )
                .all()
            )
            for pid, from_email, inbox_email in messages:
                # Priority: 1. Linked Inbox Email, 2. Snapshot From Email, 3. Placeholder
                sender = inbox_email or from_email or "System Default"
                inbox_map[pid] = sender
            
            # Batch lookup notes from ProspectListMember (most recent non-empty note)
            note_rows = (
                self.db.query(ProspectListMember.prospect_id, ProspectListMember.notes, ProspectListMember.list_id)
                .filter(
                    ProspectListMember.prospect_id.in_(prospect_ids),
                    ProspectListMember.notes.isnot(None),
                    ProspectListMember.notes != "",
                )
                .order_by(ProspectListMember.added_at.desc())
                .all()
            )
            for pid, note, lid in note_rows:
                if pid not in notes_map:  # Keep the most recent one
                    notes_map[pid] = {"notes": note, "list_id": lid}
        
        for cp, prospect in enrolled:
            prospect.status = cp.status  # Add campaign-specific status
            prospect.assigned_inbox = inbox_map.get(prospect.prospect_id)
            note_data = notes_map.get(prospect.prospect_id, {})
            prospect.notes = note_data.get("notes")
            prospect.notes_list_id = note_data.get("list_id")
            results.append(prospect)
        
        return results

    # =============================
    # HELPER METHODS
    # =============================
    
    def _validate_and_load_inboxes(self, tenant_id: str, inbox_ids: List[str]) -> List[SendingInbox]:
        """
        Validate selected inboxes for campaign usage:
        - inbox belongs to current tenant
        - inbox status is ACTIVE
        - domain is not blacklisted (if tracked in sending_domains)
        """
        unique_ids = list(dict.fromkeys(inbox_ids or []))
        if not unique_ids:
            return []

        inboxes = (
            self.db.query(SendingInbox)
            .filter(
                SendingInbox.tenant_id == tenant_id,
                SendingInbox.inbox_id.in_(unique_ids),
            )
            .all()
        )

        if len(inboxes) != len(unique_ids):
            found_ids = {i.inbox_id for i in inboxes}
            missing = [i for i in unique_ids if i not in found_ids]
            raise ValueError(f"Invalid inbox selection. Not found in tenant: {', '.join(missing)}")

        inactive = [i.email_address for i in inboxes if (i.status or "").upper() != "ACTIVE"]
        if inactive:
            raise ValueError(f"Selected inboxes are not ACTIVE: {', '.join(inactive)}")

        domains = {
            i.email_address.split("@", 1)[1].lower()
            for i in inboxes
            if i.email_address and "@" in i.email_address
        }
        if domains:
            domain_rows = (
                self.db.query(SendingDomain.domain_name, SendingDomain.is_blacklisted)
                .filter(SendingDomain.domain_name.in_(domains))
                .all()
            )
            blocked = [name for name, is_blacklisted in domain_rows if is_blacklisted]
            if blocked:
                raise ValueError(
                    f"Selected inbox domains are blocked for sending: {', '.join(sorted(set(blocked)))}"
                )

        return inboxes

    def _record_state_change(
        self,
        campaign_id: str,
        from_state: str,
        to_state: str,
        reason: Optional[str] = None
    ):
        """Record a campaign state transition."""
        event = CampaignStateEvent(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
        self.db.add(event)

    def _rebind_pending_messages_for_campaign(self, campaign: Campaign) -> None:
        """
        Rebind unsent campaign messages to the currently selected sending inboxes.

        This repairs older queued messages that were created before inbox binding
        was fixed and keeps pending messages aligned when inbox selection changes.
        """
        pending_messages = (
            self.db.query(EmailMessage)
            .filter(
                EmailMessage.campaign_id == campaign.campaign_id,
                EmailMessage.sent_at.is_(None),
                EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN", "FAILED"]),
            )
            .order_by(EmailMessage.prospect_id.asc(), EmailMessage.scheduled_at.asc())
            .all()
        )
        if not pending_messages:
            return

        active_inboxes = sorted(
            [inbox for inbox in (campaign.inboxes or []) if (inbox.status or "").upper() == "ACTIVE"],
            key=lambda inbox: (inbox.email_address or "").lower(),
        )

        if not active_inboxes:
            for message in pending_messages:
                message.inbox_id = None
                message.from_email = settings.SENDER_EMAIL
            self.db.flush()
            return

        active_by_id = {inbox.inbox_id: inbox for inbox in active_inboxes}
        active_by_email = {
            (inbox.email_address or "").strip().lower(): inbox
            for inbox in active_inboxes
            if inbox.email_address
        }

        prospect_sender_map: Dict[str, SendingInbox] = {}
        sent_messages = (
            self.db.query(EmailMessage)
            .filter(
                EmailMessage.campaign_id == campaign.campaign_id,
                EmailMessage.sent_at.isnot(None),
            )
            .order_by(EmailMessage.sent_at.desc())
            .all()
        )
        for sent_message in sent_messages:
            if sent_message.prospect_id in prospect_sender_map:
                continue
            chosen_inbox = None
            if sent_message.inbox_id:
                chosen_inbox = active_by_id.get(sent_message.inbox_id)
            if not chosen_inbox and sent_message.from_email:
                chosen_inbox = active_by_email.get(sent_message.from_email.strip().lower())
            if chosen_inbox:
                prospect_sender_map[sent_message.prospect_id] = chosen_inbox

        next_rotation_index = 0
        for message in pending_messages:
            assigned_inbox = prospect_sender_map.get(message.prospect_id)
            if not assigned_inbox:
                assigned_inbox = active_inboxes[next_rotation_index % len(active_inboxes)]
                prospect_sender_map[message.prospect_id] = assigned_inbox
                next_rotation_index += 1

            message.inbox_id = assigned_inbox.inbox_id
            message.from_email = assigned_inbox.email_address

        self.db.flush()
    
    def _get_creator_name(self, user_id: str) -> Optional[str]:
        """Get creator's full name."""
        user = self.db.query(User).filter(User.user_id == user_id).first()
        return user.full_name if user else None
    
    def _get_sequence_count(self, campaign_id: str) -> int:
        """Get number of sequence steps."""
        return self.db.query(func.count(EmailSequence.sequence_id)).filter(
            EmailSequence.campaign_id == campaign_id
        ).scalar() or 0
    
    def _get_prospect_count(self, campaign_id: str) -> int:
        """Get number of enrolled prospects."""
        return self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id
        ).scalar() or 0
    
    def _to_response(self, campaign: Campaign) -> CampaignResponse:
        """Convert Campaign to full response."""
        metrics = self.analytics.get_dynamic_metrics(campaign.campaign_id)
        
        # Serialize assigned inboxes for frontend tabs
        sending_inboxes = []
        for inbox in (campaign.inboxes or []):
            sending_inboxes.append({
                "inbox_id": inbox.inbox_id,
                "email_address": inbox.email_address,
                "provider": getattr(inbox, "provider", "SMTP"),
                "status": inbox.status,
                "daily_limit": getattr(inbox, "daily_limit", 50),
                "last_sync_at": inbox.last_sync_at.isoformat() if getattr(inbox, "last_sync_at", None) else None,
                "imap_host": getattr(inbox, "imap_host", None),
                "imap_port": getattr(inbox, "imap_port", 993),
                "imap_username": getattr(inbox, "imap_username", None),
            })

        return CampaignResponse(
            campaign_id=campaign.campaign_id,
            tenant_id=campaign.tenant_id,
            campaign_name=campaign.campaign_name,
            sender_name=campaign.sender_name,
            sender_title=campaign.sender_title,
            cta_link=campaign.cta_link,
            status=CampaignStatus(campaign.status),
            paused_reason=campaign.paused_reason,
            paused_at=campaign.paused_at,
            auto_paused=bool(campaign.auto_paused),
            created_by=campaign.created_by,
            creator_name=self._get_creator_name(campaign.created_by),
            send_window_start=campaign.send_window_start,
            send_window_end=campaign.send_window_end,
            respect_timezone=campaign.respect_timezone,
            campaign_timezone=campaign.campaign_timezone or "UTC",
            sending_mode=campaign.sending_mode or "spread",
            min_gap_minutes=int(campaign.min_gap_minutes or 2),
            batch_size=campaign.batch_size,
            batch_gap_minutes=int(campaign.batch_gap_minutes or 30),
            unsubscribe_mode=campaign.unsubscribe_mode or "plain",
            daily_batch_size=campaign.daily_batch_size,
            start_date=campaign.start_date,
            end_date=campaign.end_date,
            created_at=campaign.created_at,
            updated_at=campaign.updated_at,
            metrics=CampaignMetrics(
                sent_count=metrics["sent_count"],
                opened_count=metrics["opened_count"],
                replied_count=metrics["replied_count"],
                bounced_count=metrics["bounced_count"],
                sender_bounced_count=metrics.get("sender_bounced_count", 0),
                positive_replied_count=metrics.get("positive_replied_count", 0),
                ooo_count=metrics.get("ooo_count", 0),
                unsubscribed_count=metrics.get("unsubscribed_count", 0),
            ),
            sequence_count=self._get_sequence_count(campaign.campaign_id),
            prospect_count=self._get_prospect_count(campaign.campaign_id),
            sending_inboxes=sending_inboxes,
        )
    
    def _to_list_item(self, campaign: Campaign) -> CampaignListItem:
        """Convert Campaign to list item."""
        metrics = self.analytics.get_dynamic_metrics(campaign.campaign_id)
        
        return CampaignListItem(
            campaign_id=campaign.campaign_id,
            campaign_name=campaign.campaign_name,
            status=CampaignStatus(campaign.status),
            created_by=campaign.created_by,
            creator_name=self._get_creator_name(campaign.created_by),
            sent_count=metrics["sent_count"],
            opened_count=metrics["opened_count"],
            replied_count=metrics["replied_count"],
            bounced_count=metrics["bounced_count"],
            created_at=campaign.created_at,
            updated_at=campaign.updated_at,
        )
