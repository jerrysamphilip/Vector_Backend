
# app/models/automation_rule.py
"""
Automation Rules model for defining triggers and actions.
"""

from sqlalchemy import Column, String, Boolean, JSON, TIMESTAMP, ForeignKey, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
import enum

from app.models.base import Base

class TriggerType(str, enum.Enum):
    EMAIL_OPENED = "EMAIL_OPENED"
    EMAIL_CLICKED = "EMAIL_CLICKED"
    NO_REPLY = "NO_REPLY"
    TIME_DELAY = "TIME_DELAY"

class ActionType(str, enum.Enum):
    SEND_EMAIL = "SEND_EMAIL"
    CHANGE_CAMPAIGN_STEP = "CHANGE_CAMPAIGN_STEP"
    PAUSE_PROSPECT = "PAUSE_PROSPECT"
    MARK_INTERESTED = "MARK_INTERESTED"

class AutomationRule(Base):
    """
    Rule definition for automated actions based on triggers.
    """
    __tablename__ = "automation_rules"

    rule_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)
    
    name = Column(String(255), nullable=False)
    description = Column(String(500), nullable=True)
    
    trigger_type = Column(String(50), nullable=False) # e.g. EMAIL_OPENED (Using String for flexibility/sqlite compat)
    trigger_config = Column(JSON, nullable=True) # e.g. {"days": 3} or {"link_url": "..."}
    
    action_type = Column(String(50), nullable=False) # e.g. SEND_EMAIL
    action_config = Column(JSON, nullable=True) # e.g. {"template_id": "...", "step_id": 2}
    
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    campaign = relationship("Campaign", back_populates="automation_rules")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<AutomationRule {self.name} ({self.trigger_type} -> {self.action_type})>"
