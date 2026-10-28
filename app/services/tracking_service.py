"""
Public email tracking (open pixel, click redirect, unsubscribe; SEC-12, BR-DF-08).

Reached without a user session from links in sent emails, so every call starts from the opaque
message id. Tracking never breaks the email: open/click recording errors are logged and swallowed.
No sqlalchemy / fastapi imports: SQL lives in app/repositories/email_events.py.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote_plus, unquote_plus

from app.core.security import verify_tracking_url
from app.models.email_message import EmailEvent, EmailMessage
from app.repositories.email_events import EmailEventsUnitOfWork, Session, SqlAlchemyEmailEventsUnitOfWork
from app.services.suppression import suppress

logger = logging.getLogger(__name__)

__all__ = ["TrackingService", "url_in_email"]


def url_in_email(email_message: Optional[EmailMessage], url: str) -> bool:
    """True when this exact destination is a link in the email we sent (legacy unsigned links)."""
    if not email_message or not url:
        return False
    for body in (getattr(email_message, "body_html", None), email_message.body_text):
        if body and (url in body or quote_plus(url) in body):
            return True
    return False


class TrackingService:
    def __init__(self, uow: EmailEventsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> TrackingService:
        return cls(SqlAlchemyEmailEventsUnitOfWork.for_session(db))

    def record_open(self, message_id: str) -> None:
        """An open event for a known message; errors are logged (the pixel is served regardless)."""
        try:
            with self.uow as uow:
                if uow.events.message(message_id):
                    uow.events.add_event(message_id, EmailEvent.EVENT_OPEN, {"source": "tracking_pixel"})
                    uow.commit()
                    logger.info(f"[TRACKING] Open tracked for message {message_id}")
                else:
                    logger.warning(f"[TRACKING] Open: Message {message_id} not found")
        except Exception as e:
            logger.error(f"[TRACKING] Error tracking open: {e}")

    def record_click(self, message_id: str, url: str, destination: str, sig: Optional[str]) -> bool:
        """
        Record a click; True when the destination may be redirected to automatically: the link is
        signed (HMAC over message id + url) or appears in the tracked email.
        """
        in_email = False
        try:
            with self.uow as uow:
                email_message = uow.events.message(message_id)
                if email_message:
                    in_email = url_in_email(email_message, url) or url_in_email(email_message, unquote_plus(url))
                    uow.events.add_event(message_id, EmailEvent.EVENT_CLICK, {
                        "url": destination,
                        "signed": bool(sig),
                        "source": "click_tracking",
                    })
                    uow.commit()
                    logger.info(f"[TRACKING] Click tracked for message {message_id}, URL: {destination[:50]}...")
                else:
                    logger.warning(f"[TRACKING] Click: Message {message_id} not found")
        except Exception as e:
            logger.error(f"[TRACKING] Error tracking click: {e}")
        return verify_tracking_url(message_id, url, sig) or in_email

    def message_exists(self, message_id: str) -> bool:
        with self.uow as uow:
            return uow.events.message(message_id) is not None

    def unsubscribe(self, message_id: str, one_click: bool) -> bool:
        """Unsubscribe the message's contact from every campaign in the workspace (BR-DF-08).

        False when the message does not exist.
        """
        source = "one_click" if one_click else "confirmation_page"
        with self.uow as uow:
            email_message = uow.events.message(message_id)
            if not email_message:
                return False
            uow.events.add_event(message_id, EmailEvent.EVENT_UNSUBSCRIBE, {"source": source})
            prospect = uow.events.prospect(email_message.prospect_id) if email_message.prospect_id else None
            if prospect and prospect.email and prospect.tenant_id:
                reason = "One-click unsubscribe (List-Unsubscribe)" if one_click else "User clicked unsubscribe link"
                suppress(uow.session, prospect.tenant_id, prospect.email, reason,
                         kind="UNSUBSCRIBE", source=f"email_link:{message_id}")
            uow.commit()
            logger.info(f"[TRACKING] Unsubscribe tracked for message {message_id} ({source})")
            return True
