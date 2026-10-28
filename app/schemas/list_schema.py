"""
Request / response models of the lists aggregate (/lists, /prospect-lists, list enrollment, uploads),
moved out of app/routers/lists_router.py and app/routers/prospect_list_router.py unchanged (chunk A1).
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


# ── /lists ─────────────────────────────────────────────────────────


class ListWrite(BaseModel):
    list_name: Optional[str] = Field(None, max_length=255)
    list_type: Optional[str] = None  # STATIC / ACTIVE (create only)
    filters: Optional[Any] = None
    description: Optional[str] = Field(None, max_length=2000)


class Members(BaseModel):
    prospect_ids: List[str] = Field(..., min_length=1, max_length=10000)


class FilterPreview(BaseModel):
    filters: Any = None


# ── /prospect-lists ────────────────────────────────────────────────


class ProspectListResponse(BaseModel):
    list_id: str
    list_name: str
    source_type: str
    prospect_count: int
    uploaded_at: Optional[datetime] = None  # S10: column is nullable

    class Config:
        from_attributes = True


class ProspectListPaginatedResponse(BaseModel):
    items: List[ProspectListResponse]
    total: int
    page: int
    page_size: int


class EnrollmentRequest(BaseModel):
    campaign_id: str
    list_ids: List[str]


class EnrollmentResult(BaseModel):
    total_checked: int
    enrolled: int
    rejected: dict
    rejections: List[dict] = []  # one entry per contact not enrolled, with the reason


class ProspectInListResponse(BaseModel):
    prospect_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str
    company_name: Optional[str] = None
    designation: Optional[str] = None
    poc_city: Optional[str] = None
    poc_state: Optional[str] = None
    poc_country: Optional[str] = None
    consent_status: Optional[str] = None  # S10: column is nullable
    notes: Optional[str] = None
    row_version: Optional[int] = None  # send back as expected_version on PATCH /prospects/{id} (T-3)

    class Config:
        from_attributes = True


class ProspectListDetailResponse(BaseModel):
    list_id: str
    list_name: str
    prospects: List[ProspectInListResponse]
    total: int
    page: int
    page_size: int


class ProspectNoteUpdate(BaseModel):
    notes: Optional[str] = None


class ProspectUpdateRequest(BaseModel):
    # Same column limits as the contact (prospects table), so an over-long value is a 422
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    email: Optional[EmailStr] = Field(None, max_length=255)
    company_name: Optional[str] = Field(None, max_length=255)
    designation: Optional[str] = Field(None, max_length=150)
    poc_city: Optional[str] = Field(None, max_length=100)
    poc_state: Optional[str] = Field(None, max_length=100)
    poc_country: Optional[str] = Field(None, max_length=100)
    expected_version: Optional[int] = Field(None, ge=1)  # the contact's row_version the client loaded (T-3)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email_unchanged(cls, value):
        # Edit forms send the whole row; a blank email means "leave it", never "clear it"
        if isinstance(value, str):
            value = value.strip().lower()
        return value or None


class RevalidationResultItem(BaseModel):
    prospect_id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    status: str  # "valid", "invalid", "warning"
    issues: List[str] = []


class RevalidationResponse(BaseModel):
    list_id: str
    list_name: str
    total_checked: int
    valid_count: int
    invalid_count: int
    warning_count: int
    updated_count: int = 0  # Number of prospects with metadata updated
    results: List[RevalidationResultItem]


class ProspectStatsResponse(BaseModel):
    total: int
    available: int
    active: int
    opted_out: int


class UploadRevalidateRequest(BaseModel):
    records: list[dict]


class UploadConfirmRequest(BaseModel):
    # The id from the validation step; it becomes the list id, so confirming twice is idempotent
    upload_id: str = Field(..., min_length=1, max_length=36)
    title: str = Field(..., max_length=255)
    records: list[dict]
