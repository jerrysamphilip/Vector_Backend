"""
Campaign Draft Router.
API endpoints for saving and resuming campaign creation progress.

Thin routes (ADR 0009, chunk A2): one call to app/services/campaign_draft_service.py each.
"""

from typing import Any, List, Optional

from fastapi import APIRouter, Depends

from app.core.auth import require_role
from app.core.database import get_db
from app.schemas.campaign_draft_schema import DraftResponse, SaveDraftRequest
from app.services.campaign_draft_service import CampaignDraftService

router = APIRouter(prefix="/campaign-drafts", tags=["Campaign Drafts"])

ANY_ROLE = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def draft_service(db=Depends(get_db)) -> CampaignDraftService:
    return CampaignDraftService.for_session(db)


# ============================================================
# Endpoints
# ============================================================

@router.post("/", response_model=DraftResponse)
def save_draft(request: SaveDraftRequest, drafts: CampaignDraftService = Depends(draft_service),
               current_user: Any = Depends(require_role(*ANY_ROLE))):
    """
    Save or update campaign draft.
    Upserts based on user_id (one active draft per user).
    Auto-save friendly - called frequently during wizard.
    """
    return drafts.save(current_user, request.draft_name, request.current_step, request.draft_data)


@router.get("/", response_model=List[DraftResponse])
def list_drafts(drafts: CampaignDraftService = Depends(draft_service),
                current_user: Any = Depends(require_role(*ANY_ROLE))):
    """Get all drafts for current user."""
    return drafts.mine(current_user)


@router.get("/current", response_model=Optional[DraftResponse])
def get_current_draft(drafts: CampaignDraftService = Depends(draft_service),
                      current_user: Any = Depends(require_role(*ANY_ROLE))):
    """Get the most recent draft for current user (used on wizard load)."""
    return drafts.current(current_user)


@router.get("/{draft_id}", response_model=DraftResponse)
def get_draft(draft_id: str, drafts: CampaignDraftService = Depends(draft_service),
              current_user: Any = Depends(require_role(*ANY_ROLE))):
    """Get specific draft by ID."""
    return drafts.get(current_user, draft_id)


@router.delete("/{draft_id}")
def delete_draft(draft_id: str, drafts: CampaignDraftService = Depends(draft_service),
                 current_user: Any = Depends(require_role(*ANY_ROLE))):
    """Delete a specific draft."""
    return drafts.delete(current_user, draft_id)


@router.delete("/")
def clear_all_drafts(drafts: CampaignDraftService = Depends(draft_service),
                     current_user: Any = Depends(require_role(*ANY_ROLE))):
    """Clear all drafts for current user (called after successful campaign launch)."""
    return drafts.clear(current_user)
