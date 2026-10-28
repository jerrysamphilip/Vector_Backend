"""
Subject-line A/B test routes (BR-SF-18 / RP-104): one method per route of
app/routers/subject_variant_router.py.

Owns the unit of work (commit on success, rollback on a refusal) and the access rules the routes
had: another workspace's campaign is 404; promotion is for the campaign's owner or an admin. The
variant rules themselves live in app/services/subject_variant_service.py. No sqlalchemy / fastapi
imports.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.clock import iso_utc
from app.core.errors import (
    BadRequestError, ConflictError, DomainError, NotFoundError, PermissionDenied, ValidationFailed,
)
from app.models.campaign import Campaign
from app.models.email_sequence import EmailSequence
from app.repositories.campaigns import CampaignsUnitOfWork, Session
from app.services import ab_testing
from app.services.subject_variant_service import SubjectVariantError, SubjectVariantService

__all__ = ["SubjectVariantRoutesService", "variant_out"]

_ERRORS: Dict[int, type[DomainError]] = {
    400: BadRequestError, 403: PermissionDenied, 404: NotFoundError, 409: ConflictError, 422: ValidationFailed,
}


def _domain_error(e: SubjectVariantError) -> DomainError:
    return _ERRORS.get(e.status_code, BadRequestError)(e.detail)


def variant_out(v: Any) -> dict:
    return {"variant_id": v.variant_id, "label": v.label, "subject": v.subject, "weight": v.weight,
            "is_winner": bool(v.is_winner), "promoted_at": iso_utc(v.promoted_at)}


class SubjectVariantRoutesService:
    def __init__(self, uow: CampaignsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> SubjectVariantRoutesService:
        return cls(CampaignsUnitOfWork.for_session(db))

    # helpers -------------------------------------------------------------------
    def _campaign(self, campaign_id: str, user: Any) -> Campaign:
        campaign = self.uow.campaigns.get(user.tenant_id, campaign_id)
        if not campaign:
            raise NotFoundError("Campaign not found")
        return campaign

    def _variants(self) -> SubjectVariantService:
        return SubjectVariantService(self.uow.session, uow=self.uow)

    @staticmethod
    def _listing(svc: SubjectVariantService, seq: EmailSequence) -> dict:
        variants = svc.list_variants(seq.sequence_id)
        winner = svc.promoted(variants)
        return {
            "step": seq.step_number,
            "sequence_id": seq.sequence_id,
            "variants": [variant_out(v) for v in variants],
            "ab_active": len(variants) >= ab_testing.MIN_VARIANTS,
            "promoted_label": winner.label if winner else None,
            "min_variants": ab_testing.MIN_VARIANTS,
            "max_variants": ab_testing.MAX_VARIANTS,
            "min_sends_per_variant": ab_testing.MIN_SENDS_PER_VARIANT,
        }

    # reads -----------------------------------------------------------------------
    def listing(self, campaign_id: str, step: int, user: Any) -> dict:
        with self.uow:
            campaign = self._campaign(campaign_id, user)
            svc = self._variants()
            try:
                seq = svc.get_step(campaign.campaign_id, step)
            except SubjectVariantError as e:
                raise _domain_error(e) from e
            return self._listing(svc, seq)

    def stats(self, campaign_id: str, step: int, user: Any) -> Dict[str, Any]:
        with self.uow:
            campaign = self._campaign(campaign_id, user)
            svc = self._variants()
            try:
                return svc.stats(svc.get_step(campaign.campaign_id, step))
            except SubjectVariantError as e:
                raise _domain_error(e) from e

    def messages(self, campaign_id: str, step: int, label: Optional[str], limit: int, offset: int,
                 user: Any) -> Dict[str, Any]:
        with self.uow:
            campaign = self._campaign(campaign_id, user)
            svc = self._variants()
            try:
                seq = svc.get_step(campaign.campaign_id, step)
            except SubjectVariantError as e:
                raise _domain_error(e) from e
            return svc.messages(seq, label.upper() if label else None, limit, offset)

    # writes ----------------------------------------------------------------------
    def create(self, campaign_id: str, step: int, subject: str, weight: Optional[int], user: Any) -> dict:
        with self.uow as uow:
            campaign = self._campaign(campaign_id, user)
            svc = self._variants()
            try:
                seq = svc.get_step(campaign.campaign_id, step)
                variant, warnings, moved = svc.create(campaign, seq, subject, weight, user.user_id)
                uow.commit()
            except SubjectVariantError as e:
                raise _domain_error(e) from e
            return {**variant_out(variant), "warnings": warnings, "updated_messages": moved,
                    "listing": self._listing(svc, seq)}

    def update(self, campaign_id: str, step: int, label: str, subject: Optional[str], weight: Optional[int],
               user: Any) -> dict:
        if subject is None and weight is None:
            raise ValidationFailed("Nothing to update: give subject and/or weight")
        with self.uow as uow:
            campaign = self._campaign(campaign_id, user)
            svc = self._variants()
            try:
                seq = svc.get_step(campaign.campaign_id, step)
                variant, warnings, moved = svc.update(campaign, seq, label, subject, weight, user.user_id)
                uow.commit()
            except SubjectVariantError as e:
                raise _domain_error(e) from e
            return {**variant_out(variant), "warnings": warnings, "updated_messages": moved}

    def delete(self, campaign_id: str, step: int, label: str, user: Any) -> None:
        with self.uow as uow:
            campaign = self._campaign(campaign_id, user)
            svc = self._variants()
            try:
                svc.delete(campaign, svc.get_step(campaign.campaign_id, step), label, user.user_id)
                uow.commit()
            except SubjectVariantError as e:
                raise _domain_error(e) from e

    def promote(self, campaign_id: str, step: int, label: str, user: Any) -> Dict[str, Any]:
        with self.uow as uow:
            campaign = self._campaign(campaign_id, user)
            if user.role not in ("SUPER_ADMIN", "ADMIN") and campaign.created_by != user.user_id:
                raise PermissionDenied("Only the campaign owner or an admin can promote a variant")
            svc = self._variants()
            try:
                result = svc.promote(campaign, svc.get_step(campaign.campaign_id, step), label, user.user_id)
                uow.commit()
            except SubjectVariantError as e:
                raise _domain_error(e) from e
            return result
