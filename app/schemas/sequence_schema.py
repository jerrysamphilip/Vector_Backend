# app/schemas/sequence_schema.py
"""
Pydantic schemas for Email Sequence API.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime


# ===========================
# REQUEST SCHEMAS
# ===========================

class SequenceStepCreate(BaseModel):
    """Schema for creating a sequence step."""
    
    step_number: int = Field(..., ge=1, le=7, description="Step number (1-7)")
    wait_days: int = Field(0, ge=0, le=30, description="Days to wait before this step")
    
    # Stop conditions
    stop_on_reply: bool = Field(True, description="Stop sequence if prospect replies")
    stop_on_bounce: bool = Field(True, description="Stop sequence if email bounces")
    
    # Send window (hour of day, 0-23)
    send_start_hour: Optional[int] = Field(None, ge=0, le=23)
    send_end_hour: Optional[int] = Field(None, ge=0, le=23)
    
    # Template reference (optional at creation)
    template_id: Optional[str] = Field(None, description="Email template ID")
    
    @field_validator("step_number")
    @classmethod
    def validate_step(cls, v: int) -> int:
        if v < 1 or v > 7:
            raise ValueError("Step number must be between 1 and 7")
        return v
    
    class Config:
        json_schema_extra = {
            "example": {
                "step_number": 1,
                "wait_days": 0,
                "stop_on_reply": True,
                "stop_on_bounce": True,
                "send_start_hour": 9,
                "send_end_hour": 17
            }
        }


class SequenceStepUpdate(BaseModel):
    """Schema for updating a sequence step."""
    
    wait_days: Optional[int] = Field(None, ge=0, le=30)
    stop_on_reply: Optional[bool] = None
    stop_on_bounce: Optional[bool] = None
    send_start_hour: Optional[int] = Field(None, ge=0, le=23)
    send_end_hour: Optional[int] = Field(None, ge=0, le=23)
    template_id: Optional[str] = None


# ===========================
# RESPONSE SCHEMAS
# ===========================

class SequenceStepResponse(BaseModel):
    """Response schema for a sequence step."""
    
    sequence_id: str
    campaign_id: str
    step_number: int
    wait_days: int
    
    stop_on_reply: bool
    stop_on_bounce: bool
    
    send_start_hour: Optional[int] = None
    send_end_hour: Optional[int] = None
    
    # Template info (if attached)
    template_id: Optional[str] = None
    template_subject: Optional[str] = None
    
    class Config:
        from_attributes = True


class SequenceResponse(BaseModel):
    """Full sequence with all steps."""
    
    campaign_id: str
    steps: List[SequenceStepResponse]
    total_steps: int
    total_wait_days: int  # Sum of all wait days
    
    class Config:
        from_attributes = True
