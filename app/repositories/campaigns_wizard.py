"""
Campaign wizard and drafts (ADR 0009, chunk A2): the tenant's prospect lists with their sizes, the
contacts of a list, stored persona classifications, and the saved wizard drafts (one user's own).

Part of the campaign aggregate (``CampaignsUnitOfWork.wizard`` / ``.drafts``). Tenant entry points name
the tenant. Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign_draft import CampaignDraft
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.repositories.base import TenantId


class CampaignWizardRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def prospect_list(self, tenant_id: TenantId, list_id: str) -> Optional[ProspectList]:
        return self._s.execute(select(ProspectList).where(
            ProspectList.list_id == list_id, ProspectList.tenant_id == tenant_id)).scalars().first()

    def list_tenant(self, list_id: str) -> Optional[str]:
        """The tenant of a list (the legacy wizard helpers resolve a list by id)."""
        return self._s.execute(select(ProspectList.tenant_id).where(ProspectList.list_id == list_id)).scalar()

    def lists_with_sizes(self, tenant_id: TenantId) -> list[tuple[ProspectList, int]]:
        lists = list(self._s.execute(select(ProspectList).where(ProspectList.tenant_id == tenant_id)).scalars())
        if not lists:
            return []
        counts = {lid: int(n) for lid, n in self._s.execute(
            select(ProspectListMember.list_id, func.count(ProspectListMember.id))
            .where(ProspectListMember.list_id.in_([pl.list_id for pl in lists]))
            .group_by(ProspectListMember.list_id)).all()}
        return [(pl, counts.get(pl.list_id, 0)) for pl in lists]

    def live_contacts(self, prospect_ids: Sequence[str]) -> list[Prospect]:
        """The contacts that are not deleted, in no particular order."""
        if not prospect_ids:
            return []
        return list(self._s.execute(select(Prospect).where(
            Prospect.prospect_id.in_(list(prospect_ids)), Prospect.deleted_at.is_(None))).scalars())

    def persona(self, prospect_id: str) -> Optional[ProspectPersona]:
        return self._s.execute(select(ProspectPersona).where(
            ProspectPersona.prospect_id == prospect_id)).scalars().first()


class CampaignDraftsRepository:
    """Wizard drafts: the user's own (tenant + user); a draft id is looked up within the tenant."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def latest(self, tenant_id: TenantId, user_id: str) -> Optional[CampaignDraft]:
        return self._s.execute(select(CampaignDraft).where(
            CampaignDraft.tenant_id == tenant_id, CampaignDraft.user_id == user_id
        ).order_by(CampaignDraft.updated_at.desc())).scalars().first()

    def first(self, tenant_id: TenantId, user_id: str) -> Optional[CampaignDraft]:
        return self._s.execute(select(CampaignDraft).where(
            CampaignDraft.tenant_id == tenant_id, CampaignDraft.user_id == user_id)).scalars().first()

    def of_user(self, tenant_id: TenantId, user_id: str) -> list[CampaignDraft]:
        return list(self._s.execute(select(CampaignDraft).where(
            CampaignDraft.tenant_id == tenant_id, CampaignDraft.user_id == user_id
        ).order_by(CampaignDraft.updated_at.desc())).scalars())

    def get(self, tenant_id: TenantId, draft_id: str) -> Optional[CampaignDraft]:
        return self._s.execute(select(CampaignDraft).where(
            CampaignDraft.draft_id == draft_id, CampaignDraft.tenant_id == tenant_id)).scalars().first()

    def add(self, draft: CampaignDraft) -> None:
        self._s.add(draft)

    def delete(self, draft: CampaignDraft) -> None:
        self._s.delete(draft)

    def delete_of_user(self, tenant_id: TenantId, user_id: str) -> int:
        return int(self._s.query(CampaignDraft).filter(
            CampaignDraft.tenant_id == tenant_id, CampaignDraft.user_id == user_id).delete())

    def refresh(self, draft: CampaignDraft) -> None:
        self._s.refresh(draft)
