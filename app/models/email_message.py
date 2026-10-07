# app/models/email_message.py
"""
Email Message and Event models for the SMTP sequencing engine.
"""

from sqlalchemy import Column, String, SmallInteger, Integer, Text, TIMESTAMP, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class EmailMessage(Base):
    """
    Individual email message in the sending queue.
    Tracks scheduling, retries, and delivery status.
    """
    __tablename__ = "email_messages"

    message_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=True)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    sequence_id = Column(String(36), ForeignKey("email_sequences.sequence_id"), nullable=True)
    inbox_id = Column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=True)
    template_id = Column(String(36), ForeignKey("email_templates.template_id"), nullable=True)  # Link to AI template for performance tracking

    # Scheduling
    scheduled_at = Column(TIMESTAMP, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    # Set once the provider (SES) confirms the message was delivered to the
    # recipient's mail server. NULL means "sent, delivery not yet confirmed" —
    # kept separate from `status` since most reporting queries treat
    # status == "SENT" as the successful-send bucket.
    delivered_at = Column(TIMESTAMP, nullable=True)

    # Status
    # Status
    direction = Column(String(20), default="OUTBOUND") # INBOUND / OUTBOUND
    status = Column(String(50), default="QUEUED")  # QUEUED / SENT / DELIVERED / BOUNCED / COMPLAINED / REJECTED / FAILED / CANCELLED
    provider_message_id = Column(String(255), nullable=True)

    # Content (Snapshot)
    subject = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    to_email = Column(String(255), nullable=False)
    from_email = Column(String(255), nullable=True)

    # Retry Logic
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    next_retry_at = Column(TIMESTAMP, nullable=True)
    failure_reason = Column(Text, nullable=True)
    last_error_code = Column(String(50), nullable=True)

    # Send safety (BR-DF-06): set when the scheduler claims a campaign step for a
    # recipient, "<campaign>:<step>:<email>". The unique index means the same
    # step can never be claimed twice for the same address, even concurrently.
    send_key = Column(String(330), nullable=True, unique=True)
    claimed_at = Column(TIMESTAMP, nullable=True)

    # Delivery outcome (BR-DF-04). SES's own id, for matching events that arrive
    # without our message_id tag, and the final status every sent email gets
    # within 15 minutes: DELIVERED / BOUNCED / COMPLAINED / REJECTED / FAILED /
    # UNCONFIRMED (SES never reported back).
    ses_message_id = Column(String(100), nullable=True, index=True)
    final_status = Column(String(30), nullable=True)
    final_status_at = Column(TIMESTAMP, nullable=True)

    # Relationships
    campaign = relationship("Campaign", back_populates="messages")
    prospect = relationship("Prospect", back_populates="email_messages")
    sequence = relationship("EmailSequence", back_populates="messages")
    inbox = relationship("SendingInbox", back_populates="messages")
    conversation = relationship("Conversation", back_populates="messages")
    events = relationship("EmailEvent", back_populates="message", lazy="dynamic")

    __table_args__ = (
        Index("ix_email_messages_status_scheduled", "status", "scheduled_at"),
        Index("ix_email_messages_reconcile", "final_status", "status", "sent_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<EmailMessage {self.message_id[:8]}... ({self.status})>"


class EmailEvent(Base):
    """
    Email engagement event (open, click, reply, bounce, etc).
    """
    __tablename__ = "email_events"

    event_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id = Column(String(36), ForeignKey("email_messages.message_id"), nullable=False)
    
    # Event Type: 1=SENT, 2=OPEN, 3=CLICK, 4=REPLY, 5=BOUNCE, 6=UNSUBSCRIBE
    event_type = Column(SmallInteger, nullable=False)
    event_time = Column(TIMESTAMP, server_default=func.now())
    event_metadata = Column(JSON, nullable=True)  # Renamed from 'metadata' (reserved)

    # Relationships
    message = relationship("EmailMessage", back_populates="events")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    EVENT_SENT = 1
    EVENT_OPEN = 2
    EVENT_CLICK = 3
    EVENT_REPLY = 4
    EVENT_BOUNCE = 5
    EVENT_UNSUBSCRIBE = 6
    EVENT_SENDER_BOUNCE = 7
    EVENT_POSITIVE_REPLY = 8
    EVENT_REPLY_OOO = 9
    EVENT_DELIVERED = 10

    def __repr__(self):
        return f"<EmailEvent type={self.event_type}>"
