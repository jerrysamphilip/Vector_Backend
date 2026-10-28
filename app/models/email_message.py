# app/models/email_message.py
"""
Email Message and Event models for the SMTP sequencing engine.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Computed, Float, ForeignKey, Index, Integer, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.status_checks import status_check
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id, short_id

# One email per campaign step per contact (item 19): non-NULL only for campaign OUTBOUND step mail
# that is not CANCELLED, so inbound rows, manual replies, automation sends (no step) and cancelled
# duplicates are never constrained (a UNIQUE index ignores NULLs). Literal copy in
# alembic/versions/0020_send_integrity_keys.py: keep the two identical.
STEP_KEY_SQL = (
    "CASE WHEN COALESCE(direction, 'OUTBOUND') = 'OUTBOUND' AND campaign_id IS NOT NULL "
    "AND sequence_id IS NOT NULL AND COALESCE(status, '') <> 'CANCELLED' "
    "THEN CONCAT(campaign_id, ':', prospect_id, ':', sequence_id) END"
)


class EmailMessage(Base):
    """
    Individual email message in the sending queue.
    Tracks scheduling, retries, and delivery status.
    """
    __tablename__ = "email_messages"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False, default=new_id)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=True)
    prospect_id: Mapped[str] = mapped_column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    # The contact's tenant (D3, 0028_child_tenant_ids); filled from the prospect by app/models/tenant_fill.py
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("email_sequences.sequence_id"), nullable=True)
    inbox_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("sending_inboxes.inbox_id"), nullable=True)
    template_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("email_templates.template_id"), nullable=True)  # Link to AI template for performance tracking

    # Scheduling
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    # Set once the provider (SES) confirms the message was delivered to the
    # recipient's mail server. NULL means "sent, delivery not yet confirmed" —
    # kept separate from `status` since most reporting queries treat
    # status == "SENT" as the successful-send bucket.
    delivered_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # Status
    direction: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default="OUTBOUND")  # INBOUND / OUTBOUND
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="QUEUED")  # QUEUED / SENT / DELIVERED / BOUNCED / COMPLAINED / REJECTED / FAILED / CANCELLED
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Content (Snapshot)
    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    from_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # A/B subject test (BR-SF-18): the variant label (A-D) whose subject this
    # message carries; assigned once at pre-schedule time and kept on re-plan.
    subject_variant: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)

    # Retry Logic
    retry_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    max_retries: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=3)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    # Re-queues for a platform / sender-identity failure (item 14): not the message's fault, so
    # they do not spend retry_count; capped by send_breaker.MAX_DEFERRED_ATTEMPTS.
    deferred_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Send safety (BR-DF-06): set when the scheduler claims a campaign step for a
    # recipient, "<campaign>:<step>:<email>". The unique index means the same
    # step can never be claimed twice for the same address, even concurrently.
    send_key: Mapped[Optional[str]] = mapped_column(String(330), nullable=True, unique=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    # Generated (VIRTUAL) key behind ux_email_messages_step_key; never written by the application
    step_key: Mapped[Optional[str]] = mapped_column(String(110), Computed(STEP_KEY_SQL, persisted=False), nullable=True)

    # Delivery outcome (BR-DF-04). SES's own id, for matching events that arrive
    # without our message_id tag, and the final status every sent email gets
    # within 15 minutes: DELIVERED / BOUNCED / COMPLAINED / REJECTED / FAILED /
    # UNCONFIRMED (SES never reported back).
    ses_message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    final_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    final_status_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # Reply classification (S-09), INBOUND rows only: POSITIVE / NEUTRAL / NEGATIVE / OUT_OF_OFFICE /
    # UNSUBSCRIBE with a confidence (0-1) and where it came from (HEADER / RULES / LLM). NULL
    # reply_classified_at = not classified yet; the reply_classification job retries those (at most
    # app.domain.inbox.MAX_CLASSIFY_ATTEMPTS attempts). A low-confidence UNSUBSCRIBE is flagged for
    # review (reply_needs_review) instead of suppressing the contact. Migration 0023_reply_classification.
    reply_label: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reply_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reply_label_source: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    reply_classified_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    reply_classify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reply_needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

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
        # Per-inbox send history / daily counts (item 19)
        Index("ix_email_messages_inbox_sent", "inbox_id", "sent_at"),
        Index("ux_email_messages_step_key", "step_key", unique=True),
        # Replies still to classify (reply_classification job)
        Index("ix_email_messages_reply_pending", "direction", "reply_classified_at", "sent_at"),
        # Tenant-wide send history / reports / inbox (D3)
        Index("ix_email_messages_tenant_sent", "tenant_id", "direction", "sent_at"),
        Index("ix_email_messages_tenant_prospect", "tenant_id", "prospect_id"),
        status_check("email_messages", "status"),  # D5, 0029_status_checks
        status_check("email_messages", "final_status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<EmailMessage {short_id(self.message_id)}... ({self.status})>"


class EmailEvent(Base):
    """
    Email engagement event (open, click, reply, bounce, etc).
    """
    __tablename__ = "email_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False, default=new_id)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("email_messages.message_id"), nullable=False)
    # The message's tenant (D3, 0028_child_tenant_ids); filled from the message by app/models/tenant_fill.py
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Event Type: 1=SENT, 2=OPEN, 3=CLICK, 4=REPLY, 5=BOUNCE, 6=UNSUBSCRIBE
    event_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event_time: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    event_metadata: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)  # Renamed from 'metadata' (reserved)

    # Relationships
    message = relationship("EmailMessage", back_populates="events")

    __table_args__ = (
        Index("ix_email_events_tenant_type_time", "tenant_id", "event_type", "event_time"),
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

    def __repr__(self) -> str:
        return f"<EmailEvent type={self.event_type}>"
