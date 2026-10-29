"""
SQL for the IMAP reply sync (BR-DF-05, ADR 0009): the inboxes to sync, the per-folder UID position, the
contact a message belongs to (tenant-scoped), the sent email a reply answers, the conversation it is
threaded into, its campaign attribution, and the cancellation of pending mail of no step on reply.
Never commits: app/services/imap_sync_service.py owns each message's short transaction (T-1).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, List, Optional, Sequence

from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from app.core.auth import BLOCKED_TENANT_STATUSES
from app.domain.inbox_sync import OUTBOUND, STOPPABLE_STATUSES, SUBJECT_MATCH_WINDOW, SyncPosition
from app.models.campaign import CampaignProspect
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.models.imap_sync_state import ImapSyncState
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.models.tenant import Tenant
from app.repositories.base import SqlAlchemyUnitOfWork
from app.repositories.contacts import SqlAlchemyContactRepository

__all__ = ["ImapSyncRepository", "ImapSyncUnitOfWork", "Session"]


class ImapSyncRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Inboxes and positions ─────────────────────────────────────────────
    def inboxes_to_sync(self) -> List[SendingInbox]:
        """Every inbox with an IMAP host (all tenants: the background job), except those of suspended /
        deleted workspaces (DEC-20): their mail stays on the server and is picked up after reactivation."""
        return list(self._session.query(SendingInbox).join(Tenant, Tenant.tenant_id == SendingInbox.tenant_id).filter(
            SendingInbox.imap_host.isnot(None),
            func.upper(func.coalesce(Tenant.status, "ACTIVE")).notin_(sorted(BLOCKED_TENANT_STATUSES))).all())

    def get_inbox(self, inbox_id: str) -> Optional[SendingInbox]:
        return self._session.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).first()

    def position(self, inbox_id: str, folder: str) -> Optional[SyncPosition]:
        row = self._state(inbox_id, folder)
        return None if row is None else SyncPosition(row.uidvalidity, row.last_uid)

    def _state(self, inbox_id: str, folder: str) -> Optional[ImapSyncState]:
        return self._session.query(ImapSyncState).filter(
            ImapSyncState.inbox_id == inbox_id, ImapSyncState.folder == folder).first()

    def save_position(self, inbox_id: str, folder: str, uidvalidity: Optional[int], last_uid: int) -> None:
        """Upsert the folder's position (looked up each time: it is stored after every message)."""
        row = self._state(inbox_id, folder)
        if row is None:
            row = ImapSyncState(inbox_id=inbox_id, folder=folder)
            self._session.add(row)
        row.uidvalidity = uidvalidity
        row.last_uid = last_uid

    # ── The contact and duplicates ────────────────────────────────────────
    def find_prospect(self, tenant_id: Optional[str], email: str) -> Optional[Prospect]:
        """The contact with this address in the inbox's workspace, or the contact a retired or merged
        address now belongs to (C-11: a reply from an old address goes to the surviving contact). The
        same address can be a contact in several workspaces; mail is never filed into another one."""
        if not tenant_id:
            return None
        return SqlAlchemyContactRepository(self._session).resolve_contact_by_email(tenant_id, email)

    def message_stored(self, provider_message_id: str, prospect_id: str) -> bool:
        return self._session.query(EmailMessage.message_id).filter(
            EmailMessage.provider_message_id == provider_message_id,
            EmailMessage.prospect_id == prospect_id).first() is not None

    # ── Matching a reply to what we sent ──────────────────────────────────
    def outbound_by_header(self, prospect_id: str, header_ids: Sequence[str]) -> Optional[EmailMessage]:
        """The newest email sent to this contact whose provider_message_id or ses_message_id is one of
        ``header_ids`` (the header value and its bare SES id)."""
        conditions = [EmailMessage.provider_message_id == header_ids[0]]
        for ses_id in header_ids[1:]:
            conditions += [EmailMessage.ses_message_id == ses_id, EmailMessage.provider_message_id == ses_id]
        return self._session.query(EmailMessage).filter(
            or_(*conditions), EmailMessage.direction == OUTBOUND, EmailMessage.prospect_id == prospect_id,
        ).order_by(desc(EmailMessage.sent_at)).first()

    def recent_outbound(self, prospect_id: str, inbox_id: str) -> List[EmailMessage]:
        """The newest emails sent to this contact from this inbox (subject fallback)."""
        return list(self._session.query(EmailMessage).filter(
            EmailMessage.prospect_id == prospect_id, EmailMessage.inbox_id == inbox_id,
            EmailMessage.direction == OUTBOUND,
        ).order_by(desc(EmailMessage.sent_at)).limit(SUBJECT_MATCH_WINDOW).all())

    def conversation(self, tenant_id: Optional[str], conversation_id: Optional[str]) -> Optional[Conversation]:
        if not conversation_id:
            return None
        return self._session.query(Conversation).filter(
            Conversation.id == conversation_id, Conversation.tenant_id == tenant_id).first()

    def conversation_for_inbox(self, prospect_id: str, inbox_id: str) -> Optional[Conversation]:
        return self._session.query(Conversation).filter(
            Conversation.prospect_id == prospect_id, Conversation.inbox_id == inbox_id).first()

    def conversation_with_outbound(self, tenant_id: Optional[str], prospect_id: str) -> Optional[Conversation]:
        """The contact's most recent conversation that has sent mail in it."""
        return self._session.query(Conversation).filter(
            Conversation.prospect_id == prospect_id, Conversation.tenant_id == tenant_id,
            Conversation.messages.any(EmailMessage.direction == OUTBOUND),
        ).order_by(desc(Conversation.last_message_at)).first()

    def add_conversation(self, conversation: Conversation) -> Conversation:
        self._session.add(conversation)
        self._session.flush()
        return conversation

    # ── Campaign attribution ──────────────────────────────────────────────
    def thread_last_campaign_outbound(self, conversation_id: str) -> Optional[EmailMessage]:
        return self._session.query(EmailMessage).filter(
            EmailMessage.conversation_id == conversation_id, EmailMessage.direction == OUTBOUND,
            EmailMessage.campaign_id.isnot(None),
        ).order_by(desc(EmailMessage.sent_at)).first()

    def latest_enrollment(self, prospect_id: str) -> Optional[CampaignProspect]:
        return self._session.query(CampaignProspect).filter(
            CampaignProspect.prospect_id == prospect_id).order_by(desc(CampaignProspect.enrolled_at)).first()

    def enrollment(self, campaign_id: str, prospect_id: str) -> Optional[CampaignProspect]:
        return self._session.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id, CampaignProspect.prospect_id == prospect_id).first()

    # ── Writes ────────────────────────────────────────────────────────────
    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def cancel_pending_without_step(self, campaign_id: str, prospect_id: str, reason: str) -> int:
        """Cancel this contact's pending campaign mail that belongs to no sequence step (automation
        mail). Step mail is stopped by the per-step rules (sequence_stop.stop_pending_steps)."""
        return self._session.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id, EmailMessage.prospect_id == prospect_id,
            EmailMessage.direction == OUTBOUND, EmailMessage.status.in_(STOPPABLE_STATUSES),
            EmailMessage.sequence_id.is_(None),
        ).update({EmailMessage.status: "CANCELLED", EmailMessage.failure_reason: reason},
                 synchronize_session=False)


class ImapSyncUnitOfWork(SqlAlchemyUnitOfWork):
    """One short transaction of the sync. Leaving the block rolls back whatever was not committed, so
    no transaction stays open into the next IMAP round trip or LLM call (T-1)."""

    def __enter__(self) -> "ImapSyncUnitOfWork":
        super().__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        session = self.session
        try:
            session.rollback()  # no-op after a commit; ends a read-only or abandoned transaction
        finally:
            super().__exit__(exc_type, exc, tb)

    @property
    def sync(self) -> ImapSyncRepository:
        return ImapSyncRepository(self.session)

    def savepoint(self) -> AbstractContextManager[Any]:
        """A nested transaction: an exception inside it undoes only its own changes."""
        return self.session.begin_nested()

    @classmethod
    def for_session(cls, session: Session) -> "ImapSyncUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
