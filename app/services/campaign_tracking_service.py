# app/services/campaign_tracking_service.py
"""
Manual engagement tracking: a user marks a contact (or one message) as replied / opened when the
event happened outside the tracked channels. Records the event, moves the contact's campaign
status and, for prospect-level marks, updates the campaign's metrics.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.campaign import CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage
from app.services.metrics_service import MetricsService
from app.utils.campaign_prospect_status import set_prospect_status

logger = logging.getLogger(__name__)

NO_SENT_MESSAGE = "No sent message found for this prospect"


class CampaignTrackingService:
    def __init__(self, db: Session, metrics: Optional[MetricsService] = None) -> None:
        self.db = db
        self.metrics = metrics or MetricsService(db)

    # ── Lookups ──────────────────────────────────────────────────────────────

    def _latest_sent_message(self, campaign_id: str, prospect_id: str) -> EmailMessage:
        message = self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.status == "SENT"
        ).order_by(EmailMessage.sent_at.desc()).first()
        if not message:
            raise NotFoundError(NO_SENT_MESSAGE)
        return message

    def _existing_reply(self, message_id: str) -> Optional[EmailEvent]:
        return self.db.query(EmailEvent).filter(
            EmailEvent.message_id == message_id,
            EmailEvent.event_type == EmailEvent.EVENT_REPLY
        ).first()

    def _manual_event(self, message_id: str, event_type: str, marked_by: str) -> EmailEvent:
        event = EmailEvent(
            message_id=message_id,
            event_type=event_type,
            event_time=datetime.utcnow(),
            event_metadata={"source": "manual", "marked_by": marked_by}
        )
        self.db.add(event)
        return event

    def _set_enrollment_status(self, campaign_id: str, prospect_id: str, status: str) -> None:
        cp = self.db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.prospect_id == prospect_id
        ).first()
        if cp:
            set_prospect_status(cp, status)

    # ── Actions ──────────────────────────────────────────────────────────────

    def mark_prospect_replied(self, campaign_id: str, prospect_id: str, user_id: str) -> Dict[str, Any]:
        """REPLY event on the contact's latest sent message (idempotent), status -> REPLIED."""
        message = self._latest_sent_message(campaign_id, prospect_id)
        existing_reply = self._existing_reply(message.message_id)
        if existing_reply:
            return {
                "status": "already_marked",
                "message_id": message.message_id,
                "replied_at": existing_reply.event_time
            }

        event = self._manual_event(message.message_id, EmailEvent.EVENT_REPLY, user_id)
        self._set_enrollment_status(campaign_id, prospect_id, "REPLIED")
        self.metrics.process_event(campaign_id, EmailEvent.EVENT_REPLY)
        self.db.commit()
        logger.info(f"[Reply] Manually marked prospect {prospect_id} as replied in campaign {campaign_id}")
        return {
            "status": "success",
            "message_id": message.message_id,
            "prospect_id": prospect_id,
            "replied_at": event.event_time
        }

    def mark_prospect_opened(self, campaign_id: str, prospect_id: str, user_id: str) -> Dict[str, Any]:
        """OPEN event on the contact's latest sent message (repeat opens allowed), status -> OPENED."""
        message = self._latest_sent_message(campaign_id, prospect_id)
        event = self._manual_event(message.message_id, EmailEvent.EVENT_OPEN, user_id)
        self._set_enrollment_status(campaign_id, prospect_id, "OPENED")
        self.metrics.process_event(campaign_id, EmailEvent.EVENT_OPEN)
        self.db.commit()
        logger.info(f"[Open] Manually marked prospect {prospect_id} as opened in campaign {campaign_id}")
        return {
            "status": "success",
            "message_id": message.message_id,
            "prospect_id": prospect_id,
            "opened_at": event.event_time
        }

    def mark_message_replied(self, campaign_id: str, message_id: str, user_id: str) -> Dict[str, Any]:
        """REPLY event on one specific message of the campaign (idempotent), status -> REPLIED."""
        message = self.db.query(EmailMessage).filter(
            EmailMessage.message_id == message_id,
            EmailMessage.campaign_id == campaign_id
        ).first()
        if not message:
            raise NotFoundError("Message not found")

        existing = self._existing_reply(message_id)
        if existing:
            return {"status": "already_marked", "replied_at": existing.event_time}

        event = self._manual_event(message_id, EmailEvent.EVENT_REPLY, user_id)
        self._set_enrollment_status(campaign_id, message.prospect_id, "REPLIED")
        self.db.commit()
        return {"status": "success", "message_id": message_id, "replied_at": event.event_time}
