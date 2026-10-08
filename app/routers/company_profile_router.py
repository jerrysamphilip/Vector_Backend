# app/routers/company_profile_router.py
"""
API routes for Company Profiles.
Scoped to the current user's tenant. Legacy rows with NULL tenant_id are
hidden from every tenant (kept, not deleted).
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
import uuid

from app.core.database import get_db
from app.core.auth import require_role
from app.models.company_profile import CompanyProfile
from app.models.user import User

router = APIRouter(prefix="/api/company-profiles", tags=["Company Profiles"])


# ---------- Pydantic Schemas ----------

class CompanyProfileCreate(BaseModel):
    profile_name: str
    company_name: str
    company_description: Optional[str] = None
    default_sender_name: Optional[str] = None
    default_cta_link: Optional[str] = None
    is_default: bool = False

class CompanyProfileUpdate(BaseModel):
    profile_name: Optional[str] = None
    company_name: Optional[str] = None
    company_description: Optional[str] = None
    default_sender_name: Optional[str] = None
    default_cta_link: Optional[str] = None
    is_default: Optional[bool] = None

class CompanyProfileResponse(BaseModel):
    profile_id: str
    profile_name: str
    company_name: str
    company_description: Optional[str]
    default_sender_name: Optional[str]
    default_cta_link: Optional[str]
    is_default: bool
    
    class Config:
        from_attributes = True


# ---------- Routes ----------

@router.get("", response_model=List[CompanyProfileResponse])
def list_company_profiles(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List all company profiles, default first."""
    profiles = db.query(CompanyProfile).filter(
        CompanyProfile.tenant_id == current_user.tenant_id
    ).order_by(
        CompanyProfile.is_default.desc(),
        CompanyProfile.profile_name
    ).all()
    return profiles


@router.get("/{profile_id}", response_model=CompanyProfileResponse)
def get_company_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get a single company profile."""
    profile = db.query(CompanyProfile).filter(
        CompanyProfile.profile_id == profile_id,
        CompanyProfile.tenant_id == current_user.tenant_id
    ).first()
    
    if not profile:
        raise HTTPException(status_code=404, detail="Company profile not found")
    
    return profile


@router.post("", response_model=CompanyProfileResponse)
def create_company_profile(
    data: CompanyProfileCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Create a new company profile."""
    # If this is set as default, unset other defaults
    if data.is_default:
        db.query(CompanyProfile).filter(
            CompanyProfile.tenant_id == current_user.tenant_id,
            CompanyProfile.is_default == True
        ).update({"is_default": False})
    
    profile = CompanyProfile(
        profile_id=str(uuid.uuid4()),
        tenant_id=current_user.tenant_id,
        profile_name=data.profile_name,
        company_name=data.company_name,
        company_description=data.company_description,
        default_sender_name=data.default_sender_name,
        default_cta_link=data.default_cta_link,
        is_default=data.is_default,
    )
    
    db.add(profile)
    db.commit()
    db.refresh(profile)
    
    return profile


@router.put("/{profile_id}", response_model=CompanyProfileResponse)
def update_company_profile(
    profile_id: str,
    data: CompanyProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Update a company profile."""
    profile = db.query(CompanyProfile).filter(
        CompanyProfile.profile_id == profile_id,
        CompanyProfile.tenant_id == current_user.tenant_id
    ).first()
    
    if not profile:
        raise HTTPException(status_code=404, detail="Company profile not found")
    
    # If setting as default, unset other defaults
    if data.is_default:
        db.query(CompanyProfile).filter(
            CompanyProfile.tenant_id == current_user.tenant_id,
            CompanyProfile.is_default == True,
            CompanyProfile.profile_id != profile_id
        ).update({"is_default": False})
    
    # Update fields
    if data.profile_name is not None:
        profile.profile_name = data.profile_name
    if data.company_name is not None:
        profile.company_name = data.company_name
    if data.company_description is not None:
        profile.company_description = data.company_description
    if data.default_sender_name is not None:
        profile.default_sender_name = data.default_sender_name
    if data.default_cta_link is not None:
        profile.default_cta_link = data.default_cta_link
    if data.is_default is not None:
        profile.is_default = data.is_default
    
    db.commit()
    db.refresh(profile)
    
    return profile


@router.delete("/{profile_id}")
def delete_company_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Delete a company profile."""
    profile = db.query(CompanyProfile).filter(
        CompanyProfile.profile_id == profile_id,
        CompanyProfile.tenant_id == current_user.tenant_id
    ).first()
    
    if not profile:
        raise HTTPException(status_code=404, detail="Company profile not found")
    
    db.delete(profile)
    db.commit()
    
    return {"message": "Company profile deleted", "profile_id": profile_id}
