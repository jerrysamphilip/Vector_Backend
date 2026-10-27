# app/models/conversation.py
"""
Conversation model for grouping email messages between a prospect and an inbox.
"""

from sqlalchemy import Column, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship
import uuid

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class Conversation(Base):
    """
    Groups messages between a Prospect and a Sending Inbox.
    Used for the Unified Inbox view.
    """
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    inbox_id = Column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=False)
    
    subject = Column(String(255), nullable=True)
    status = Column(String(50), default="OPEN")  # OPEN / CLOSED / SNOOZED
    is_unread = Column(Boolean, default=True)
    
    last_message_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    # Relationships
    tenant = relationship("Tenant")
    prospect = relationship("Prospect")
    inbox = relationship("SendingInbox")
    # Chronological; on a sent_at tie (second precision) the outbound message
    # comes before the reply to it ('OUTBOUND' > 'INBOUND').
    messages = relationship(
        "EmailMessage", back_populates="conversation",
        order_by="[EmailMessage.sent_at, EmailMessage.direction.desc()]",
    )

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id[:8]}... status={self.status}>"
