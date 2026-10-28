"""
Company profiles repository and unit of work (ADR 0009): the tenant's company profiles and the sender
identity read from them (CAN-SPAM company name + postal address). Tenant-scoped; legacy rows with a
NULL tenant_id match no tenant. Never commits.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.models.company_profile import CompanyProfile
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId

__all__ = [
    "CompanyProfileRepository", "CompanyProfilesUnitOfWork", "Session", "SqlAlchemyCompanyProfileRepository",
    "SqlAlchemyCompanyProfilesUnitOfWork", "company_profiles_repo",
]


class CompanyProfileRepository(Protocol):
    def list(self, tenant_id: TenantId) -> list[CompanyProfile]: ...
    def get(self, tenant_id: TenantId, profile_id: str) -> Optional[CompanyProfile]: ...
    def default(self, tenant_id: TenantId) -> Optional[CompanyProfile]: ...
    def clear_default(self, tenant_id: TenantId, keep_profile_id: Optional[str] = None) -> None: ...
    def add(self, profile: CompanyProfile) -> CompanyProfile: ...
    def delete(self, profile: CompanyProfile) -> None: ...
    def refresh(self, profile: CompanyProfile) -> None: ...
    def identity_fields(self, tenant_id: TenantId) -> Optional[tuple[Optional[str], Optional[str]]]: ...
    def tenant_name(self, tenant_id: TenantId) -> Optional[str]: ...
    def user_name_parts(self, user_id: Optional[str]) -> Optional[tuple[Optional[str], Optional[str]]]: ...


class SqlAlchemyCompanyProfileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _of(self, tenant_id: TenantId) -> Any:
        return self._session.query(CompanyProfile).filter(CompanyProfile.tenant_id == tenant_id)

    def list(self, tenant_id: TenantId) -> list[CompanyProfile]:
        """Default first, then by name."""
        return self._of(tenant_id).order_by(CompanyProfile.is_default.desc(), CompanyProfile.profile_name).all()

    def get(self, tenant_id: TenantId, profile_id: str) -> Optional[CompanyProfile]:
        return self._of(tenant_id).filter(CompanyProfile.profile_id == profile_id).first()

    def default(self, tenant_id: TenantId) -> Optional[CompanyProfile]:
        """The default profile, else the oldest one."""
        return self._of(tenant_id).order_by(CompanyProfile.is_default.desc(),
                                            CompanyProfile.created_at.asc()).first()

    def clear_default(self, tenant_id: TenantId, keep_profile_id: Optional[str] = None) -> None:
        q = self._of(tenant_id).filter(CompanyProfile.is_default == True)  # noqa: E712
        if keep_profile_id is not None:
            q = q.filter(CompanyProfile.profile_id != keep_profile_id)
        q.update({"is_default": False})

    def add(self, profile: CompanyProfile) -> CompanyProfile:
        self._session.add(profile)
        return profile

    def delete(self, profile: CompanyProfile) -> None:
        self._session.delete(profile)

    def refresh(self, profile: CompanyProfile) -> None:
        self._session.refresh(profile)

    def identity_fields(self, tenant_id: TenantId) -> Optional[tuple[Optional[str], Optional[str]]]:
        """(company_name, postal_address) of the default profile, in a savepoint so a missing column (schema
        not migrated yet) does not abort the caller's transaction."""
        with self._session.begin_nested():
            row = self._session.query(CompanyProfile.company_name, CompanyProfile.postal_address).filter(
                CompanyProfile.tenant_id == tenant_id
            ).order_by(CompanyProfile.is_default.desc(), CompanyProfile.created_at.asc()).first()
        return (row[0], row[1]) if row else None

    def tenant_name(self, tenant_id: TenantId) -> Optional[str]:
        with self._session.begin_nested():
            name: Optional[str] = self._session.query(Tenant.tenant_name).filter(
                Tenant.tenant_id == tenant_id).scalar()
        return name

    def user_name_parts(self, user_id: Optional[str]) -> Optional[tuple[Optional[str], Optional[str]]]:
        row = self._session.query(User.first_name, User.last_name).filter(User.user_id == user_id).first()
        return (row.first_name, row.last_name) if row else None


def company_profiles_repo(session: Session) -> SqlAlchemyCompanyProfileRepository:
    """For callers inside another unit of work (the sender identity read by the send path)."""
    return SqlAlchemyCompanyProfileRepository(session)


class CompanyProfilesUnitOfWork(Protocol):
    @property
    def profiles(self) -> CompanyProfileRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> CompanyProfilesUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyCompanyProfilesUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyCompanyProfilesUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyCompanyProfilesUnitOfWork:
        super().__enter__()
        return self

    @property
    def profiles(self) -> SqlAlchemyCompanyProfileRepository:
        return SqlAlchemyCompanyProfileRepository(self.session)
