# app/models/contact_activity.py
"""
Activities logged against a single contact: notes, calls and meetings.
Emails are not stored here; the contact timeline reads them from email_messages.
"""

from sqlalchemy import Column, String, Text, Integer, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base

ACTIVITY_TYPES = ("NOTE", "CALL", "MEETING", "EMAIL")


class ContactActivity(Base):
    __tablename__ = "contact_activities"

    activity_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=False, index=True)

    activity_type = Column(String(20), nullable=False, default="NOTE")  # NOTE / CALL / MEETING / EMAIL (logged by hand)
    subject = Column(String(255), nullable=True)
    body = Column(Text, nullable=True)
    outcome = Column(String(100), nullable=True)  # e.g. Connected, Left voicemail, No answer
    duration_minutes = Column(Integer, nullable=True)
    occurred_at = Column(TIMESTAMP, nullable=False, server_default=func.now())

    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    prospect = relationship("Prospect", back_populates="activities")
    author = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<ContactActivity {self.activity_type} prospect={self.prospect_id}>"
