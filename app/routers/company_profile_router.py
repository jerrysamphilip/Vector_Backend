# app/routers/company_profile_router.py
"""
API routes for Company Profiles.
Scoped to the current user's tenant. Legacy rows with NULL tenant_id are
hidden from every tenant (kept, not deleted).
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid

from app.core.database import get_db
from app.core.auth import require_role
from app.models.company_profile import CompanyProfile
from app.models.user import User
from app.services.sender_identity import invalidate as invalidate_sender_identity

router = APIRouter(prefix="/api/company-profiles", tags=["Company Profiles"])


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


def _default_profile(db: Session, tenant_id: str) -> Optional[CompanyProfile]:
    return db.query(CompanyProfile).filter(
        CompanyProfile.tenant_id == tenant_id
    ).order_by(CompanyProfile.is_default.desc(), CompanyProfile.created_at.asc()).first()


@router.get("/sender-identity", response_model=SenderIdentityResponse)
def get_sender_identity_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """The tenant's sender company name and postal address (shown in every email footer)."""
    from app.services.sender_identity import get_sender_identity
    profile = _default_profile(db, current_user.tenant_id)
    identity = get_sender_identity(current_user.tenant_id, db, fresh=True)
    return SenderIdentityResponse(
        company_name=identity.company_name,
        postal_address=identity.postal_address,
        profile_id=profile.profile_id if profile else None,
        is_complete=bool(identity.company_name and identity.postal_address),
    )


@router.put("/sender-identity", response_model=SenderIdentityResponse)
def update_sender_identity_settings(
    data: SenderIdentityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Set the tenant's sender company name and postal address (creates the default profile if needed)."""
    name = data.company_name.strip()
    address = data.postal_address.strip()
    if not name or len(address) < 5:
        raise HTTPException(status_code=400, detail="Company name and a full postal address are required")
    profile = _default_profile(db, current_user.tenant_id)
    if not profile:
        profile = CompanyProfile(
            profile_id=str(uuid.uuid4()),
            tenant_id=current_user.tenant_id,
            profile_name="Default",
            company_name=name,
            is_default=True,
        )
        db.add(profile)
    elif not profile.is_default:
        db.query(CompanyProfile).filter(
            CompanyProfile.tenant_id == current_user.tenant_id,
            CompanyProfile.is_default == True,
        ).update({"is_default": False})
        profile.is_default = True
    profile.company_name = name
    profile.postal_address = address
    db.commit()
    invalidate_sender_identity(current_user.tenant_id)
    return SenderIdentityResponse(company_name=name, postal_address=address,
                                  profile_id=profile.profile_id, is_complete=True)


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
        postal_address=(data.postal_address or "").strip() or None,
        is_default=data.is_default,
    )
    
    db.add(profile)
    db.commit()
    db.refresh(profile)
    invalidate_sender_identity(current_user.tenant_id)
    
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
    if data.postal_address is not None:
        profile.postal_address = data.postal_address.strip() or None
    if data.is_default is not None:
        profile.is_default = data.is_default
    
    db.commit()
    db.refresh(profile)
    invalidate_sender_identity(current_user.tenant_id)
    
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
    invalidate_sender_identity(current_user.tenant_id)
    
    return {"message": "Company profile deleted", "profile_id": profile_id}
