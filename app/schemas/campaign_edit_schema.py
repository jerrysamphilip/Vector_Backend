"""
Request / response models of editing a campaign after launch (P2 S-04 / F-04, F-05):
POST /campaigns/{id}/edits, POST /campaigns/{id}/edits/checks, POST /campaigns/{id}/email-previews and
GET /campaigns/{id}/template-assignments (app/services/campaign_edit_service.py).
"""

from __future__ import annotations

from datetime import date, time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.domain.campaigns_edit import CampaignEdit, StepEdit, TemplateEdit
from app.schemas.campaign_schema import CLEARABLE_FIELDS, CampaignResponse
from app.schemas.types import UtcDatetime


class CampaignEditSettings(BaseModel):
    """Campaign settings to change; a field left out is unchanged, null clears a clearable one."""

    campaign_name: Optional[str] = Field(None, min_length=1, max_length=255)
    sender_name: Optional[str] = Field(None, max_length=100)
    sender_title: Optional[str] = Field(None, max_length=150)
    cta_link: Optional[str] = Field(None, max_length=500)
    send_window_start: Optional[time] = None
    send_window_end: Optional[time] = None
    respect_timezone: Optional[bool] = None
    campaign_timezone: Optional[str] = Field(None, max_length=50)
    wait_business_days: Optional[bool] = None
    sending_mode: Optional[str] = Field(None, pattern="^(spread|random|batch)$")
    min_gap_minutes: Optional[int] = Field(None, ge=0, le=10080)
    batch_size: Optional[int] = Field(None, ge=1, le=100000)
    batch_gap_minutes: Optional[int] = Field(None, ge=1, le=10080)
    daily_batch_size: Optional[int] = Field(None, ge=1, le=1000000)
    unsubscribe_mode: Optional[str] = Field(None, pattern="^(plain|html)$")
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    @field_validator("campaign_name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("campaign_name must not be blank")
        return v

    def changes(self) -> Dict[str, Any]:
        """The fields sent: null only for the clearable ones (S-23); other nulls are ignored."""
        return {name: getattr(self, name) for name in self.model_fields_set
                if getattr(self, name) is not None or name in CLEARABLE_FIELDS}


class TemplateEditIn(BaseModel):
    """A persona version of a step: with template_id it is edited, without it a version is added."""

    template_id: Optional[str] = Field(None, max_length=36)
    designation: Optional[str] = Field(None, max_length=150, description="Persona; empty = the generic version")
    subject: Optional[str] = Field(None, max_length=998)
    body: Optional[str] = Field(None, max_length=100000)


class StepEditIn(BaseModel):
    """A step of the edited sequence, in order: with sequence_id it is kept, without it a step is added."""

    sequence_id: Optional[str] = Field(None, max_length=36)
    wait_days: Optional[int] = Field(None, ge=0, le=30)
    stop_on_reply: Optional[bool] = None
    stop_on_bounce: Optional[bool] = None
    templates: List[TemplateEditIn] = Field(default_factory=list, max_length=20)


class CampaignEditRequest(BaseModel):
    """
    One edit of a campaign (DRAFT, ACTIVE or PAUSED). ``steps`` (optional) is the whole step list in order:
    existing steps keep their relative order, a step left out is deleted (refused once it was sent).
    ``expected_version`` (or If-Match) is the campaign's row_version you loaded (409 when stale).
    """

    expected_version: Optional[int] = Field(None, ge=1)
    settings: Optional[CampaignEditSettings] = None
    inbox_ids: Optional[List[str]] = Field(None, max_length=50)
    steps: Optional[List[StepEditIn]] = Field(None, max_length=7)

    def to_domain(self) -> CampaignEdit:
        steps = None
        if self.steps is not None:
            steps = tuple(StepEdit(sequence_id=s.sequence_id, wait_days=s.wait_days, stop_on_reply=s.stop_on_reply,
                                   stop_on_bounce=s.stop_on_bounce,
                                   templates=tuple(TemplateEdit(template_id=t.template_id, designation=t.designation,
                                                                subject=t.subject, body=t.body) for t in s.templates))
                          for s in self.steps)
        return CampaignEdit(settings=self.settings.changes() if self.settings else {},
                            inbox_ids=tuple(self.inbox_ids) if self.inbox_ids is not None else None,
                            steps=steps, expected_version=self.expected_version)


class CampaignEditResult(BaseModel):
    """What the edit changed (the dry run: what it would change). ``unsent_emails_affected`` is the
    "N unsent emails will be updated" number."""

    dry_run: bool
    settings_changed: List[str] = []
    steps_added: List[int] = []
    steps_deleted: List[int] = []
    steps_renumbered: bool = False
    templates_changed: int = 0
    emails_rerendered: int = 0
    emails_rescheduled: int = 0
    emails_rebound: int = 0
    emails_created: int = 0
    emails_removed: int = 0
    unsent_emails_affected: int = 0
    campaign: Optional[CampaignResponse] = None


class EmailPreviewRequest(BaseModel):
    """Preview one step for one enrolled contact. Without subject / body / template_id: the email as it will be
    sent (the scheduled email when it exists, else the template launch picks for the contact's persona)."""

    prospect_id: str = Field(..., max_length=36)
    step_number: int = Field(..., ge=1, le=7)
    template_id: Optional[str] = Field(None, max_length=36, description="Preview this persona version instead")
    subject: Optional[str] = Field(None, max_length=998, description="Unsaved subject from the editor")
    body: Optional[str] = Field(None, max_length=100000, description="Unsaved body from the editor")


class EmailPreview(BaseModel):
    campaign_id: str
    prospect_id: str
    step_number: int
    sequence_id: str
    persona: Optional[str] = None
    source: str  # scheduled_email | sent_email | template | draft
    template_id: Optional[str] = None
    designation: Optional[str] = None
    message_id: Optional[str] = None
    message_status: Optional[str] = None
    scheduled_at: Optional[UtcDatetime] = None
    sent_at: Optional[UtcDatetime] = None
    from_name: Optional[str] = None
    from_email: Optional[str] = None
    to_email: str
    subject: str
    body: str
    is_html: bool
    is_internal: bool = False
    subject_variant: Optional[str] = None


class AssignedTemplate(BaseModel):
    template_id: str
    designation: Optional[str] = None
    subject: str
    body: str
    recipients: int = 0


class StepTemplates(BaseModel):
    sequence_id: str
    step_number: int
    wait_days: int = 0
    templates: List[AssignedTemplate] = []
    unassigned: int = 0


class StepAssignment(BaseModel):
    step_number: int
    sequence_id: str
    template_id: Optional[str] = None
    source: str  # email | planned
    message_status: Optional[str] = None


class ProspectAssignments(BaseModel):
    prospect_id: str
    name: str = ""
    email: Optional[str] = None
    designation: Optional[str] = None
    persona: Optional[str] = None
    enrolment_status: Optional[str] = None
    assignments: List[StepAssignment] = []


class TemplateAssignments(BaseModel):
    campaign_id: str
    steps: List[StepTemplates]
    total: int
    limit: int
    offset: int
    prospects: List[ProspectAssignments]
