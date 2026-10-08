# app/schemas/template_schema.py
"""
Pydantic schemas for Email Template API.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime
from enum import Enum


class TemplateTone(str, Enum):
    """Email tone options."""
    PROFESSIONAL = "professional"
    CASUAL = "casual"
    FORMAL = "formal"
    FRIENDLY = "friendly"


# ===========================
# REQUEST SCHEMAS
# ===========================

class EmailTemplateCreate(BaseModel):
    """Schema for creating an email template."""
    
    campaign_id: str = Field(..., description="Campaign this template belongs to")
    sequence_id: Optional[str] = Field(None, description="Sequence step this template is for")
    
    subject: str = Field(..., min_length=1, max_length=500, description="Email subject line")
    body: str = Field(..., min_length=1, description="Email body content")
    
    # Optional metadata
    designation: Optional[str] = Field(None, max_length=150, description="Target designation for personalization")
    tone: Optional[TemplateTone] = Field(None, description="Email tone")
    cta_link: Optional[str] = Field(None, description="Call-to-action link")
    
    # AI generation flag
    is_ai_generated: bool = Field(False, description="Whether content was AI-generated")
    ai_prompt_id: Optional[str] = Field(None, description="AI prompt used for generation")
    
    @field_validator("subject", "body")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()
    
    class Config:
        json_schema_extra = {
            "example": {
                "campaign_id": "uuid-here",
                "subject": "Question about {{company_name}}",
                "body": "Hi {{first_name}},\n\nI noticed {{company_name}} is growing...",
                "tone": "professional",
                "is_ai_generated": False
            }
        }


class EmailTemplateUpdate(BaseModel):
    """Schema for updating an email template."""
    
    subject: Optional[str] = Field(None, min_length=1, max_length=500)
    body: Optional[str] = Field(None, min_length=1)
    designation: Optional[str] = Field(None, max_length=150)
    tone: Optional[TemplateTone] = None
    cta_link: Optional[str] = None


class EmailTemplateApprove(BaseModel):
    """Schema for approving a template."""
    
    approved_by: Optional[str] = Field(None, description="Ignored: the approver is always the signed-in user")
    
    class Config:
        json_schema_extra = {
            "example": {
                "approved_by": "user-uuid-here"
            }
        }


# ===========================
# RESPONSE SCHEMAS
# ===========================

class EmailAttachmentResponse(BaseModel):
    """Attachment metadata attached to a template."""

    attachment_id: str
    template_id: str
    filename: str
    content_type: Optional[str] = None
    size_bytes: int
    created_at: datetime

    class Config:
        from_attributes = True


class EmailTemplateResponse(BaseModel):
    """Full template response."""

    template_id: str
    campaign_id: str
    sequence_id: Optional[str] = None

    subject: str
    body: str

    designation: Optional[str] = None
    tone: Optional[TemplateTone] = None
    cta_link: Optional[str] = None

    is_ai_generated: bool = False
    ai_model: Optional[str] = None
    ai_prompt_id: Optional[str] = None
    content_fingerprint: Optional[str] = None

    # Approval info
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    is_approved: bool = False

    created_at: datetime

    # Version info
    version_count: int = 0

    # Attachments
    attachments: List[EmailAttachmentResponse] = []

    class Config:
        from_attributes = True


class EmailTemplateListItem(BaseModel):
    """Simplified template for list display."""
    
    template_id: str
    subject: str
    is_ai_generated: bool
    is_approved: bool
    step_number: Optional[int] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class TemplateVersionResponse(BaseModel):
    """Template version history item."""
    
    version_id: str
    template_id: str
    subject: str
    body: str
    version_number: int
    created_by: str
    created_at: datetime
    is_active: bool
    
    class Config:
        from_attributes = True
