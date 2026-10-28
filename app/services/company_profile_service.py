"""
Company profiles and the tenant's sender identity (CAN-SPAM company name + postal address).

One method per route of app/routers/company_profile_router.py. Scoped to the current user's tenant;
legacy rows with NULL tenant_id are hidden from every tenant (kept, not deleted). Every change drops
the cached sender identity. No sqlalchemy / fastapi imports: SQL lives in
app/repositories/company_profiles.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app.core.errors import BadRequestError, NotFoundError
from app.models.company_profile import CompanyProfile
from app.repositories.company_profiles import (
    CompanyProfilesUnitOfWork, Session, SqlAlchemyCompanyProfilesUnitOfWork,
)
from app.services.sender_identity import get_sender_identity, invalidate as invalidate_sender_identity

__all__ = ["CompanyProfileService", "SenderIdentitySettings"]

_NOT_FOUND = "Company profile not found"
_UPDATABLE = ("profile_name", "company_name", "company_description", "default_sender_name", "default_cta_link")


@dataclass(frozen=True)
class SenderIdentitySettings:
    company_name: Optional[str]
    postal_address: Optional[str]
    profile_id: Optional[str]
    is_complete: bool


class CompanyProfileService:
    def __init__(self, uow: CompanyProfilesUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> CompanyProfileService:
        return cls(SqlAlchemyCompanyProfilesUnitOfWork.for_session(db))

    def list(self, user: Any) -> list[CompanyProfile]:
        with self.uow as uow:
            return uow.profiles.list(user.tenant_id)

    def get(self, profile_id: str, user: Any) -> CompanyProfile:
        with self.uow as uow:
            profile = uow.profiles.get(user.tenant_id, profile_id)
            if not profile:
                raise NotFoundError(_NOT_FOUND)
            return profile

    def sender_identity(self, user: Any) -> SenderIdentitySettings:
        with self.uow as uow:
            profile = uow.profiles.default(user.tenant_id)
            identity = get_sender_identity(user.tenant_id, uow.session, fresh=True)
            return SenderIdentitySettings(
                company_name=identity.company_name,
                postal_address=identity.postal_address,
                profile_id=profile.profile_id if profile else None,
                is_complete=bool(identity.company_name and identity.postal_address),
            )

    def set_sender_identity(self, company_name: str, postal_address: str, user: Any) -> SenderIdentitySettings:
        """Store the tenant's sender name and address on its default profile (created when missing)."""
        name, address = company_name.strip(), postal_address.strip()
        if not name or len(address) < 5:
            raise BadRequestError("Company name and a full postal address are required")
        with self.uow as uow:
            profile = uow.profiles.default(user.tenant_id)
            if not profile:
                profile = uow.profiles.add(CompanyProfile(
                    profile_id=str(uuid.uuid4()), tenant_id=user.tenant_id, profile_name="Default",
                    company_name=name, is_default=True,
                ))
            elif not profile.is_default:
                uow.profiles.clear_default(user.tenant_id)
                profile.is_default = True
            profile.company_name = name
            profile.postal_address = address
            uow.commit()
            invalidate_sender_identity(user.tenant_id)
            return SenderIdentitySettings(company_name=name, postal_address=address,
                                          profile_id=profile.profile_id, is_complete=True)

    def create(self, data: Any, user: Any) -> CompanyProfile:
        with self.uow as uow:
            if data.is_default:
                uow.profiles.clear_default(user.tenant_id)
            profile = uow.profiles.add(CompanyProfile(
                profile_id=str(uuid.uuid4()),
                tenant_id=user.tenant_id,
                profile_name=data.profile_name,
                company_name=data.company_name,
                company_description=data.company_description,
                default_sender_name=data.default_sender_name,
                default_cta_link=data.default_cta_link,
                postal_address=(data.postal_address or "").strip() or None,
                is_default=data.is_default,
            ))
            uow.commit()
            uow.profiles.refresh(profile)
            invalidate_sender_identity(user.tenant_id)
            return profile

    def update(self, profile_id: str, data: Any, user: Any) -> CompanyProfile:
        with self.uow as uow:
            profile = uow.profiles.get(user.tenant_id, profile_id)
            if not profile:
                raise NotFoundError(_NOT_FOUND)
            if data.is_default:
                uow.profiles.clear_default(user.tenant_id, keep_profile_id=profile_id)
            for field in _UPDATABLE:
                value = getattr(data, field)
                if value is not None:
                    setattr(profile, field, value)
            if data.postal_address is not None:
                profile.postal_address = data.postal_address.strip() or None
            if data.is_default is not None:
                profile.is_default = data.is_default
            uow.commit()
            uow.profiles.refresh(profile)
            invalidate_sender_identity(user.tenant_id)
            return profile

    def delete(self, profile_id: str, user: Any) -> dict:
        with self.uow as uow:
            profile = uow.profiles.get(user.tenant_id, profile_id)
            if not profile:
                raise NotFoundError(_NOT_FOUND)
            uow.profiles.delete(profile)
            uow.commit()
            invalidate_sender_identity(user.tenant_id)
            return {"message": "Company profile deleted", "profile_id": profile_id}
