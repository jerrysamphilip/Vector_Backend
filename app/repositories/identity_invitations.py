"""
Workspace invitations (ADR 0009, chunk A5). Every entry point but the token lookup is tenant-scoped; an
invitation is found by its token hash before the invitee is signed in. Nothing here commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import func, text

from app.domain.identity import OPEN_INVITATION_STATUSES, InvitationHistory, InvitationStatus
from app.models.invitation import Invitation
from app.repositories.base import TenantId

__all__ = ["InvitationRepository", "SqlAlchemyInvitationRepository"]


class InvitationRepository(Protocol):
    def add(self, **fields: Any) -> Invitation: ...

    def get(self, tenant_id: TenantId, invitation_id: str) -> Optional[Invitation]: ...

    def pending_by_token(self, token_hash: str) -> Optional[Invitation]: ...

    def pending_for(self, tenant_id: TenantId, email: str) -> Optional[Invitation]: ...

    def for_tenant(self, tenant_id: TenantId) -> list[Invitation]: ...

    def accept_open(self, tenant_id: TenantId, email: str) -> int: ...

    def revoke_pending(self, tenant_id: TenantId, email: str) -> int: ...

    def history(self, tenant_id: TenantId, email: str) -> InvitationHistory: ...

    def seconds_since_last(self, tenant_id: TenantId, email: str) -> Optional[int]: ...

    def delete_sent_by(self, user_id: str) -> None: ...


class SqlAlchemyInvitationRepository:
    def __init__(self, session: Any) -> None:
        self._session = session

    def add(self, **fields: Any) -> Invitation:
        invitation = Invitation(**fields)
        self._session.add(invitation)
        return invitation

    def get(self, tenant_id: TenantId, invitation_id: str) -> Optional[Invitation]:
        return self._session.query(Invitation).filter(
            Invitation.invitation_id == invitation_id,
            Invitation.tenant_id == tenant_id,
        ).first()

    def pending_by_token(self, token_hash: str) -> Optional[Invitation]:
        """Global: the invitee is not signed in; the emailed token is the credential."""
        return self._session.query(Invitation).filter(
            Invitation.token == token_hash, Invitation.status == InvitationStatus.PENDING).first()

    def pending_for(self, tenant_id: TenantId, email: str) -> Optional[Invitation]:
        return self._session.query(Invitation).filter(
            Invitation.tenant_id == tenant_id,
            Invitation.email == email,
            Invitation.status == InvitationStatus.PENDING,
        ).first()

    def for_tenant(self, tenant_id: TenantId) -> list[Invitation]:
        """All invitations of the workspace, newest first."""
        return self._session.query(Invitation).filter(
            Invitation.tenant_id == tenant_id).order_by(Invitation.created_at.desc()).all()

    def accept_open(self, tenant_id: TenantId, email: str) -> int:
        """The invitee joined: their open (PENDING / EXPIRED) invitation is accepted."""
        return self._session.query(Invitation).filter(
            Invitation.tenant_id == tenant_id,
            Invitation.email == email,
            Invitation.status.in_(OPEN_INVITATION_STATUSES),
        ).update({Invitation.status: InvitationStatus.ACCEPTED}, synchronize_session=False)

    def revoke_pending(self, tenant_id: TenantId, email: str) -> int:
        return self._session.query(Invitation).filter(
            Invitation.tenant_id == tenant_id,
            Invitation.email == email,
            Invitation.status == InvitationStatus.PENDING,
        ).update({Invitation.status: InvitationStatus.REVOKED}, synchronize_session=False)

    def history(self, tenant_id: TenantId, email: str) -> InvitationHistory:
        joined = self._session.query(Invitation.invitation_id).filter(
            Invitation.tenant_id == tenant_id, Invitation.email == email,
            Invitation.status == InvitationStatus.ACCEPTED).first()
        invited = self._session.query(Invitation.invitation_id).filter(
            Invitation.tenant_id == tenant_id, Invitation.email == email).first()
        return InvitationHistory(invited=invited is not None, accepted=joined is not None)

    def seconds_since_last(self, tenant_id: TenantId, email: str) -> Optional[int]:
        """Seconds since the newest invitation to the address, on the database clock (created_at is set
        by the database); None when there is none."""
        since_last = self._session.query(
            func.min(func.timestampdiff(text("SECOND"), Invitation.created_at, func.now()))).filter(
            Invitation.tenant_id == tenant_id, Invitation.email == email).scalar()
        return None if since_last is None else int(since_last)

    def delete_sent_by(self, user_id: str) -> None:
        """Invitations this user sent (invited_by is NOT NULL, so it can't be nulled out)."""
        self._session.query(Invitation).filter(Invitation.invited_by == user_id).delete(synchronize_session=False)
