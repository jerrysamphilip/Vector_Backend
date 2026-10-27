# app/models/contact_activity.py
"""
Activities logged against a single contact: notes, calls and meetings.
Emails are not stored here; the contact timeline reads them from email_messages.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

ACTIVITY_TYPES = ("NOTE", "CALL", "MEETING", "EMAIL")


class ContactActivity(Base):
    __tablename__ = "contact_activities"

    activity_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("prospects.prospect_id"), nullable=True, index=True)
    # Activity logged on a deal (BR-SF-06); prospect_id is then the deal's contact, if it has one
    opportunity_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("opportunities.opportunity_id"), nullable=True, index=True)
    # Where it came from: MANUAL, or GMAIL / OUTLOOK / GOOGLE_CALENDAR / OUTLOOK_CALENDAR for synced items
    # (BR-SF-16); external_id de-duplicates synced items
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

    activity_type: Mapped[str] = mapped_column(String(20), nullable=False, default="NOTE")  # NOTE / CALL / MEETING / EMAIL (logged by hand)
    subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # e.g. Connected, Left voicemail, No answer
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, server_default=NOW6)

    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)

    prospect = relationship("Prospect", back_populates="activities")
    author = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<ContactActivity {self.activity_type} prospect={self.prospect_id}>"
