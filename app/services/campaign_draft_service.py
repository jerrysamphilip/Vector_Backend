"""
Campaign wizard drafts (chunk A2): save and resume an unfinished campaign creation. One draft per user is
kept up to date by the wizard's auto-save; a user lists and clears only their own drafts. SQL in
app/repositories/campaigns_wizard.py (CampaignDraftsRepository); each method is one unit of work.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.errors import NotFoundError
from app.models.campaign_draft import CampaignDraft
from app.repositories.campaigns import CampaignsUnitOfWork

DRAFT_NOT_FOUND = "Draft not found"


class CampaignDraftService:
    def __init__(self, db: Any = None, *, uow: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)

    @classmethod
    def for_session(cls, db: Any) -> "CampaignDraftService":
        return cls(db)

    def save(self, user: Any, draft_name: Optional[str], current_step: int, draft_data: dict) -> CampaignDraft:
        """Upsert the user's draft (auto-save friendly: called often during the wizard)."""
        with self.uow as uow:
            draft = uow.drafts.first(user.tenant_id, user.user_id)
            if draft:
                draft.draft_name = draft_name or draft.draft_name
                draft.current_step = current_step
                draft.draft_data = draft_data
            else:
                draft = CampaignDraft(tenant_id=user.tenant_id, user_id=user.user_id, draft_name=draft_name,
                                      current_step=current_step, draft_data=draft_data)
                uow.drafts.add(draft)
            uow.commit()
            uow.drafts.refresh(draft)
            return draft

    def mine(self, user: Any) -> List[CampaignDraft]:
        return self.uow.drafts.of_user(user.tenant_id, user.user_id)

    def current(self, user: Any) -> Optional[CampaignDraft]:
        """The user's most recent draft (the wizard loads it on open)."""
        return self.uow.drafts.latest(user.tenant_id, user.user_id)

    def get(self, user: Any, draft_id: str) -> CampaignDraft:
        draft = self.uow.drafts.get(user.tenant_id, draft_id)
        if not draft:
            raise NotFoundError(DRAFT_NOT_FOUND)
        return draft

    def delete(self, user: Any, draft_id: str) -> Dict[str, str]:
        with self.uow as uow:
            draft = self.get(user, draft_id)
            uow.drafts.delete(draft)
            uow.commit()
            return {"status": "deleted", "draft_id": draft_id}

    def clear(self, user: Any) -> Dict[str, Any]:
        """Delete all the user's drafts (called after a successful launch)."""
        with self.uow as uow:
            deleted = uow.drafts.delete_of_user(user.tenant_id, user.user_id)
            uow.commit()
            return {"status": "cleared", "deleted_count": deleted}
