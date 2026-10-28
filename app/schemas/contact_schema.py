# app/schemas/contact_schema.py
"""Request bodies of the contact management API (app/routers/contacts_router.py)."""

from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, EmailStr, Field

from app.domain.mentions import MAX_MENTIONS
from app.schemas.types import UtcDatetime


class ContactWrite(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=50)
    mobile_phone: Optional[str] = Field(None, max_length=50)
    designation: Optional[str] = None
    company_name: Optional[str] = None
    account_id: Optional[str] = None
    owner_id: Optional[str] = None
    industry: Optional[str] = None
    emp_band: Optional[str] = None
    linkedin_url: Optional[str] = None
    poc_city: Optional[str] = None
    poc_state: Optional[str] = None
    poc_country: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    lead_status: Optional[str] = None
    lead_source: Optional[str] = Field(None, max_length=100)
    legal_basis: Optional[str] = None
    consent_status: Optional[str] = None  # OPT_IN / UNSUBSCRIBED (synced with the global unsubscribe list)
    tags: Optional[List[str]] = None
    custom_fields: Optional[Dict[str, Any]] = None


class ContactCreate(ContactWrite):
    email: EmailStr
    list_id: Optional[str] = None


class ContactUpdate(ContactWrite):
    email: Optional[EmailStr] = None
    # The row_version the client loaded (T-3, app/core/concurrency.py); stale -> 409 STALE_VERSION
    expected_version: Optional[int] = Field(None, ge=1)


UserId = Annotated[str, Field(min_length=1, max_length=36)]


class ActivityWrite(BaseModel):
    activity_type: str = "NOTE"
    subject: Optional[str] = Field(None, max_length=255)
    body: Optional[str] = None
    outcome: Optional[str] = Field(None, max_length=100)
    duration_minutes: Optional[int] = Field(None, ge=0, le=24 * 60)
    occurred_at: Optional[UtcDatetime] = None
    # Colleagues @mentioned in a note (BR-CM-16); the body keeps the "@First Last" text
    mentioned_user_ids: Optional[List[UserId]] = Field(None, max_length=MAX_MENTIONS)


class ActivityUpdate(BaseModel):
    subject: Optional[str] = Field(None, max_length=255)
    body: Optional[str] = None
    outcome: Optional[str] = Field(None, max_length=100)
    duration_minutes: Optional[int] = Field(None, ge=0, le=24 * 60)
    occurred_at: Optional[UtcDatetime] = None
    mentioned_user_ids: Optional[List[UserId]] = Field(None, max_length=MAX_MENTIONS)


class CompanyLinkCreate(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=36)
    label: Optional[str] = Field(None, max_length=100)
    make_primary: bool = False


class CompanyLinkUpdate(BaseModel):
    label: Optional[str] = Field(None, max_length=100)
    is_primary: Optional[bool] = None  # true makes this the primary company; to demote, promote another


class BulkAction(BaseModel):
    prospect_ids: List[str] = Field(..., min_length=1, max_length=5000)
    # assign_owner / add_tags / remove_tags / set_account / set_property /
    # add_to_list / remove_from_list / enroll / delete
    action: str
    owner_id: Optional[str] = None
    tags: Optional[List[str]] = None
    account_id: Optional[str] = None
    field: Optional[str] = None
    value: Any = None
    list_id: Optional[str] = None
    new_list_name: Optional[str] = Field(None, max_length=255)
    campaign_id: Optional[str] = None


class MergeRequest(BaseModel):
    primary_id: str
    duplicate_ids: List[str] = Field(..., min_length=1, max_length=20)
    choices: Dict[str, str] = {}  # property -> prospect_id whose value to keep


class RestoreRequest(BaseModel):
    prospect_ids: List[str] = Field(..., min_length=1, max_length=5000)


class FieldOption(BaseModel):
    """A choice option (C-17): ``value`` is what records store, ``label`` what people see. Renaming a label
    keeps the value; a new option without a value takes its label as value."""
    value: Optional[str] = Field(None, max_length=100)
    label: str = Field(..., min_length=1, max_length=100)


class FieldWrite(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    # Fixed after creation (C-17): on PATCH leave it out or send the current type; creating defaults to TEXT
    field_type: Optional[str] = None
    options: Optional[List[Union[str, FieldOption]]] = None
    max_length: Optional[int] = Field(None, ge=1, le=10000)  # TEXT / LONG_TEXT only
    is_active: Optional[bool] = None  # PATCH: false deactivates, true reactivates
    sort_order: Optional[int] = None
    group_name: Optional[str] = Field(None, max_length=100)
    required: bool = False
