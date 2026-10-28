# app/routers/company_profile_router.py
"""
API routes for Company Profiles.
Scoped to the current user's tenant. Legacy rows with NULL tenant_id are
hidden from every tenant (kept, not deleted).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from typing import Any, Optional, List

from app.core.database import get_db
from app.core.auth import require_role
from app.services.company_profile_service import CompanyProfileService

router = APIRouter(prefix="/api/company-profiles", tags=["Company Profiles"])


def company_profile_service(db=Depends(get_db)) -> CompanyProfileService:
    return CompanyProfileService.for_session(db)


Service = Depends(company_profile_service)


# ---------- Pydantic Schemas ----------

class CompanyProfileCreate(BaseModel):
    profile_name: str
    company_name: str
    company_description: Optional[str] = None
    default_sender_name: Optional[str] = None
    default_cta_link: Optional[str] = None
    postal_address: Optional[str] = None
    is_default: bool = False

class CompanyProfileUpdate(BaseModel):
    profile_name: Optional[str] = None
    company_name: Optional[str] = None
    company_description: Optional[str] = None
    default_sender_name: Optional[str] = None
    default_cta_link: Optional[str] = None
    postal_address: Optional[str] = None
    is_default: Optional[bool] = None

class CompanyProfileResponse(BaseModel):
    profile_id: str
    profile_name: str
    company_name: str
    company_description: Optional[str]
    default_sender_name: Optional[str]
    default_cta_link: Optional[str]
    postal_address: Optional[str] = None
    is_default: Optional[bool] = None  # S10: column is nullable
    
    class Config:
        from_attributes = True


# ---------- Routes ----------

@router.get("", response_model=List[CompanyProfileResponse])
def list_company_profiles(
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List all company profiles, default first."""
    return service.list(current_user)


# ---------- Sender identity (tenant-level, admin only) ----------
# The company name + physical postal address every campaign email of this
# tenant is sent under (CAN-SPAM). Stored on the tenant's default company
# profile. Declared before "/{profile_id}" so the path is not taken as an id.

class SenderIdentityUpdate(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    postal_address: str = Field(..., min_length=5, max_length=1000)


class SenderIdentityResponse(BaseModel):
    company_name: Optional[str]
    postal_address: Optional[str]
    profile_id: Optional[str]
    is_complete: bool


@router.get("/sender-identity", response_model=SenderIdentityResponse)
def get_sender_identity_settings(
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """The tenant's sender company name and postal address (shown in every email footer)."""
    return SenderIdentityResponse(**vars(service.sender_identity(current_user)))


@router.put("/sender-identity", response_model=SenderIdentityResponse)
def update_sender_identity_settings(
    data: SenderIdentityUpdate,
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Set the tenant's sender company name and postal address (creates the default profile if needed)."""
    return SenderIdentityResponse(**vars(service.set_sender_identity(data.company_name, data.postal_address,
                                                                     current_user)))


@router.get("/{profile_id}", response_model=CompanyProfileResponse)
def get_company_profile(
    profile_id: str,
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get a single company profile."""
    return service.get(profile_id, current_user)


@router.post("", response_model=CompanyProfileResponse)
def create_company_profile(
    data: CompanyProfileCreate,
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Create a new company profile."""
    return service.create(data, current_user)


@router.put("/{profile_id}", response_model=CompanyProfileResponse)
def update_company_profile(
    profile_id: str,
    data: CompanyProfileUpdate,
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Update a company profile."""
    return service.update(profile_id, data, current_user)


@router.delete("/{profile_id}")
def delete_company_profile(
    profile_id: str,
    service: CompanyProfileService = Service,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Delete a company profile."""
    return service.delete(profile_id, current_user)
