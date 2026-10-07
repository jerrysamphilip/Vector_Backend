# app/routers/campaign_draft_router.py
"""
Campaign Draft Router.
API endpoints for saving and resuming campaign creation progress.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime

from app.core.database import get_db
from app.models.campaign_draft import CampaignDraft
from app.core.auth import require_role
from app.models.user import User


router = APIRouter(prefix="/campaign-drafts", tags=["Campaign Drafts"])


# ============================================================
# Request/Response Models
# ============================================================

class SaveDraftRequest(BaseModel):
    """Request to save campaign draft state."""
    draft_name: Optional[str] = None
    current_step: int = 1
    draft_data: dict  # Full wizard state as JSON


class DraftResponse(BaseModel):
    """Response for single draft."""
    draft_id: str
    draft_name: Optional[str]
    current_step: int
    draft_data: dict[str, Any]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ============================================================
# Endpoints
# ============================================================

@router.post("/", response_model=DraftResponse)
def save_draft(request: SaveDraftRequest, db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """
    Save or update campaign draft.
    Upserts based on user_id (one active draft per user).
    Auto-save friendly - called frequently during wizard.
    """
    # Find existing draft for this user
    existing = db.query(CampaignDraft).filter(
        CampaignDraft.tenant_id == current_user.tenant_id,
        CampaignDraft.user_id == current_user.user_id
    ).first()
    
    if existing:
        # Update existing draft
        existing.draft_name = request.draft_name or existing.draft_name
        existing.current_step = request.current_step
        existing.draft_data = request.draft_data
        # updated_at auto-updates via onupdate
        db.commit()
        db.refresh(existing)
        return existing
    else:
        # Create new draft
        new_draft = CampaignDraft(
            tenant_id=current_user.tenant_id,
            user_id=current_user.user_id,
            draft_name=request.draft_name,
            current_step=request.current_step,
            draft_data=request.draft_data
        )
        db.add(new_draft)
        db.commit()
        db.refresh(new_draft)
        return new_draft


@router.get("/", response_model=List[DraftResponse])
def list_drafts(db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Get all drafts for current user."""
    drafts = db.query(CampaignDraft).filter(
        CampaignDraft.tenant_id == current_user.tenant_id,
        CampaignDraft.user_id == current_user.user_id
    ).order_by(CampaignDraft.updated_at.desc()).all()
    
    return drafts


@router.get("/current", response_model=Optional[DraftResponse])
def get_current_draft(db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Get the most recent draft for current user (used on wizard load)."""
    draft = db.query(CampaignDraft).filter(
        CampaignDraft.tenant_id == current_user.tenant_id,
        CampaignDraft.user_id == current_user.user_id
    ).order_by(CampaignDraft.updated_at.desc()).first()
    
    if not draft:
        return None
    return draft


@router.get("/{draft_id}", response_model=DraftResponse)
def get_draft(draft_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Get specific draft by ID."""
    draft = db.query(CampaignDraft).filter(
        CampaignDraft.draft_id == draft_id,
        CampaignDraft.tenant_id == current_user.tenant_id
    ).first()
    
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found"
        )
    return draft


@router.delete("/{draft_id}")
def delete_draft(draft_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Delete a specific draft."""
    draft = db.query(CampaignDraft).filter(
        CampaignDraft.draft_id == draft_id,
        CampaignDraft.tenant_id == current_user.tenant_id
    ).first()
    
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found"
        )
    
    db.delete(draft)
    db.commit()
    return {"status": "deleted", "draft_id": draft_id}


@router.delete("/")
def clear_all_drafts(db: Session = Depends(get_db), current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))):
    """Clear all drafts for current user (called after successful campaign launch)."""
    deleted = db.query(CampaignDraft).filter(
        CampaignDraft.tenant_id == current_user.tenant_id,
        CampaignDraft.user_id == current_user.user_id
    ).delete()
    
    db.commit()
    return {"status": "cleared", "deleted_count": deleted}
