"""
SQL for reply classification (S-09, ADR 0009): inbound replies, the sent email they answer, their
analytics events, and the positive-replier lookup used to keep those contacts out of new enrolments.
Never commits; the calling service owns the unit of work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, List, Optional, Set

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.repositories.base import SqlAlchemyUnitOfWork

POSITIVE = "POSITIVE"


class ReplyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_reply(self, message_id: str, *, lock: bool = False) -> Optional[EmailMessage]:
        query = self._session.query(EmailMessage).filter(
            EmailMessage.message_id == message_id, EmailMessage.direction == "INBOUND")
        if lock:
            query = query.with_for_update()
        return query.first()

    def get_outbound(self, message_id: Optional[str]) -> Optional[EmailMessage]:
        if not message_id:
            return None
        return self._session.query(EmailMessage).filter(
            EmailMessage.message_id == message_id, EmailMessage.direction == "OUTBOUND").first()

    def thread_outbound(self, reply: EmailMessage) -> Optional[EmailMessage]:
        """The newest email sent in the reply's thread no later than the reply (what it most likely answers)."""
        if not reply.conversation_id:
            return None
        query = self._session.query(EmailMessage).filter(
            EmailMessage.conversation_id == reply.conversation_id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.isnot(None),
        )
        if reply.sent_at is not None:
            query = query.filter(EmailMessage.sent_at <= reply.sent_at)
        return query.order_by(desc(EmailMessage.sent_at)).first()

    def get_prospect(self, prospect_id: str) -> Optional[Prospect]:
        return self._session.get(Prospect, prospect_id)

    def event_exists(self, message_id: str, event_type: int, reply: EmailMessage) -> bool:
        """This reply's event of this type on the sent email: tagged with the reply's id, or (written by
        the pipeline before labels were stored) untagged at the reply's time."""
        tagged = func.json_unquote(func.json_extract(EmailEvent.event_metadata, "$.reply_message_id"))
        legacy = and_(EmailEvent.event_metadata.is_(None), EmailEvent.event_time == reply.sent_at)
        return self._session.query(EmailEvent.event_id).filter(
            EmailEvent.message_id == message_id, EmailEvent.event_type == event_type,
            or_(tagged == reply.message_id, legacy),
        ).first() is not None

    def add_event(self, message_id: str, event_type: int, event_time: Optional[datetime],
                  metadata: Optional[dict[str, Any]] = None) -> None:
        self._session.add(EmailEvent(message_id=message_id, event_type=event_type, event_time=event_time,
                                     event_metadata=metadata))

    def pending_reply_ids(self, since: datetime, max_attempts: int, limit: int) -> List[str]:
        """Inbound replies not classified yet, newest first, with attempts left."""
        rows = self._session.query(EmailMessage.message_id).filter(
            EmailMessage.direction == "INBOUND",
            EmailMessage.reply_classified_at.is_(None),
            EmailMessage.sent_at >= since,
            func.coalesce(EmailMessage.reply_classify_attempts, 0) < max_attempts,
        ).order_by(desc(EmailMessage.sent_at)).limit(limit).all()
        return [message_id for (message_id,) in rows]

    def positive_replier_ids(self, tenant_id: str, prospect_ids: Iterable[str]) -> Set[str]:
        """Contacts of this tenant that sent a reply labelled POSITIVE (excluded from new enrolment)."""
        ids = list(prospect_ids)
        if not ids:
            return set()
        rows = self._session.query(EmailMessage.prospect_id).join(
            Prospect, Prospect.prospect_id == EmailMessage.prospect_id,
        ).filter(
            Prospect.tenant_id == tenant_id,
            EmailMessage.prospect_id.in_(ids),
            EmailMessage.direction == "INBOUND",
            EmailMessage.reply_label == POSITIVE,
        ).distinct().all()
        return {prospect_id for (prospect_id,) in rows}


def positive_replier_ids(session: Session, tenant_id: str, prospect_ids: Iterable[str]) -> Set[str]:
    """Function form for app/services/enrollment_rules.py (see the S-09 report)."""
    return ReplyRepository(session).positive_replier_ids(tenant_id, prospect_ids)


class ReplyUnitOfWork(SqlAlchemyUnitOfWork):
    """Unit of work with the reply repository (``SessionLocal`` for the job, ``for_session`` for IMAP sync)."""

    def __enter__(self) -> "ReplyUnitOfWork":
        super().__enter__()
        return self

    @property
    def replies(self) -> ReplyRepository:
        return ReplyRepository(self.session)

    @classmethod
    def for_session(cls, session: Session) -> "ReplyUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
