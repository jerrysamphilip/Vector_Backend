"""
Suppression list repository (ADR 0009; BR-DF-08): the workspace's suppressed addresses and what a new
suppression changes (contacts with the address, their pending emails and campaign enrolments). Every
entry point takes the tenant; the follow-up writes act on the contact ids it returned. Never commits.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import ColumnElement, func, or_
from sqlalchemy.orm import Session

from app.models.campaign import CampaignProspect
from app.models.email_message import EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.repositories.base import TenantId

__all__ = ["Session", "SqlAlchemySuppressionRepository", "active_clause", "suppression_repo"]


def active_clause(now: datetime) -> ColumnElement[bool]:
    """A suppression is active while it has no expiry or the expiry is in the future."""
    return or_(GlobalUnsubscribe.suppression_expires_at.is_(None), GlobalUnsubscribe.suppression_expires_at > now)


class SqlAlchemySuppressionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def active_emails(self, tenant_id: TenantId, emails: Collection[str], now: datetime) -> list[Optional[str]]:
        """Stored addresses (as written) among ``emails`` (lower-cased) with an active suppression."""
        return [e for (e,) in self._session.query(GlobalUnsubscribe.email).filter(
            GlobalUnsubscribe.tenant_id == tenant_id, func.lower(GlobalUnsubscribe.email).in_(list(emails)),
            active_clause(now))]

    def active(self, tenant_id: TenantId, email: str, now: datetime) -> Optional[GlobalUnsubscribe]:
        return self._session.query(GlobalUnsubscribe).filter(
            GlobalUnsubscribe.tenant_id == tenant_id,
            func.lower(GlobalUnsubscribe.email) == email.strip().lower(),
            active_clause(now),
        ).first()

    def entry(self, tenant_id: TenantId, email_l: str) -> Optional[GlobalUnsubscribe]:
        """The suppression row for the address, active or expired."""
        return self._session.query(GlobalUnsubscribe).filter(
            GlobalUnsubscribe.tenant_id == tenant_id, func.lower(GlobalUnsubscribe.email) == email_l).first()

    def contacts_with_email(self, tenant_id: TenantId, email_l: str) -> list[Prospect]:
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                    func.lower(Prospect.email) == email_l).all()

    def cancel_pending(self, prospect_ids: Sequence[str], statuses: Sequence[str], failure_reason: str) -> int:
        """Cancel the contacts' outbound emails in ``statuses``; the number cancelled."""
        return self._session.query(EmailMessage).filter(
            EmailMessage.prospect_id.in_(list(prospect_ids)), EmailMessage.direction == "OUTBOUND",
            EmailMessage.status.in_(list(statuses)),
        ).update({EmailMessage.status: "CANCELLED", EmailMessage.failure_reason: failure_reason},
                 synchronize_session=False)

    def enrolments(self, prospect_ids: Sequence[str]) -> list[CampaignProspect]:
        return self._session.query(CampaignProspect).filter(CampaignProspect.prospect_id.in_(list(prospect_ids))).all()

    def remove(self, tenant_id: TenantId, email_l: str) -> None:
        self._session.query(GlobalUnsubscribe).filter(
            GlobalUnsubscribe.tenant_id == tenant_id, func.lower(GlobalUnsubscribe.email) == email_l,
        ).delete(synchronize_session=False)

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity


def suppression_repo(session: Session) -> SqlAlchemySuppressionRepository:
    return SqlAlchemySuppressionRepository(session)
