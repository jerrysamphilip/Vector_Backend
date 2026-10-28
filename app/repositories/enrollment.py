"""
Campaign enrolment screening repository (ADR 0009; BR-DF-02): the facts the enrolment rules need
about a batch of contacts. Tenant entry points take the tenant; per-contact lookups act on ids the
caller loaded in the tenant (``tenant_contacts``). Never commits.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList
from app.models.user import User
from app.repositories.base import TenantId

__all__ = ["Session", "SqlAlchemyEnrollmentRepository"]


class SqlAlchemyEnrollmentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def sent_since(self, prospect_ids: Collection[str], cutoff: datetime) -> set[str]:
        """Contacts emailed after ``cutoff``."""
        return {r[0] for r in self._session.query(EmailMessage.prospect_id).filter(
            EmailMessage.prospect_id.in_(list(prospect_ids)), EmailMessage.sent_at > cutoff).distinct()}

    def enrolled_in_live_campaigns_since(self, prospect_ids: Collection[str], cutoff: datetime) -> set[str]:
        """Contacts enrolled in an ACTIVE or COMPLETED campaign since ``cutoff``."""
        return {r[0] for r in self._session.query(CampaignProspect.prospect_id).join(Campaign).filter(
            CampaignProspect.prospect_id.in_(list(prospect_ids)),
            Campaign.status.in_(["ACTIVE", "COMPLETED"]),
            CampaignProspect.enrolled_at >= cutoff,
        ).distinct()}

    def enrolled(self, campaign_id: str, prospect_ids: Collection[str]) -> set[str]:
        return {r[0] for r in self._session.query(CampaignProspect.prospect_id).filter(
            CampaignProspect.campaign_id == campaign_id, CampaignProspect.prospect_id.in_(list(prospect_ids)))}

    def tenant_contacts(self, tenant_id: TenantId, prospect_ids: Collection[str]) -> list[Prospect]:
        """Live contacts of the tenant among ``prospect_ids``."""
        return self._session.query(Prospect).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(list(prospect_ids)),
            Prospect.deleted_at.is_(None)).all()

    def lists_with_owners(self, tenant_id: TenantId,
                          list_ids: Collection[str]) -> list[tuple[ProspectList, Optional[User]]]:
        """The tenant's lists among ``list_ids``, each with the user who created it."""
        out = []
        for plist in self._session.query(ProspectList).filter(ProspectList.tenant_id == tenant_id,
                                                               ProspectList.list_id.in_(list(list_ids))).all():
            owner = self._session.query(User).filter(User.user_id == plist.uploaded_by).first()
            out.append((plist, owner))
        return out
