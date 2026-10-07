# app/schemas/prospect_schema.py

from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID


class ProspectCreate(BaseModel):
    # =========================
    # TENANCY
    # =========================
    tenant_id: UUID

    # =========================
    # RESEARCH METADATA
    # =========================
    research_month: Optional[str] = None
    research_by: Optional[str] = None
    source_sheet: Optional[str] = None
    disposition: Optional[str] = None

    # =========================
    # CORE IDENTITY
    # =========================
    first_name: str
    last_name: str
    designation: Optional[str] = None
    company_name: str

    # =========================
    # EMAIL
    # =========================
    email: EmailStr
    email_domain: Optional[str] = None
    is_valid_email: bool = True
    is_business_email: bool

    # =========================
    # CONTACT / ENRICHMENT
    # =========================
    linkedin_url: Optional[str] = None
    poc_city: Optional[str] = None
    poc_state: Optional[str] = None
    emp_band: Optional[str] = None
    industry: Optional[str] = None

    # =========================
    # COMPLIANCE
    # =========================
    consent_status: str = "UNKNOWN"
    domain_confidence: str = "UNKNOWN"
    is_anonymized: bool = False

    class Config:
        extra = "forbid"
