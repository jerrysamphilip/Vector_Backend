"""
Request models of the campaign wizard API (app/routers/campaign_wizard_router.py). Moved out of the router
unchanged (chunk A2); the OpenAPI schema is the same.
"""

from typing import List, Optional

from pydantic import BaseModel, Field

class CreateCampaignRequest(BaseModel):
    campaign_name: str
    send_start_hour: int = Field(9, ge=0, le=23)
    send_end_hour: int = Field(17, ge=0, le=23)
    timezone: str = "Asia/Kolkata"
    cta_link: Optional[str] = None
    inbox_ids: List[str] = []

class ProcessListRequest(BaseModel):
    """Request to auto-classify prospects and generate templates."""
    campaign_id: str
    list_id: str
    product_name: str = "your solution"
    company_name: str = "your company"
    campaign_description: str = ""  # Description for LLM context
    sequence_steps: Optional[List[dict]] = None  # User-configured sequence timing
    include_first_name_in_subject: bool = False  # Include {{first_name}} in subject
    creative_email: bool = False  # Apply creative writing style instructions
    excluded_prospect_ids: Optional[List[str]] = None  # Contacts deselected in the wizard

class GeneratePersonaTemplateRequest(BaseModel):
    """Request to generate (or on-demand regenerate) a single persona's template."""
    list_id: str
    product_name: str = "your solution"
    company_name: str = "your company"
    campaign_description: str = ""
    sequence_steps: Optional[List[dict]] = None
    excluded_prospect_ids: Optional[List[str]] = None
    include_first_name_in_subject: bool = False
    creative_email: bool = False

class SegmentationRequest(BaseModel):
    list_id: str
    persona_types: Optional[List[str]] = None
    email_types: Optional[List[str]] = ["BUSINESS"]
    industries: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    min_confidence: float = 0.0

class EnrollRequest(BaseModel):
    campaign_id: str
    list_id: str
    persona_types: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    cool_off_days: int = Field(7, ge=0, le=3650)
    excluded_prospect_ids: Optional[List[str]] = None

class ActivateRequest(BaseModel):
    campaign_id: str
    expected_version: Optional[int] = Field(None, ge=1)  # the campaign's row_version the wizard loaded (T-3)

class WizardRequest(BaseModel):
    campaign_name: str
    list_id: str
    persona_types: Optional[List[str]] = None
    exclude_personal_emails: bool = True
    cool_off_days: int = Field(7, ge=0, le=3650)

class SequenceStepInput(BaseModel):
    """Single sequence step for schedule preview."""
    step_number: int
    wait_days: int

class SaveTemplateRequest(BaseModel):
    """Request to manually update a template's subject or body."""
    template_id: str
    subject: Optional[str] = None
    body: Optional[str] = None

class SchedulePreviewRequest(BaseModel):
    """Request to preview schedule dates for sequence steps."""
    sequence_steps: List[SequenceStepInput]
    state: Optional[str] = "NY"  # US state code for timezone (default: New York)
