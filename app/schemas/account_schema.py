"""Request bodies of the companies API (app/routers/accounts_router.py, BR-CM-03/04)."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class AccountWrite(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    domain: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = Field(None, max_length=500)
    phone: Optional[str] = Field(None, max_length=50)
    industry: Optional[str] = Field(None, max_length=100)
    emp_band: Optional[str] = Field(None, max_length=50)
    street: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    owner_id: Optional[str] = None
    annual_revenue: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    # Company custom properties (C-16): {field_key: value}; a blank value removes the key
    custom_fields: Optional[Dict[str, Any]] = None


class AccountUpdate(AccountWrite):
    expected_version: Optional[int] = Field(None, ge=1)  # the row_version the client loaded (T-3)
