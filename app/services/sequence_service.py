# app/services/sequence_service.py
"""
Email Sequence service for managing campaign steps (chunk A2: SQL in app/repositories/campaigns_content.py).

The route methods (``add_step_for``, ``steps_for``, ``update_step_for``) own their unit of work and scope
the step to the caller's tenant through its campaign; deleting a step goes through
CampaignEditService.delete_step (unsent emails, renumbering, re-planning). The plain methods
(``add_step``, ``update_step``, ...) run inside the caller's transaction.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional

from app.core.concurrency import resolve_expected
from app.core.errors import BadRequestError, NotFoundError
from app.models.email_sequence import EmailSequence
from app.repositories.campaigns import CampaignsUnitOfWork
from app.schemas.sequence_schema import (SequenceResponse, SequenceStepCreate, SequenceStepResponse,
                                         SequenceStepUpdate)
from app.services.campaign_common import require_campaign

SEQUENCE_STEP_NOT_FOUND = "Sequence step not found"


class SequenceService:
    """Service for email sequence management."""

    MAX_STEPS = 7

    def __init__(self, db: Any = None, *, uow: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)
        self.db = db

    @classmethod
    def for_session(cls, db: Any) -> "SequenceService":
        return cls(db)

    # ── routes ────────────────────────────────────────────────────────────────────────────────

    def add_step_for(self, user: Any, campaign_id: str, data: SequenceStepCreate) -> SequenceStepResponse:
        with self.uow as uow:
            require_campaign(uow.campaigns, user.tenant_id, campaign_id)
            try:
                step = self.add_step(campaign_id, data)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
            uow.commit()
            return self.to_response(step)

    def steps_for(self, user: Any, campaign_id: str) -> SequenceResponse:
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        steps = self.get_campaign_sequences(campaign_id)
        return SequenceResponse(campaign_id=campaign_id, steps=[self.to_response(s) for s in steps],
                                total_steps=len(steps), total_wait_days=self.get_total_wait_days(campaign_id))

    def update_step_for(self, user: Any, sequence_id: str, data: SequenceStepUpdate,
                        if_match: Optional[int]) -> SequenceStepResponse:
        """Send the step's row_version as expected_version or If-Match (T-3); stale -> 409."""
        with self.uow as uow:
            step = uow.content.step_for_tenant(user.tenant_id, sequence_id)
            if not step:
                raise NotFoundError(SEQUENCE_STEP_NOT_FOUND)
            uow.claim_version(step, resolve_expected(data.expected_version, if_match), "sequence step")
            updated = self.update_step(sequence_id, data)
            if not updated:
                raise NotFoundError(SEQUENCE_STEP_NOT_FOUND)
            uow.commit()
            return self.to_response(updated)

    # ── inside the caller's transaction ───────────────────────────────────────────────────────

    def get_step(self, sequence_id: str) -> Optional[EmailSequence]:
        return self.uow.content.step_by_id(sequence_id)

    def get_campaign_sequences(self, campaign_id: str) -> List[EmailSequence]:
        """All steps of a campaign, ordered by step number."""
        return self.uow.content.steps(campaign_id)

    def get_step_count(self, campaign_id: str) -> int:
        return self.uow.content.step_count(campaign_id)

    def add_step(self, campaign_id: str, data: SequenceStepCreate) -> EmailSequence:
        """Add a step; ValueError when the campaign has 7 steps or the number is taken."""
        if self.get_step_count(campaign_id) >= self.MAX_STEPS:
            raise ValueError(f"Maximum {self.MAX_STEPS} steps allowed per campaign")
        if self.uow.content.step(campaign_id, data.step_number):
            raise ValueError(f"Step {data.step_number} already exists in this campaign")
        step = EmailSequence(
            sequence_id=str(uuid.uuid4()), campaign_id=campaign_id, step_number=data.step_number,
            wait_days=data.wait_days, stop_on_reply=data.stop_on_reply, stop_on_bounce=data.stop_on_bounce,
            send_start_hour=data.send_start_hour, send_end_hour=data.send_end_hour,
        )
        self.uow.content.add(step)
        self.uow.content.flush()
        return step

    def update_step(self, sequence_id: str, data: SequenceStepUpdate) -> Optional[EmailSequence]:
        step = self.get_step(sequence_id)
        if not step:
            return None
        for name in ("wait_days", "stop_on_reply", "stop_on_bounce", "send_start_hour", "send_end_hour"):
            value = getattr(data, name)
            if value is not None:
                setattr(step, name, value)
        self.uow.content.flush()
        return step

    def delete_step(self, sequence_id: str) -> bool:
        """Delete a step row only (no emails / templates): see CampaignEditService.delete_step for the route."""
        step = self.get_step(sequence_id)
        if not step:
            return False
        self.uow.content.delete(step)
        self.uow.content.flush()
        return True

    def get_total_wait_days(self, campaign_id: str) -> int:
        return self.uow.content.total_wait_days(campaign_id)

    def to_response(self, step: EmailSequence) -> SequenceStepResponse:
        """The step with its first template's subject (if any)."""
        template = self.uow.content.first_template_of_step(step.sequence_id)
        subject = None
        if template:
            subject = template.subject[:50] + "..." if len(template.subject) > 50 else template.subject
        return SequenceStepResponse(
            sequence_id=step.sequence_id, campaign_id=step.campaign_id, step_number=step.step_number,
            row_version=step.row_version or 1, wait_days=step.wait_days, stop_on_reply=step.stop_on_reply,
            stop_on_bounce=step.stop_on_bounce, send_start_hour=step.send_start_hour,
            send_end_hour=step.send_end_hour, template_id=template.template_id if template else None,
            template_subject=subject,
        )
