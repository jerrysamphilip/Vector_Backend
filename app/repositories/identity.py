"""
Identity aggregate repository (ADR 0009, chunk A5): users and tenants, plus the unit of work the identity
services own (users, tenants, invitations and tokens on one session).

Tenant entry points take ``tenant_id``. A few reads are global on purpose and say so: sign-in resolves an
account by email / Google id / token subject before any tenant is known, and "one live account per email"
(F017) is checked across every workspace. Nothing here commits or applies business rules.

Related modules: app/repositories/identity_invitations.py (invitations), identity_tokens.py (refresh,
magic-login, password-reset and revoked access tokens), identity_platform.py (the cross-tenant reads of
the platform admin).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.identity import LIVE_STATUSES, PLATFORM_ADMIN
from app.models.audit import AuditLog
from app.models.campaign import Campaign
from app.models.campaign_group import CampaignGroup
from app.models.email_template import EmailTemplate, EmailTemplateVersion
from app.models.prospect_list import ProspectList
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId
from app.repositories.identity_invitations import InvitationRepository, SqlAlchemyInvitationRepository
from app.repositories.identity_platform import PlatformRepository, SqlAlchemyPlatformRepository
from app.repositories.identity_tokens import SqlAlchemyTokenRepository, TokenRepository

__all__ = [
    "DuplicateAccount", "IdentityAccess", "IdentityUnitOfWork", "Session", "SqlAlchemyIdentityUnitOfWork",
    "SqlAlchemyTenantRepository", "SqlAlchemyUserRepository", "TenantFlags", "TenantRepository",
    "UserRepository", "borrow", "identity_uow",
]


class DuplicateAccount(Exception):
    """A write hit a unique key on users (``users.active_email``: another live account has the address,
    F017; or a concurrent request created the same account first)."""


@dataclass(frozen=True)
class TenantFlags:
    status: Optional[str]
    require_mfa: bool


# Records that keep a non-nullable reference to their creator (kind -> owner column);
# app.domain.identity.BLOCKING_RECORD_MESSAGES words the refusal
_BLOCKING_RECORDS: tuple[tuple[str, Any, Any], ...] = (
    ("campaigns", Campaign, Campaign.created_by),
    ("prospect_lists", ProspectList, ProspectList.uploaded_by),
    ("campaign_groups", CampaignGroup, CampaignGroup.owner_id),
    ("template_versions", EmailTemplateVersion, EmailTemplateVersion.created_by),
)


# ── Users ──────────────────────────────────────────────────────────

class UserRepository(Protocol):
    def get(self, user_id: str) -> Optional[User]: ...

    def member(self, tenant_id: TenantId, user_id: str) -> Optional[User]: ...

    def members(self, tenant_id: TenantId) -> list[User]: ...

    def team_member(self, tenant_id: TenantId, user_id: str) -> Optional[User]: ...

    def in_tenant_with_email(self, tenant_id: TenantId, email: str, status: Optional[str] = None) -> Optional[User]: ...

    def by_google_id(self, google_id: str) -> Optional[User]: ...

    def with_email(self, email: str) -> list[User]: ...

    def any_with_email(self, email: str) -> Optional[User]: ...

    def live_with_email(self, email: str, exclude_user_id: Optional[str] = None) -> Optional[User]: ...

    def add(self, **fields: Any) -> User: ...

    def accept_totp_step(self, user: User, step: int) -> bool: ...

    def replace_recovery_codes(self, user: User, stored: Optional[str], remaining: str) -> bool: ...

    def blocking_record(self, user_id: str) -> Optional[str]: ...

    def purge(self, user: User) -> None: ...


class SqlAlchemyUserRepository:
    def __init__(self, session: Any) -> None:
        self._session = session

    def get(self, user_id: str) -> Optional[User]:
        """Global: the subject of a token or reset link (no tenant known yet)."""
        return self._session.query(User).filter(User.user_id == user_id).first()

    def member(self, tenant_id: TenantId, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.user_id == user_id, User.tenant_id == tenant_id).first()

    def members(self, tenant_id: TenantId) -> list[User]:
        """Team members of the tenant (not platform admins), newest first."""
        return self._session.query(User).filter(
            User.tenant_id == tenant_id,
            User.role != PLATFORM_ADMIN,
        ).order_by(User.created_at.desc()).all()

    def team_member(self, tenant_id: TenantId, user_id: str) -> Optional[User]:
        """A member of the tenant who is not a platform admin."""
        return self._session.query(User).filter(User.user_id == user_id, User.tenant_id == tenant_id,
                                                User.role != PLATFORM_ADMIN).first()

    def in_tenant_with_email(self, tenant_id: TenantId, email: str, status: Optional[str] = None) -> Optional[User]:
        """The tenant's account with this address (in ``status`` when given)."""
        query = self._session.query(User).filter(User.tenant_id == tenant_id, User.email == email)
        if status is not None:
            query = query.filter(User.status == status)
        return query.first()

    def by_google_id(self, google_id: str) -> Optional[User]:
        """Global: Google sign-in."""
        return self._session.query(User).filter(User.google_id == google_id).first()

    def with_email(self, email: str) -> list[User]:
        """Global: every account with this address (sign-in picks one, app.domain.identity.pick_login_user)."""
        return self._session.query(User).filter(User.email == email).all()

    def any_with_email(self, email: str) -> Optional[User]:
        """Global: any account with this address, in any state (sign-up / tenant creation refuse it)."""
        return self._session.query(User).filter(User.email == email).first()

    def live_with_email(self, email: str, exclude_user_id: Optional[str] = None) -> Optional[User]:
        """Global: another ACTIVE / INVITED account with this email, in any workspace (F017)."""
        query = self._session.query(User).filter(User.email == email, User.status.in_(LIVE_STATUSES))
        if exclude_user_id:
            query = query.filter(User.user_id != exclude_user_id)
        return query.first()

    def add(self, **fields: Any) -> User:
        user = User(**fields)
        self._session.add(user)
        return user

    def accept_totp_step(self, user: User, step: int) -> bool:
        """Record ``step`` as the user's last accepted TOTP step unless it (or a later one) already is:
        atomic, so a code is never accepted twice, also under concurrency."""
        updated = self._session.query(User).filter(
            User.user_id == user.user_id,
            or_(User.mfa_last_step.is_(None), User.mfa_last_step < step),
        ).update({User.mfa_last_step: step}, synchronize_session=False)
        if updated != 1:
            return False
        self._session.expire(user, ["mfa_last_step"])
        return True

    def replace_recovery_codes(self, user: User, stored: Optional[str], remaining: str) -> bool:
        """Swap the stored recovery-code hashes, only if nobody changed them meanwhile."""
        updated = self._session.query(User).filter(
            User.user_id == user.user_id,
            User.mfa_recovery_codes == stored,
        ).update({User.mfa_recovery_codes: remaining}, synchronize_session=False)
        if updated != 1:
            return False
        self._session.expire(user, ["mfa_recovery_codes"])
        return True

    def blocking_record(self, user_id: str) -> Optional[str]:
        """The first kind of business record (app.domain.identity.BLOCKING_RECORD_KINDS) that still
        references the user as its creator / owner, or None."""
        for kind, model, owner_column in _BLOCKING_RECORDS:
            if self._session.query(model).filter(owner_column == user_id).first():
                return kind
        return None

    def purge(self, user: User) -> None:
        """Stage the permanent deletion of a user and their own tokens, invitations they sent and nullable
        references to them. The audit trail is kept (SEC-16), detached from the user."""
        user_id = user.user_id
        SqlAlchemyTokenRepository(self._session).delete_for_user(user_id)
        SqlAlchemyInvitationRepository(self._session).delete_sent_by(user_id)
        self._session.query(AuditLog).filter(AuditLog.user_id == user_id).update(
            {AuditLog.user_id: None}, synchronize_session=False)
        # Nullable references: template approvals and users this person invited
        self._session.query(EmailTemplate).filter(EmailTemplate.approved_by == user_id).update(
            {EmailTemplate.approved_by: None}, synchronize_session=False)
        self._session.query(User).filter(User.invited_by == user_id).update(
            {User.invited_by: None}, synchronize_session=False)
        self._session.delete(user)


