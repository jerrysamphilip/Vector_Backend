# app/routers/contact_scoring_router.py
"""
Contact scoring on fit and engagement (BRD v2.0 BR-CM-43, CM-083).

    GET  /contacts/scoring-model             the workspace's model, vocabulary and status (admins)
    PUT  /contacts/scoring-model             replace the model; a full recompute is queued (admins)
    POST /contacts/scoring-model/recompute   rescore every contact in the background (admins, 202)
    GET  /contacts/{id}/score                stored score + live breakdown (anyone who can see the contact)
    POST /contacts/{id}/score/recompute      rescore one contact now and store it

Scores are rule based and transparent (app/services/scoring_engine.py): no external service
and no AI. The contact list sorts by score and filters by grade / score range
(GET /contacts?sort_by=score&grade=A&score_min=50).

Registered before the contacts router so /contacts/scoring-model is not read as a contact id.

Layered (ADR 0009, chunk A7): each handler makes one call to ContactScoringService
(app/services/contact_scoring.py); SQL is in app/repositories/contact_scoring.py.
"""
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field, field_validator

from app.core.auth import require_role
from app.core.database import get_db
from app.routers.contacts_router import tenant_user
from app.services import contact_scoring, scoring_engine
from app.services.contact_scoring import ContactScoringService

router = APIRouter(prefix="/contacts", tags=["Contact scoring"])

admin_user = require_role("SUPER_ADMIN", "ADMIN")


# =============================
# SCHEMAS
# =============================

class FitRuleIn(BaseModel):
    id: Optional[str] = Field(None, max_length=36)
    label: Optional[str] = Field(None, max_length=scoring_engine.MAX_LABEL)
    field: str = Field(..., min_length=1, max_length=40)
    operator: str = Field(..., min_length=1, max_length=20)
    value: Optional[Union[str, List[str]]] = None
    points: int = Field(..., ge=scoring_engine.POINTS_RANGE[0], le=scoring_engine.POINTS_RANGE[1])

    @field_validator("value")
    @classmethod
    def _bound_value(cls, v):
        if isinstance(v, list):
            if len(v) > 100 or any(len(str(x)) > 200 for x in v):
                raise ValueError("at most 100 values of up to 200 characters")
        elif v is not None and len(v) > scoring_engine.MAX_VALUE:
            raise ValueError(f"at most {scoring_engine.MAX_VALUE} characters")
        return v


class SignalIn(BaseModel):
    points: int = Field(..., ge=0, le=100)
    max: int = Field(..., ge=0, le=100)


class GradeThresholdsIn(BaseModel):
    A: int = Field(..., ge=1, le=100)
    B: int = Field(..., ge=1, le=100)
    C: int = Field(..., ge=1, le=100)


class ScoringModelWrite(BaseModel):
    fit_rules: Optional[List[FitRuleIn]] = Field(None, max_length=scoring_engine.MAX_RULES)
    engagement: Optional[Dict[str, SignalIn]] = None
    half_life_days: Optional[int] = Field(None, ge=0, le=365)
    lookback_days: Optional[int] = Field(None, ge=1, le=730)
    fit_weight: Optional[int] = Field(None, ge=0, le=100)
    engagement_weight: Optional[int] = Field(None, ge=0, le=100)
    grade_thresholds: Optional[GradeThresholdsIn] = None

    @field_validator("engagement")
    @classmethod
    def _bound_signals(cls, v):
        if v is not None and (len(v) > len(scoring_engine.ENGAGEMENT_SIGNALS) or any(len(k) > 40 for k in v)):
            raise ValueError("too many engagement signals")
        return v


def scoring_service(db=Depends(get_db)) -> ContactScoringService:
    return ContactScoringService.for_session(db)


Scoring = Depends(scoring_service)


# =============================
# MODEL (admins)
# =============================

@router.get("/scoring-model")
def get_scoring_model(service: ContactScoringService = Scoring, current_user: Any = Depends(admin_user)):
    return service.model(current_user)


@router.put("/scoring-model")
def put_scoring_model(payload: ScoringModelWrite, background: BackgroundTasks,
                      service: ContactScoringService = Scoring, current_user: Any = Depends(admin_user)):
    body = service.save_model(current_user, payload.model_dump(exclude_none=True))
    # Every contact is rescored with the new model, after the response
    background.add_task(contact_scoring.recompute_tenant, current_user.tenant_id)
    return body


@router.post("/scoring-model/recompute", status_code=202)
def recompute_scores(background: BackgroundTasks, service: ContactScoringService = Scoring,
                     current_user: Any = Depends(admin_user)):
    """Rescore every contact in the workspace, in batches, after the response (also picked up by the worker)."""
    body = service.request_recompute(current_user)
    background.add_task(contact_scoring.recompute_tenant, current_user.tenant_id)
    return body


# =============================
# ONE CONTACT
# =============================

@router.get("/{prospect_id}/score")
def get_contact_score(prospect_id: str, service: ContactScoringService = Scoring,
                      current_user: Any = Depends(tenant_user)):
    """The stored score and a live breakdown: which fit rules matched and what each activity added."""
    return service.contact_score(current_user, prospect_id)


@router.post("/{prospect_id}/score/recompute")
def recompute_contact_score(prospect_id: str, service: ContactScoringService = Scoring,
                            current_user: Any = Depends(tenant_user)):
    return service.recompute_contact(current_user, prospect_id)
