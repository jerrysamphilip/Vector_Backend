# app/routers/subject_variant_router.py
"""
A/B testing of email subject lines (BR-SF-18 / RP-104).

    GET    /campaigns/{id}/sequences/{step}/subject-variants                 list
    POST   /campaigns/{id}/sequences/{step}/subject-variants                 add (next free label A..D)
    PATCH  /campaigns/{id}/sequences/{step}/subject-variants/{label}         subject / weight
    DELETE /campaigns/{id}/sequences/{step}/subject-variants/{label}
    GET    /campaigns/{id}/sequences/{step}/subject-variants/stats           per-variant stats + winner
    GET    /campaigns/{id}/sequences/{step}/subject-variants/messages        emails with their variant
    POST   /campaigns/{id}/sequences/{step}/subject-variants/{label}/promote promote the winner

Reads: any role of the workspace (like step analytics). Changes: the manage_campaigns
permission. Promotion: the campaign's owner (creator) or a workspace admin, with
manage_campaigns. Another workspace's campaign is 404.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.models.campaign import Campaign
from app.models.user import User
from app.services import ab_testing
from app.services.subject_variant_service import SubjectVariantError, SubjectVariantService

router = APIRouter(prefix="/campaigns", tags=["Subject A/B tests"])

BASE = "/{campaign_id}/sequences/{step}/subject-variants"
READ_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
WRITE_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

StepParam = Path(..., ge=1, le=7, description="Sequence step number (1-7)")
LabelParam = Path(..., pattern="^[A-Da-d]$", description="Variant label A-D")
CampaignParam = Path(..., min_length=1, max_length=36)


class VariantCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=ab_testing.MAX_SUBJECT_LENGTH)
    weight: Optional[int] = Field(None, ge=ab_testing.MIN_WEIGHT, le=ab_testing.MAX_WEIGHT,
                                  description="Relative share of the step's emails; default equal split")

    @field_validator("subject")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Subject must not be empty")
        return v


class VariantUpdate(BaseModel):
    subject: Optional[str] = Field(None, min_length=1, max_length=ab_testing.MAX_SUBJECT_LENGTH)
    weight: Optional[int] = Field(None, ge=ab_testing.MIN_WEIGHT, le=ab_testing.MAX_WEIGHT)

    @field_validator("subject")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Subject must not be empty")
        return v


class VariantOut(BaseModel):
    variant_id: str
    label: str
    subject: str
    weight: int
    is_winner: bool
    promoted_at: Optional[str] = None


def _out(v) -> dict:
    return VariantOut(variant_id=v.variant_id, label=v.label, subject=v.subject, weight=v.weight,
                      is_winner=bool(v.is_winner),
                      promoted_at=v.promoted_at.isoformat() if v.promoted_at else None).model_dump()


def _campaign(db: Session, campaign_id: str, user: User) -> Campaign:
    campaign = db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
    if not campaign or campaign.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _fail(e: SubjectVariantError, db: Session):
    db.rollback()
    raise HTTPException(status_code=e.status_code, detail=e.detail)


def _listing(svc: SubjectVariantService, seq) -> dict:
    variants = svc.list_variants(seq.sequence_id)
    winner = svc.promoted(variants)
    return {
        "step": seq.step_number,
        "sequence_id": seq.sequence_id,
        "variants": [_out(v) for v in variants],
        "ab_active": len(variants) >= ab_testing.MIN_VARIANTS,
        "promoted_label": winner.label if winner else None,
        "min_variants": ab_testing.MIN_VARIANTS,
        "max_variants": ab_testing.MAX_VARIANTS,
        "min_sends_per_variant": ab_testing.MIN_SENDS_PER_VARIANT,
    }


@router.get(BASE)
def list_subject_variants(
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*READ_ROLES)),
):
    campaign = _campaign(db, campaign_id, current_user)
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
    except SubjectVariantError as e:
        _fail(e, db)
    return _listing(svc, seq)


@router.post(BASE, status_code=201, dependencies=[Depends(require_permission("manage_campaigns"))])
def create_subject_variant(
    data: VariantCreate,
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*WRITE_ROLES)),
):
    campaign = _campaign(db, campaign_id, current_user)
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
        variant, warnings, moved = svc.create(campaign, seq, data.subject, data.weight, current_user.user_id)
        db.commit()
    except SubjectVariantError as e:
        _fail(e, db)
    return {**_out(variant), "warnings": warnings, "updated_messages": moved, "listing": _listing(svc, seq)}


@router.get(BASE + "/stats")
def subject_variant_stats(
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*READ_ROLES)),
):
    campaign = _campaign(db, campaign_id, current_user)
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
    except SubjectVariantError as e:
        _fail(e, db)
    return svc.stats(seq)


@router.get(BASE + "/messages")
def subject_variant_messages(
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    label: Optional[str] = Query(None, pattern="^[A-Da-d]$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*READ_ROLES)),
):
    campaign = _campaign(db, campaign_id, current_user)
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
    except SubjectVariantError as e:
        _fail(e, db)
    return svc.messages(seq, label.upper() if label else None, limit, offset)


@router.patch(BASE + "/{label}", dependencies=[Depends(require_permission("manage_campaigns"))])
def update_subject_variant(
    data: VariantUpdate,
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    label: str = LabelParam,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*WRITE_ROLES)),
):
    if data.subject is None and data.weight is None:
        raise HTTPException(status_code=422, detail="Nothing to update: give subject and/or weight")
    campaign = _campaign(db, campaign_id, current_user)
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
        variant, warnings, moved = svc.update(campaign, seq, label, data.subject, data.weight, current_user.user_id)
        db.commit()
    except SubjectVariantError as e:
        _fail(e, db)
    return {**_out(variant), "warnings": warnings, "updated_messages": moved}


@router.delete(BASE + "/{label}", status_code=204, dependencies=[Depends(require_permission("manage_campaigns"))])
def delete_subject_variant(
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    label: str = LabelParam,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*WRITE_ROLES)),
):
    campaign = _campaign(db, campaign_id, current_user)
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
        svc.delete(campaign, seq, label, current_user.user_id)
        db.commit()
    except SubjectVariantError as e:
        _fail(e, db)
    return None


@router.post(BASE + "/{label}/promote", dependencies=[Depends(require_permission("manage_campaigns"))])
def promote_subject_variant(
    campaign_id: str = CampaignParam,
    step: int = StepParam,
    label: str = LabelParam,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(*WRITE_ROLES)),
):
    campaign = _campaign(db, campaign_id, current_user)
    if current_user.role not in ("SUPER_ADMIN", "ADMIN") and campaign.created_by != current_user.user_id:
        raise HTTPException(status_code=403, detail="Only the campaign owner or an admin can promote a variant")
    svc = SubjectVariantService(db)
    try:
        seq = svc.get_step(campaign.campaign_id, step)
        result = svc.promote(campaign, seq, label, current_user.user_id)
        db.commit()
    except SubjectVariantError as e:
        _fail(e, db)
    return result


__all__: List[str] = ["router"]
