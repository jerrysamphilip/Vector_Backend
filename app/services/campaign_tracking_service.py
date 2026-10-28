# app/services/campaign_tracking_service.py
"""
Manual engagement tracking: a user marks a contact (or one message) as replied / opened when the
event happened outside the tracked channels. Records the event, moves the contact's campaign
status and, for prospect-level marks, updates the campaign's metrics. Each action is one unit of
work (SQL in app/repositories/campaigns_messages.py); the ``*_for`` methods scope the campaign first.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.core.clock import utcnow
from app.core.errors import NotFoundError
from app.models.email_message import EmailEvent, EmailMessage
from app.repositories.campaigns import CampaignsUnitOfWork
from app.services.campaign_common import require_campaign
from app.services.metrics_service import MetricsService
from app.utils.campaign_prospect_status import set_prospect_status

logger = logging.getLogger(__name__)

NO_SENT_MESSAGE = "No sent message found for this prospect"


class CampaignTrackingService:
    def __init__(self, db: Any = None, metrics: Optional[MetricsService] = None, *, uow: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)
        self.db = db
        self._metrics = metrics

    @classmethod
    def for_session(cls, db: Any) -> "CampaignTrackingService":
        return cls(db)

    @property
    def metrics(self) -> MetricsService:
        if self._metrics is None:
            self._metrics = MetricsService(self.uow.session)
        return self._metrics

    # ── Lookups ──────────────────────────────────────────────────────────────

    def _latest_sent_message(self, campaign_id: str, prospect_id: str) -> EmailMessage:
        message = self.uow.messages.latest_sent(campaign_id, prospect_id)
        if not message:
            raise NotFoundError(NO_SENT_MESSAGE)
        return message

    def _manual_event(self, message_id: str, event_type: int, marked_by: str) -> EmailEvent:
        return self.uow.messages.add_event(message_id, event_type, utcnow(),
                                           {"source": "manual", "marked_by": marked_by})

    def _set_enrollment_status(self, campaign_id: str, prospect_id: str, status: str) -> None:
        cp = self.uow.campaigns.enrolment(campaign_id, prospect_id)
        if cp:
            set_prospect_status(cp, status)

    # ── Routes ───────────────────────────────────────────────────────────────

    def mark_prospect_replied_for(self, user: Any, campaign_id: str, prospect_id: str) -> Dict[str, Any]:
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        return self.mark_prospect_replied(campaign_id, prospect_id, user.user_id)

    def mark_prospect_opened_for(self, user: Any, campaign_id: str, prospect_id: str) -> Dict[str, Any]:
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        return self.mark_prospect_opened(campaign_id, prospect_id, user.user_id)

    def mark_message_replied_for(self, user: Any, campaign_id: str, message_id: str) -> Dict[str, Any]:
        require_campaign(self.uow.campaigns, user.tenant_id, campaign_id)
        return self.mark_message_replied(campaign_id, message_id, user.user_id)

    # ── Actions ──────────────────────────────────────────────────────────────

    def mark_prospect_replied(self, campaign_id: str, prospect_id: str, user_id: str) -> Dict[str, Any]:
        """REPLY event on the contact's latest sent message (idempotent), status -> REPLIED."""
        with self.uow as uow:
            message = self._latest_sent_message(campaign_id, prospect_id)
            existing_reply = uow.messages.reply_event(message.message_id)
            if existing_reply:
                return {"status": "already_marked", "message_id": message.message_id,
                        "replied_at": existing_reply.event_time}
            event = self._manual_event(message.message_id, EmailEvent.EVENT_REPLY, user_id)
            self._set_enrollment_status(campaign_id, prospect_id, "REPLIED")
            self.metrics.process_event(campaign_id, EmailEvent.EVENT_REPLY)
            uow.commit()
            logger.info(f"[Reply] Manually marked prospect {prospect_id} as replied in campaign {campaign_id}")
            return {"status": "success", "message_id": message.message_id, "prospect_id": prospect_id,
                    "replied_at": event.event_time}

    def mark_prospect_opened(self, campaign_id: str, prospect_id: str, user_id: str) -> Dict[str, Any]:
        """OPEN event on the contact's latest sent message (repeat opens allowed), status -> OPENED."""
        with self.uow as uow:
            message = self._latest_sent_message(campaign_id, prospect_id)
            event = self._manual_event(message.message_id, EmailEvent.EVENT_OPEN, user_id)
            self._set_enrollment_status(campaign_id, prospect_id, "OPENED")
            self.metrics.process_event(campaign_id, EmailEvent.EVENT_OPEN)
            uow.commit()
            logger.info(f"[Open] Manually marked prospect {prospect_id} as opened in campaign {campaign_id}")
            return {"status": "success", "message_id": message.message_id, "prospect_id": prospect_id,
                    "opened_at": event.event_time}

    def mark_message_replied(self, campaign_id: str, message_id: str, user_id: str) -> Dict[str, Any]:
        """REPLY event on one specific message of the campaign (idempotent), status -> REPLIED."""
        with self.uow as uow:
            message = uow.messages.get(campaign_id, message_id)
            if not message:
                raise NotFoundError("Message not found")
            existing = uow.messages.reply_event(message_id)
            if existing:
                return {"status": "already_marked", "replied_at": existing.event_time}
            event = self._manual_event(message_id, EmailEvent.EVENT_REPLY, user_id)
            self._set_enrollment_status(campaign_id, message.prospect_id, "REPLIED")
            uow.commit()
            return {"status": "success", "message_id": message_id, "replied_at": event.event_time}
