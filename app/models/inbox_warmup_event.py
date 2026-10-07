"""
Warmup activity event log per inbox.
"""

import uuid

from sqlalchemy import Column, String, TIMESTAMP, ForeignKey, JSON, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import Base


class InboxWarmupEvent(Base):
    __tablename__ = "inbox_warmup_events"

    event_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    inbox_id = Column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=False)
    peer_inbox_id = Column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=True)

    event_type = Column(String(50), nullable=False)  # SENT / RECEIVED / OPENED / REPLIED / SAVED_FROM_SPAM / ISSUE
    status = Column(String(20), nullable=False, default="SUCCESS")
    subject = Column(String(255), nullable=True)
    detail = Column(Text, nullable=True)
    event_metadata = Column(JSON, nullable=True)
    event_time = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    inbox = relationship("SendingInbox", foreign_keys=[inbox_id], back_populates="warmup_events")
    peer_inbox = relationship("SendingInbox", foreign_keys=[peer_inbox_id])

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
