# app/schemas/ai_email_schema.py
"""
Pydantic schemas for AI Email Generation endpoints.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# ============================================================
# Request Schemas
# ============================================================

class ClassifyProspectRequest(BaseModel):
    """Request to classify a single prospect."""
    designation: str = Field(..., description="Job title/designation")
    company_name: Optional[str] = Field(None, description="Company name")


class ClassifyBatchRequest(BaseModel):
    """Request to classify multiple prospects."""
    prospect_ids: List[str] = Field(..., description="List of prospect IDs to classify")


class GenerateEmailRequest(BaseModel):
    """Request to generate email for a prospect."""
    prospect_id: str = Field(..., description="Prospect ID")
    product_name: Optional[str] = Field("Our Product", description="Product name for email")
    cta_link: Optional[str] = Field(None, description="CTA Link to include in email")


class CreateBlueprintRequest(BaseModel):
    """Request to create a new persona blueprint."""
    persona_type: str = Field(..., description="Persona type (PATIENT_SERVICES_HUB, MARKET_ACCESS, etc.)")
    openers: List[str] = Field(..., description="List of opener options")
    value_angles: List[str] = Field(..., description="List of value proposition options")
    ctas: List[str] = Field(..., description="List of CTA options")
    proof_points: Optional[List[str]] = Field(None, description="Social proof examples")
    tone_rules: Optional[Dict[str, Any]] = Field(None, description="Tone configuration")


# ============================================================
# Response Schemas
# ============================================================

class ClassificationResult(BaseModel):
    """Result of prospect classification."""
    prospect_id: Optional[str] = None
    persona_type: str
    confidence_score: float
    classification_method: str = "RULE_BASED"


class GeneratedEmail(BaseModel):
    """Generated email content."""
    subject: str
    body: str
    opener_used: str
    cta_used: str
    tone: str


class BlueprintResponse(BaseModel):
    """Persona blueprint response."""
    blueprint_id: str
    persona_type: str
    openers: List[str]
    value_angles: List[str]
    ctas: List[str]
    proof_points: Optional[List[str]]
    tone_rules: Optional[Dict[str, Any]]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ClassifyTestResponse(BaseModel):
    """Response for testing classification."""
    designation: str
    persona_type: str
    confidence_score: float