# ── Tenants ────────────────────────────────────────────────────────

class TenantRepository(Protocol):
    def get(self, tenant_id: TenantId) -> Optional[Tenant]: ...

    def flags(self, tenant_id: TenantId) -> Optional[TenantFlags]: ...

    def name_taken(self, name: str) -> bool: ...

    def add(self, **fields: Any) -> Tenant: ...


class SqlAlchemyTenantRepository:
    def __init__(self, session: Any) -> None:
        self._session = session

    def get(self, tenant_id: TenantId) -> Optional[Tenant]:
        return self._session.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()

    def flags(self, tenant_id: TenantId) -> Optional[TenantFlags]:
        row = self._session.query(Tenant.status, Tenant.require_mfa).filter(Tenant.tenant_id == tenant_id).first()
        return TenantFlags(row[0], bool(row[1])) if row else None

    def name_taken(self, name: str) -> bool:
        """Exact, case-insensitive match (ilike would treat % and _ in the name as wildcards)."""
        return self._session.query(Tenant).filter(func.lower(Tenant.tenant_name) == name.lower()).first() is not None

    def add(self, **fields: Any) -> Tenant:
        tenant = Tenant(**fields)
        self._session.add(tenant)
        return tenant


# ── Unit of work ───────────────────────────────────────────────────

