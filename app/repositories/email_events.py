"""
Email event repository and unit of work (ADR 0009; BR-DF-04 / B09): what the SES/SNS webhook and the
public tracking endpoints (open pixel, click redirect, unsubscribe) read and write.

Both are reached without a user session and for any workspace, so these lookups are cross-tenant by
design: they start from an opaque message id (or SES's messageId) and resolve the tenant an event
belongs to (the message's contact, else every workspace holding the address). Never commits; the
services own the transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.repositories.base import SqlAlchemyUnitOfWork

__all__ = [
    "Session", "EmailEventRepository", "EmailEventsUnitOfWork", "SqlAlchemyEmailEventRepository",
    "SqlAlchemyEmailEventsUnitOfWork",
]


class EmailEventRepository(Protocol):
    def prospect_tenant(self, prospect_id: str) -> Optional[str]: ...
    def tenants_with_email(self, email: str) -> list[str]: ...
    def message_id_for_provider_id(self, ses_message_id: str) -> Optional[str]: ...
    def has_event_with_key(self, message_id: str, dedupe_key: str) -> bool: ...
    def message(self, message_id: str) -> Optional[EmailMessage]: ...
    def conversation(self, conversation_id: str) -> Optional[Conversation]: ...
    def prospect(self, prospect_id: str) -> Optional[Prospect]: ...
    def add_event(self, message_id: str, event_type: int, metadata: dict,
                  at: Optional[datetime] = None) -> EmailEvent: ...
    def add_message(self, **fields: Any) -> EmailMessage: ...


class SqlAlchemyEmailEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def prospect_tenant(self, prospect_id: str) -> Optional[str]:
        row = self._session.query(Prospect.tenant_id).filter(Prospect.prospect_id == prospect_id).first()
        return row[0] if row else None

    def tenants_with_email(self, email: str) -> list[str]:
        return [t for (t,) in self._session.query(Prospect.tenant_id).filter(
            func.lower(Prospect.email) == email.strip().lower()).distinct()]

    def message_id_for_provider_id(self, ses_message_id: str) -> Optional[str]:
        """Our message_id for SES's messageId (stored as ses_message_id, else provider_message_id)."""
        row = self._session.query(EmailMessage.message_id).filter(
            EmailMessage.ses_message_id == ses_message_id).first() \
            or self._session.query(EmailMessage.message_id).filter(
                EmailMessage.provider_message_id == ses_message_id).first()
        return row[0] if row else None

    def has_event_with_key(self, message_id: str, dedupe_key: str) -> bool:
        return self._session.query(EmailEvent.event_id).filter(
            EmailEvent.message_id == message_id,
            func.json_unquote(func.json_extract(EmailEvent.event_metadata, "$.dedupe_key")) == dedupe_key,
        ).first() is not None

    def message(self, message_id: str) -> Optional[EmailMessage]:
        return self._session.query(EmailMessage).filter(EmailMessage.message_id == message_id).first()

    def conversation(self, conversation_id: str) -> Optional[Conversation]:
        return self._session.query(Conversation).filter(Conversation.id == conversation_id).first()

    def prospect(self, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id).first()

    def add_event(self, message_id: str, event_type: int, metadata: dict,
                  at: Optional[datetime] = None) -> EmailEvent:
        """``at``: when the event happened (SES's timestamp, naive UTC); now when not given."""
        event = EmailEvent(message_id=message_id, event_type=event_type, event_time=at or utcnow(),
                           event_metadata=metadata)
        self._session.add(event)
        return event

    def add_message(self, **fields: Any) -> EmailMessage:
        message = EmailMessage(message_id=str(uuid.uuid4()), **fields)
        self._session.add(message)
        return message


class EmailEventsUnitOfWork(Protocol):
    @property
    def events(self) -> EmailEventRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> EmailEventsUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyEmailEventsUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyEmailEventsUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyEmailEventsUnitOfWork:
        super().__enter__()
        return self

    @property
    def events(self) -> SqlAlchemyEmailEventRepository:
        return SqlAlchemyEmailEventRepository(self.session)
