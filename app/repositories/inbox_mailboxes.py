"""
SQL for sending inboxes (mailboxes) of the email accounts page (F-20, ADR 0009): tenant-scoped lookups,
add, and delete with what hangs off a mailbox (its conversations, warm-up rows and campaign links; the
emails are kept, unlinked). Never commits: app/services/inbox_service.py owns the unit of work.
"""

from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.inbox_warmup_metric import InboxWarmupMetric
from app.models.join_tables import campaign_inboxes
from app.models.sending_inbox import SendingInbox
from app.repositories.base import SqlAlchemyUnitOfWork

__all__ = ["MailboxRepository", "MailboxUnitOfWork", "Session", "SendingInbox"]


class MailboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, tenant_id: str, inbox_id: str) -> Optional[SendingInbox]:
        return self._session.query(SendingInbox).filter(
            SendingInbox.inbox_id == inbox_id, SendingInbox.tenant_id == tenant_id).first()

    def by_email(self, tenant_id: str, email_address: str) -> Optional[SendingInbox]:
        return self._session.query(SendingInbox).filter(
            SendingInbox.email_address == email_address, SendingInbox.tenant_id == tenant_id).first()

    def list(self, tenant_id: str) -> List[SendingInbox]:
        return list(self._session.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id)
                    .order_by(SendingInbox.email_address).all())

    def add(self, inbox: SendingInbox) -> SendingInbox:
        self._session.add(inbox)
        return inbox

    def refresh(self, entity: Any) -> None:
        self._session.refresh(entity)

    def delete(self, inbox: SendingInbox) -> None:
        """Delete the mailbox. Its emails are kept (unlinked from it and from its conversations); its
        conversations, warm-up events (as sender or peer) and metrics and its campaign links go."""
        inbox_id = inbox.inbox_id
        conversation_ids = [cid for (cid,) in self._session.query(Conversation.id)
                            .filter(Conversation.inbox_id == inbox_id).all()]
        if conversation_ids:
            self._session.query(EmailMessage).filter(EmailMessage.conversation_id.in_(conversation_ids)).update(
                {EmailMessage.conversation_id: None}, synchronize_session=False)
        self._session.query(EmailMessage).filter(EmailMessage.inbox_id == inbox_id).update(
            {EmailMessage.inbox_id: None}, synchronize_session=False)
        self._session.query(Conversation).filter(Conversation.inbox_id == inbox_id).delete(synchronize_session=False)
        # Warm-up rows first (FK); an event can reference the mailbox as sender or as peer
        self._session.query(InboxWarmupEvent).filter(
            or_(InboxWarmupEvent.inbox_id == inbox_id, InboxWarmupEvent.peer_inbox_id == inbox_id)
        ).delete(synchronize_session=False)
        self._session.query(InboxWarmupMetric).filter(InboxWarmupMetric.inbox_id == inbox_id).delete(
            synchronize_session=False)
        # Unassigned from campaigns (they keep their other mailboxes)
        self._session.execute(campaign_inboxes.delete().where(campaign_inboxes.c.inbox_id == inbox_id))
        self._session.delete(inbox)


class MailboxUnitOfWork(SqlAlchemyUnitOfWork):
    def __enter__(self) -> "MailboxUnitOfWork":
        super().__enter__()
        return self

    @property
    def inboxes(self) -> MailboxRepository:
        return MailboxRepository(self.session)

    @classmethod
    def for_session(cls, session: Session) -> "MailboxUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
