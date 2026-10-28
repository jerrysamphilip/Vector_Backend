"""
Bounced-contact re-validation repository (ADR 0009): the tenant's suppression rows, contacts and
their BOUNCED enrolments. Every entry point takes the tenant; enrolment lookups act on a contact id
resolved in the tenant first. Never commits.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaign import CampaignProspect
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.repositories.base import TenantId

__all__ = ["Session", "SqlAlchemyRevalidationRepository"]


class SqlAlchemyRevalidationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def suppression(self, tenant_id: TenantId, email: Optional[str]) -> Optional[GlobalUnsubscribe]:
        """The suppression row stored for exactly this address."""
        return self._session.query(GlobalUnsubscribe).filter(
            GlobalUnsubscribe.tenant_id == tenant_id, GlobalUnsubscribe.email == email).first()

    def suppressions(self, tenant_id: TenantId) -> list[GlobalUnsubscribe]:
        return self._session.query(GlobalUnsubscribe).filter(GlobalUnsubscribe.tenant_id == tenant_id).all()

    def contact(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def other_contact_with_email(self, tenant_id: TenantId, email: str, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.email == email,
                                                    Prospect.prospect_id != prospect_id).first()

    def contacts_with_emails(self, tenant_id: TenantId, emails: Collection[str]) -> list[Prospect]:
        return self._session.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                    Prospect.email.in_(list(emails))).all()

    def bounced_enrolments(self, prospect_id: str) -> list[CampaignProspect]:
        return self._session.query(CampaignProspect).filter(CampaignProspect.prospect_id == prospect_id,
                                                            CampaignProspect.status == "BOUNCED").all()

    def count_bounced_enrolments(self, prospect_id: str) -> int:
        return self._session.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.prospect_id == prospect_id, CampaignProspect.status == "BOUNCED").scalar() or 0

    def delete(self, entity: Any) -> None:
        self._session.delete(entity)

    def flush(self) -> None:
        self._session.flush()
