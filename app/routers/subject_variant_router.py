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

from typing import Annotated, Any, List, Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field, field_validator

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.services import ab_testing
from app.services.subject_variant_routes import SubjectVariantRoutesService

router = APIRouter(prefix="/campaigns", tags=["Subject A/B tests"])

BASE = "/{campaign_id}/sequences/{step}/subject-variants"
READ_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
WRITE_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

StepParam = Annotated[int, Path(ge=1, le=7, description="Sequence step number (1-7)")]
LabelParam = Annotated[str, Path(pattern="^[A-Da-d]$", description="Variant label A-D")]
CampaignParam = Annotated[str, Path(min_length=1, max_length=36)]


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


def subject_variant_service(db=Depends(get_db)) -> SubjectVariantRoutesService:
    return SubjectVariantRoutesService.for_session(db)


Service = Depends(subject_variant_service)


@router.get(BASE)
def list_subject_variants(
    campaign_id: CampaignParam,
    step: StepParam,
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*READ_ROLES)),
):
    return service.listing(campaign_id, step, current_user)


@router.post(BASE, status_code=201, dependencies=[Depends(require_permission("manage_campaigns"))])
def create_subject_variant(
    data: VariantCreate,
    campaign_id: CampaignParam,
    step: StepParam,
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*WRITE_ROLES)),
):
    return service.create(campaign_id, step, data.subject, data.weight, current_user)


@router.get(BASE + "/stats")
def subject_variant_stats(
    campaign_id: CampaignParam,
    step: StepParam,
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*READ_ROLES)),
):
    return service.stats(campaign_id, step, current_user)


@router.get(BASE + "/messages")
def subject_variant_messages(
    campaign_id: CampaignParam,
    step: StepParam,
    label: Optional[str] = Query(None, pattern="^[A-Da-d]$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*READ_ROLES)),
):
    return service.messages(campaign_id, step, label, limit, offset, current_user)


@router.patch(BASE + "/{label}", dependencies=[Depends(require_permission("manage_campaigns"))])
def update_subject_variant(
    data: VariantUpdate,
    campaign_id: CampaignParam,
    step: StepParam,
    label: LabelParam,
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*WRITE_ROLES)),
):
    return service.update(campaign_id, step, label, data.subject, data.weight, current_user)


@router.delete(BASE + "/{label}", status_code=204, dependencies=[Depends(require_permission("manage_campaigns"))])
def delete_subject_variant(
    campaign_id: CampaignParam,
    step: StepParam,
    label: LabelParam,
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*WRITE_ROLES)),
):
    service.delete(campaign_id, step, label, current_user)
    return None


@router.post(BASE + "/{label}/promote", dependencies=[Depends(require_permission("manage_campaigns"))])
def promote_subject_variant(
    campaign_id: CampaignParam,
    step: StepParam,
    label: LabelParam,
    service: SubjectVariantRoutesService = Service,
    current_user: Any = Depends(require_role(*WRITE_ROLES)),
):
    return service.promote(campaign_id, step, label, current_user)


__all__: List[str] = ["router"]
