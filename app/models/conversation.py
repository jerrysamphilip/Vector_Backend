# app/models/conversation.py
"""
Conversation model for grouping email messages between a prospect and an inbox.
"""

from sqlalchemy import Column, String, Boolean, TIMESTAMP, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


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
    
    last_message_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    tenant = relationship("Tenant")
    prospect = relationship("Prospect")
    inbox = relationship("SendingInbox")
    messages = relationship("EmailMessage", back_populates="conversation", order_by="EmailMessage.sent_at")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<Conversation {self.id[:8]}... status={self.status}>"
