# app/models/email_sequence.py
"""
Email Sequence model for multi-step campaign flows.
"""

from sqlalchemy import Column, String, Boolean, Integer, ForeignKey, CheckConstraint
from sqlalchemy.orm import relationship
import uuid

from app.models.base import Base


class EmailSequence(Base):
    """
    A step in an email campaign sequence (up to 7 steps).
    Defines wait times and stop conditions.
    """
    __tablename__ = "email_sequences"

    sequence_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)

    step_number = Column(Integer, nullable=False)
    wait_days = Column(Integer, default=0)

    # Stop Conditions
    stop_on_reply = Column(Boolean, default=True)
    stop_on_bounce = Column(Boolean, default=True)

    # Send Window (hour of day, 0-23)
    send_start_hour = Column(Integer, nullable=True)
    send_end_hour = Column(Integer, nullable=True)

    # Relationships
    campaign = relationship("Campaign", back_populates="sequences")
    templates = relationship("EmailTemplate", back_populates="sequence", lazy="dynamic")
    messages = relationship("EmailMessage", back_populates="sequence", lazy="dynamic")

    __table_args__ = (
        CheckConstraint("step_number BETWEEN 1 AND 7", name="ck_sequence_step_range"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<EmailSequence step={self.step_number} wait={self.wait_days}d>"
