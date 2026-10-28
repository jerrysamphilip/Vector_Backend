"""
Automation Rules model for defining triggers and actions.

The trigger / action vocabularies live in app/domain/automation.py (re-exported here).
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.automation import ActionType, TriggerType
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

__all__ = ["ActionType", "AutomationRule", "TriggerType"]


class AutomationRule(Base):
    """
    Rule definition for automated actions based on triggers.
    """
    __tablename__ = "automation_rules"

    rule_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. EMAIL_OPENED
    trigger_config: Mapped[Any] = mapped_column(JSON, nullable=True)  # e.g. {"days": 3} or {"link_url": "..."}

    action_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. SEND_EMAIL
    action_config: Mapped[Any] = mapped_column(JSON, nullable=True)  # e.g. {"template_id": "...", "step_id": 2}

    is_active: Mapped[Optional[bool]] = mapped_column(Boolean, default=True, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6,
                                                           nullable=True)

    # Relationships
    campaign = relationship("Campaign", back_populates="automation_rules")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<AutomationRule {self.name} ({self.trigger_type} -> {self.action_type})>"
