# app/models/email_sequence.py
"""
Email Sequence model for multi-step campaign flows.
"""

import uuid
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class EmailSequence(Base):
    """
    A step in an email campaign sequence (up to 7 steps).
    Defines wait times and stop conditions.
    """
    __tablename__ = "email_sequences"

    sequence_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)

    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    wait_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)

    # Stop Conditions
    stop_on_reply: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True)
    stop_on_bounce: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True)

    # Send Window (hour of day, 0-23)
    send_start_hour: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    send_end_hour: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # T-3, app/core/concurrency.py
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Relationships
    campaign = relationship("Campaign", back_populates="sequences")
    templates = relationship("EmailTemplate", back_populates="sequence", lazy="dynamic")
    messages = relationship("EmailMessage", back_populates="sequence", lazy="dynamic")

    __table_args__ = (
        CheckConstraint("step_number BETWEEN 1 AND 7", name="ck_sequence_step_range"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<EmailSequence step={self.step_number} wait={self.wait_days}d>"
