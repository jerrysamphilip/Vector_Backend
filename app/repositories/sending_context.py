"""
Lookups the sending engine makes around one message (chunk A8, ADR 0009): its campaign (and creator), contact,
template, inbox and pace, the contact's enrollment and steps, the Unified Inbox conversation, campaign
completion counts and the admins to tell about a paused mailbox.

Worker entry points act across workspaces by message / campaign id (see app/repositories/sending.py); the
tenant-facing ones (``tenant_admin_ids``) name the tenant. Stage and flush only, never commit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignProspect
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.repositories.campaigns import not_removed
from app.schemas.campaign_schema import CampaignStatus

__all__ = ["Session", "SendingContextRepository", "SqlAlchemySendingContextRepository"]

# Enrollment statuses that keep a campaign open (anything else is terminal: objective met)
NON_TERMINAL_ENROLLMENT = ("ACTIVE", "PAUSED", "OPENED", "CLICKED", "QUEUED", "SCHEDULED", "SENDING", "FAILED")
ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")


class SendingContextRepository(Protocol):
    def fresh_campaign(self, campaign_id: str) -> Optional[Campaign]: ...

    def campaign_with_creator(self, campaign_id: str) -> Optional[Tuple[Campaign, User]]: ...

    def prospect(self, prospect_id: str) -> Optional[Prospect]: ...

    def template(self, template_id: str) -> Optional[EmailTemplate]: ...

    def inbox(self, inbox_id: str) -> Optional[SendingInbox]: ...

    def inbox_pace(self, inbox_id: str) -> Optional[Tuple[Optional[int], Optional[datetime]]]: ...

    def inbox_delay(self, inbox_id: str) -> Optional[int]: ...

    def inbox_delays(self, inbox_ids: Iterable[str]) -> Dict[str, Optional[int]]: ...

    def previous_sends(self, campaign_id: str, prospect_id: str) -> List[EmailMessage]: ...

    def enrollment(self, campaign_id: str, prospect_id: str) -> Optional[CampaignProspect]: ...

    def enrollment_status(self, campaign_id: str, prospect_id: str) -> Optional[str]: ...

    def step(self, sequence_id: Optional[str]) -> Optional[EmailSequence]: ...

    def step_number(self, campaign_id: str, number: int) -> Optional[EmailSequence]: ...


class SqlAlchemySendingContextRepository:
    """``SendingContextRepository`` over a SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @classmethod
    def for_session(cls, db: Session) -> "SqlAlchemySendingContextRepository":
        return cls(db)

    # ── Campaign, contact, template ─────────────────────────────────────────

    def fresh_campaign(self, campaign_id: str) -> Optional[Campaign]:
        """The campaign re-read from the database (its status may have changed since the batch query)."""
        campaign: Optional[Campaign] = self._s.query(Campaign).populate_existing().filter(
            Campaign.campaign_id == campaign_id).first()
        return campaign

    def campaign_with_creator(self, campaign_id: str) -> Optional[Tuple[Campaign, User]]:
        row = self._s.query(Campaign, User).join(User, Campaign.created_by == User.user_id).filter(
            Campaign.campaign_id == campaign_id).first()
        return (row[0], row[1]) if row else None

    def prospect(self, prospect_id: str) -> Optional[Prospect]:
        prospect: Optional[Prospect] = self._s.query(Prospect).filter(Prospect.prospect_id == prospect_id).first()
        return prospect

    def template(self, template_id: str) -> Optional[EmailTemplate]:
        template: Optional[EmailTemplate] = self._s.query(EmailTemplate).filter(
            EmailTemplate.template_id == template_id).first()
        return template

    # ── Inbox and pace ──────────────────────────────────────────────────────

    def inbox(self, inbox_id: str) -> Optional[SendingInbox]:
        inbox: Optional[SendingInbox] = self._s.query(SendingInbox).filter(
            SendingInbox.inbox_id == inbox_id).first()
        return inbox

    def inbox_pace(self, inbox_id: str) -> Optional[Tuple[Optional[int], Optional[datetime]]]:
        """(delay_between_emails, last_sent_at), or None for an unknown inbox."""
        row = self._s.query(SendingInbox.delay_between_emails, SendingInbox.last_sent_at).filter(
            SendingInbox.inbox_id == inbox_id).first()
        return (row[0], row[1]) if row else None

    def inbox_delay(self, inbox_id: str) -> Optional[int]:
        delay: Optional[int] = self._s.query(SendingInbox.delay_between_emails).filter(
            SendingInbox.inbox_id == inbox_id).scalar()
        return delay

    def inbox_delays(self, inbox_ids: Iterable[str]) -> Dict[str, Optional[int]]:
        ids = [i for i in inbox_ids if i]
        if not ids:
            return {}
        return {inbox_id: delay for inbox_id, delay in self._s.query(
            SendingInbox.inbox_id, SendingInbox.delay_between_emails).filter(SendingInbox.inbox_id.in_(ids)).all()}

    def previous_sends(self, campaign_id: str, prospect_id: str) -> List[EmailMessage]:
        """This contact's sent mail in this campaign, newest first (the sticky sender)."""
        return self._s.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.sent_at.isnot(None),
        ).order_by(EmailMessage.sent_at.desc()).all()

    # ── Enrollment and steps ────────────────────────────────────────────────

    def enrollment(self, campaign_id: str, prospect_id: str) -> Optional[CampaignProspect]:
        enrollment: Optional[CampaignProspect] = self._s.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.prospect_id == prospect_id,
        ).first()
        return enrollment

    def enrollment_status(self, campaign_id: str, prospect_id: str) -> Optional[str]:
        row = self._s.query(CampaignProspect.status).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.prospect_id == prospect_id,
        ).first()
        return row[0] if row else None

    def step(self, sequence_id: Optional[str]) -> Optional[EmailSequence]:
        if not sequence_id:
            return None
        step: Optional[EmailSequence] = self._s.query(EmailSequence).filter(
            EmailSequence.sequence_id == sequence_id).first()
        return step

    def step_number(self, campaign_id: str, number: int) -> Optional[EmailSequence]:
        step: Optional[EmailSequence] = self._s.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == number,
        ).first()
        return step

    # ── Unified Inbox conversation ──────────────────────────────────────────

    def conversation(self, prospect_id: str, inbox_id: str) -> Optional[Conversation]:
        conv: Optional[Conversation] = self._s.query(Conversation).filter(
            Conversation.prospect_id == prospect_id,
            Conversation.inbox_id == inbox_id,
        ).first()
        return conv

    def add_conversation(self, **fields: Any) -> Conversation:
        conv = Conversation(**fields)
        self._s.add(conv)
        self._s.flush()
        return conv

    # ── Campaign completion ─────────────────────────────────────────────────

    def active_campaigns(self) -> List[Campaign]:
        return self._s.query(Campaign).filter(Campaign.status == CampaignStatus.ACTIVE.value).all()

    def enrollment_counts(self, campaign_id: str) -> Tuple[int, int]:
        """(enrolled, still open) for the campaign; contacts the user removed (S-21) count for neither."""
        total = self._s.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id, not_removed()).scalar() or 0
        open_count = self._s.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status.in_(list(NON_TERMINAL_ENROLLMENT)), not_removed()).scalar() or 0
        return int(total), int(open_count)

    # ── Mailbox auto-pause ──────────────────────────────────────────────────

    def tenant_admin_ids(self, tenant_id: str) -> List[str]:
        return [uid for (uid,) in self._s.query(User.user_id).filter(
            User.tenant_id == tenant_id, User.role.in_(ADMIN_ROLES), User.status == "ACTIVE").all()]

    def add_inbox_event(self, **fields: Any) -> InboxWarmupEvent:
        event = InboxWarmupEvent(**fields)
        self._s.add(event)
        return event