class IdentityAccess(Protocol):
    """The identity repositories plus the session they run on (the bridge to services that still take a
    ``db``: audit log, user activity)."""

    @property
    def users(self) -> UserRepository: ...

    @property
    def tenants(self) -> TenantRepository: ...

    @property
    def invitations(self) -> InvitationRepository: ...

    @property
    def tokens(self) -> TokenRepository: ...

    @property
    def platform(self) -> PlatformRepository: ...

    @property
    def session(self) -> Session: ...

    def flush(self) -> None: ...

    def flush_account(self) -> None: ...


class IdentityUnitOfWork(IdentityAccess, Protocol):
    """The transaction an identity service call runs in."""

    def __enter__(self) -> IdentityUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def commit_account(self) -> None: ...

    def rollback(self) -> None: ...


class _Repositories:
    """Repository properties over ``self.session`` (shared by the unit of work and ``borrow``)."""

    session: Any

    @property
    def users(self) -> SqlAlchemyUserRepository:
        return SqlAlchemyUserRepository(self.session)

    @property
    def tenants(self) -> SqlAlchemyTenantRepository:
        return SqlAlchemyTenantRepository(self.session)

    @property
    def invitations(self) -> SqlAlchemyInvitationRepository:
        return SqlAlchemyInvitationRepository(self.session)

    @property
    def tokens(self) -> SqlAlchemyTokenRepository:
        return SqlAlchemyTokenRepository(self.session)

    @property
    def platform(self) -> SqlAlchemyPlatformRepository:
        return SqlAlchemyPlatformRepository(self.session)

    def flush(self) -> None:
        self.session.flush()

    def flush_account(self) -> None:
        """Flush a user that becomes ACTIVE / INVITED: a unique-key clash (``users.active_email``, F017, or
        a concurrent create) raises DuplicateAccount; the unit of work then rolls back."""
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise DuplicateAccount(str(exc.orig)) from exc


class SqlAlchemyIdentityUnitOfWork(_Repositories, SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyIdentityUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyIdentityUnitOfWork:
        super().__enter__()
        return self

    def commit_account(self) -> None:
        """Commit a user that becomes ACTIVE / INVITED (see ``flush_account``)."""
        try:
            self.session.commit()
        except IntegrityError as exc:
            raise DuplicateAccount(str(exc.orig)) from exc


@dataclass(frozen=True)
class _Borrowed(_Repositories):
    session: Session


def borrow(session: Session) -> IdentityAccess:
    """Repository access on a caller's session; the caller owns the transaction."""
    return _Borrowed(session)


def identity_uow(session: Session) -> SqlAlchemyIdentityUnitOfWork:
    """The unit of work over a request's session (``get_db``)."""
    return SqlAlchemyIdentityUnitOfWork.for_session(session)
