
# app/schemas/automation_schema.py
"""
Pydantic schemas for Automation Rules.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from app.domain.automation import ActionType, TriggerType
from app.schemas.types import UtcDatetime

class AutomationRuleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    trigger_type: TriggerType
    trigger_config: Optional[Dict[str, Any]] = {}
    action_type: ActionType
    action_config: Optional[Dict[str, Any]] = {}
    is_active: bool = True

class AutomationRuleCreate(AutomationRuleBase):
    campaign_id: str

class AutomationRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    trigger_config: Optional[Dict[str, Any]] = None
    action_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None

class AutomationRuleResponse(AutomationRuleBase):
    rule_id: str
    campaign_id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime

    class Config:
        from_attributes = True
