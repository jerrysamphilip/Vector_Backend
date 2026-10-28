"""
Warmup activity event log per inbox.
"""

from datetime import datetime
from typing import Any, Optional
import uuid

from sqlalchemy import String, ForeignKey, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class InboxWarmupEvent(Base):
    __tablename__ = "inbox_warmup_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    inbox_id: Mapped[str] = mapped_column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=False)
    peer_inbox_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=True)

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # SENT / RECEIVED / OPENED / REPLIED / SAVED_FROM_SPAM / ISSUE
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SUCCESS")
    subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[Any] = mapped_column(JSON, nullable=True)
    event_time: Mapped[datetime] = mapped_column(UtcDateTime, server_default=NOW6, nullable=False)

    inbox = relationship("SendingInbox", foreign_keys=[inbox_id], back_populates="warmup_events")
    peer_inbox = relationship("SendingInbox", foreign_keys=[peer_inbox_id])

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
