"""
SES event handling (BR-DF-04 / BR-DF-07 / BR-DF-08 / B09 / B10): what the SES/SNS webhook does with an
authenticated message.

``SesEventService.handle`` takes the message type and the parsed body (the router has already checked
the signature or token) and returns the response dict. Subscription confirmations are followed (SNS
URLs only), unsubscribe confirmations are logged, SES events are deduplicated and dispatched to one
handler per event kind. No sqlalchemy / fastapi imports: SQL lives in app/repositories/email_events.py;
the not-yet-migrated helpers (metrics, suppression, send safety, automation) get ``uow.session``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from app.core.clock import utcnow
from app.core.errors import BadRequestError
from app.core.observability import record_ses_event
from app.domain.automation import TriggerType
from app.domain.inbox import SYSTEM_NOTICE_SENDER, LabelSource
from app.domain.ses_events import (
    OUTCOME_STATUSES, dedupe_key, event_time, extract_tags, header_message_id, is_sender_attributable_bounce,
    notification_type_of, unwrap_ses_event,
)
from app.models.email_message import EmailEvent, EmailMessage
from app.repositories.email_events import Session, EmailEventsUnitOfWork, SqlAlchemyEmailEventsUnitOfWork
from app.services.automation_rule_service import automation_service
from app.services.domain_tenancy import tenant_for_message, tenant_for_sender
from app.services.send_safety import on_risk_event, set_final_status
from app.services.sequence_stop import stop_pending_steps
from app.services.suppression import suppress
from app.utils.email_utils import extract_sender_address, extract_sender_domain
from app.utils.sns_verify import is_sns_url

logger = logging.getLogger(__name__)

__all__ = ["SesEventService", "confirm_subscription"]


async def confirm_subscription(subscribe_url: Optional[str]) -> dict:
    """Visit an SNS SubscribeURL to confirm the subscription (only SNS URLs; 400 otherwise)."""
    if not is_sns_url(subscribe_url):
        raise BadRequestError("SubscribeURL is not an SNS URL")
    logger.info(f"[SES-WEBHOOK] SNS Subscription confirmation required: {subscribe_url}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(str(subscribe_url), timeout=10.0)
            if response.status_code == 200:
                logger.info("[SES-WEBHOOK] SNS Subscription confirmed successfully!")
                return {"status": "confirmed", "message": "Subscription confirmed"}
            logger.error(f"[SES-WEBHOOK] Failed to confirm subscription: {response.status_code}")
            return {"status": "error", "message": f"Confirmation failed: {response.status_code}"}
    except Exception:
        logger.exception("[SES-WEBHOOK] Error confirming subscription")
        return {"status": "error", "message": "Subscription confirmation failed"}


def _trigger_automation(session: Session, trigger: TriggerType, email_message: EmailMessage,
                        metadata: dict) -> None:
    if not (email_message.campaign_id and email_message.prospect_id):
        return
    try:
        automation_service.process_event(event_type=trigger, campaign_id=email_message.campaign_id,
                                         prospect_id=email_message.prospect_id, metadata=metadata, db=session)
    except Exception as e:
        logger.error(f"[SES-WEBHOOK] Automation trigger failed: {e}")


class SesEventService:
    def __init__(self, uow: EmailEventsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> SesEventService:
        return cls(SqlAlchemyEmailEventsUnitOfWork.for_session(db))

    # entry point -------------------------------------------------------------
    async def handle(self, message_type: Optional[str], body: Any, sns_message_id: Optional[str]) -> dict:
        """One authenticated webhook body. ``sns_message_id``: the x-amz-sns-message-id header."""
        if message_type == "SubscriptionConfirmation":
            return await confirm_subscription(body.get("SubscribeURL"))
        if message_type == "UnsubscribeConfirmation":
            logger.info("[SES-WEBHOOK] SNS UnsubscribeConfirmation for %s (no action)", body.get("TopicArn"))
            return {"status": "ignored", "type": "UnsubscribeConfirmation"}
        message = unwrap_ses_event(body)
        if message is None:
            return {"status": "ok"}
        envelope_id = body.get("MessageId") if isinstance(body, dict) and "Message" in body else None
        return self.process_event(message, envelope_id or sns_message_id)

    def process_event(self, message: dict, sns_message_id: Optional[str] = None) -> dict:
        """Record one SES event (once: SNS retries are recognised by their dedupe key)."""
        notification_type = notification_type_of(message)
        logger.info(f"[SES-WEBHOOK] Received {notification_type} notification")
        record_ses_event(notification_type)
        with self.uow as uow:
            key = dedupe_key(message, notification_type, sns_message_id)
            if key:
                if self._already_processed(uow, message.get("mail", {}) or {}, key):
                    logger.info(f"[SES-WEBHOOK] Duplicate {notification_type} event ignored ({key})")
                    return {"status": "duplicate", "type": notification_type}
                message["_dedupe_key"] = key
            handler = _HANDLERS.get(notification_type or "")
            if handler is None:
                logger.warning(f"[SES-WEBHOOK] Unknown notification type: {notification_type}")
                return {"status": "ignored", "type": notification_type}
            result: dict = getattr(self, handler)(uow, message)
            return result

    # lookups -----------------------------------------------------------------
    @staticmethod
    def _resolve_message_id(uow: EmailEventsUnitOfWork, mail_info: dict) -> Optional[str]:
        """
        Our message_id for an SES event (BR-DF-04): the message_id tag we attach on send, else SES's own
        messageId, which the scheduler stores on every send.
        """
        tags = extract_tags(mail_info)
        if tags.get("message_id"):
            return tags["message_id"]
        ses_id = mail_info.get("messageId")
        if not ses_id:
            return None
        found = uow.events.message_id_for_provider_id(ses_id)
        if not found:
            logger.warning(f"[SES-WEBHOOK] Event for unknown SES message {ses_id}")
        return found

    def _already_processed(self, uow: EmailEventsUnitOfWork, mail_info: dict, key: str) -> bool:
        internal_message_id = self._resolve_message_id(uow, mail_info)
        return bool(internal_message_id) and uow.events.has_event_with_key(str(internal_message_id), key)

    def _message_for(self, uow: EmailEventsUnitOfWork, mail_info: dict,
                     header_fallback: bool = False) -> tuple[Optional[str], Optional[EmailMessage]]:
        internal_message_id = self._resolve_message_id(uow, mail_info)
        if not internal_message_id and header_fallback:
            internal_message_id = header_message_id(mail_info)
        if not internal_message_id:
            return None, None
        return internal_message_id, uow.events.message(internal_message_id)

    @staticmethod
    def _tenants_for(uow: EmailEventsUnitOfWork, email_message: Optional[EmailMessage],
                     email: Optional[str]) -> list[str]:
        """The workspace an event belongs to: the message's, else every workspace that has this address."""
        if email_message is not None and email_message.prospect_id:
            tenant = uow.events.prospect_tenant(email_message.prospect_id)
            if tenant:
                return [tenant]
        if not email:
            return []
        return uow.events.tenants_with_email(email)

    # bounce --------------------------------------------------------------------
    def _soft_bounce(self, uow: EmailEventsUnitOfWork, message: dict, email_message: EmailMessage,
                     recipient: dict, ses_message_id: Optional[str], when: datetime) -> None:
        """Soft bounce: one "sender bounced" event, nothing else."""
        bounce = message.get("bounce", {})
        uow.events.add_event(email_message.message_id, EmailEvent.EVENT_SENDER_BOUNCE, {
            "dedupe_key": message.get("_dedupe_key"),
            "bounce_type": bounce.get("bounceType"),
            "bounce_subtype": bounce.get("bounceSubType"),
            "diagnostic_code": recipient.get("diagnosticCode"),
            "ses_message_id": ses_message_id,
        }, at=when)

    def _record_hard_bounce(self, uow: EmailEventsUnitOfWork, message: dict, email_message: EmailMessage,
                            recipient: dict, email: Optional[str], ses_message_id: Optional[str],
                            when: datetime) -> None:
        """A permanent bounce of one sent message: status, events, metrics and the inbox thread note, all
        stamped ``when`` (SES's bounce time)."""
        bounce = message.get("bounce", {})
        bounce_type, bounce_subtype = bounce.get("bounceType"), bounce.get("bounceSubType")
        email_message.status = "BOUNCED"
        set_final_status(email_message, "BOUNCED", when)
        email_message.failure_reason = f"{bounce_type}: {bounce_subtype}"
        email_message.last_error_code = f"BOUNCE_{bounce_type}"
        if email_message.campaign_id and email_message.prospect_id:
            # The campaign's remaining steps stop with the bounce reason, per step's stop_on_bounce
            # flag (before suppression, which would otherwise cancel them without it)
            stop_pending_steps(uow.session, email_message.campaign_id, email_message.prospect_id, "BOUNCED")
        detail = {
            "dedupe_key": message.get("_dedupe_key"),
            "bounce_type": bounce_type,
            "bounce_subtype": bounce_subtype,
            "diagnostic_code": recipient.get("diagnosticCode"),
        }
        uow.events.add_event(email_message.message_id, EmailEvent.EVENT_BOUNCE,
                             {**detail, "ses_message_id": ses_message_id}, at=when)
        # Sender-attributable bounces (soft/transient, or a permanent rejection citing our domain's
        # SPF/DKIM/DMARC/reputation) also count for the "Sender Bounced" metric: additive to
        # EVENT_BOUNCE, so total bounce_rate is unaffected.
        if is_sender_attributable_bounce(bounce_type, recipient.get("diagnosticCode")):
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_SENDER_BOUNCE, detail, at=when)
        if email_message.conversation_id:
            self._bounce_note(uow, email_message, recipient, bounce_type, bounce_subtype, when)

    @staticmethod
    def _bounce_note(uow: EmailEventsUnitOfWork, email_message: EmailMessage, recipient: dict,
                     bounce_type: Optional[str], bounce_subtype: Optional[str], when: datetime) -> None:
        """A visible INBOUND note in the conversation, so the bounce shows in the inbox thread. It is a system
        notice (app.domain.inbox.is_system_notice), not a reply: sent by MAILER-DAEMON, never classified."""
        diagnostic = recipient.get("diagnosticCode") or f"{bounce_type} - {bounce_subtype}"
        uow.events.add_message(
            campaign_id=email_message.campaign_id,
            prospect_id=email_message.prospect_id,
            inbox_id=email_message.inbox_id,
            conversation_id=email_message.conversation_id,
            direction="INBOUND",
            subject=f"Delivery Failed: {email_message.subject or 'Your email'}",
            body_text=(
                f"Email delivery failed ({bounce_type}).\n"
                f"Reason: {bounce_subtype}\n"
                f"Detail: {diagnostic}"
            ),
            to_email=email_message.from_email or "",
            from_email=SYSTEM_NOTICE_SENDER,
            status="SENT",
            sent_at=when,
            reply_label_source=LabelSource.SYSTEM.value,
            reply_classified_at=when,
        )
        conv = uow.events.conversation(str(email_message.conversation_id))
        if conv:
            conv.is_unread = True
            if conv.last_message_at is None or conv.last_message_at < when:
                conv.last_message_at = when

    def _bounce(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        bounce_info = message.get("bounce", {})
        bounce_type = bounce_info.get("bounceType")  # Permanent or Transient
        bounce_subtype = bounce_info.get("bounceSubType")
        mail_info = message.get("mail", {})
        ses_message_id = mail_info.get("messageId")
        source_email = extract_sender_address(mail_info.get("source"))
        sender_domain = extract_sender_domain(mail_info.get("source"))
        internal_message_id = self._resolve_message_id(uow, mail_info)
        # Only a Permanent bounce means the address is dead (B10). Transient/Undetermined bounces are
        # recorded as an event only: status, the contact, suppression and auto-pause are left alone.
        is_hard = bounce_type == "Permanent"
        when = event_time(message, "Bounce") or utcnow()
        processed_emails = []
        email_message = None
        for recipient in bounce_info.get("bouncedRecipients", []):
            email = recipient.get("emailAddress")
            processed_emails.append(email)
            email_message = uow.events.message(internal_message_id) if internal_message_id else None
            if email_message and not is_hard:
                self._soft_bounce(uow, message, email_message, recipient, ses_message_id, when)
            elif email_message:
                self._record_hard_bounce(uow, message, email_message, recipient, email, ses_message_id, when)
            if is_hard:
                # Dead address: suppressed and stopped in every campaign in the workspace (BR-DF-08)
                for tenant_id in self._tenants_for(uow, email_message, email):
                    suppress(uow.session, tenant_id, email, f"Hard bounce: {bounce_subtype}", kind="HARD_BOUNCE")
        uow.commit()
        if is_hard:
            # Auto-pause within seconds if this pushes the campaign or domain over the limits (BR-DF-07)
            on_risk_event(uow.session, email_message.campaign_id if email_message else None, sender_domain,
                          "hard bounce", tenant_id=tenant_for_message(uow.session, email_message)
                          or tenant_for_sender(uow.session, source_email))
        logger.info(f"[SES-WEBHOOK] Processed {bounce_type} bounce for {len(processed_emails)} recipients")
        if is_hard and email_message:
            _trigger_automation(uow.session, TriggerType.EMAIL_BOUNCED, email_message,
                                {"bounce_type": bounce_type})
        return {"status": "processed", "type": "bounce", "bounce_type": bounce_type, "emails": processed_emails}

    # complaint -----------------------------------------------------------------
    def _complaint(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        complaint_info = message.get("complaint", {})
        complaint_type = complaint_info.get("complaintFeedbackType")
        mail_info = message.get("mail", {})
        source_email = extract_sender_address(mail_info.get("source"))
        sender_domain = extract_sender_domain(mail_info.get("source"))
        internal_message_id = self._resolve_message_id(uow, mail_info)
        when = event_time(message, "Complaint") or utcnow()
        processed_emails = []
        email_message = None
        for recipient in complaint_info.get("complainedRecipients", []):
            email = recipient.get("emailAddress")
            processed_emails.append(email)
            if internal_message_id:
                email_message = uow.events.message(internal_message_id)
                if email_message:
                    email_message.status = "COMPLAINED"
                    set_final_status(email_message, "COMPLAINED", when)
                    email_message.failure_reason = f"Spam complaint: {complaint_type}"
                    uow.events.add_event(email_message.message_id, EmailEvent.EVENT_UNSUBSCRIBE, {
                        "dedupe_key": message.get("_dedupe_key"),
                        "complaint_type": complaint_type,
                        "source": "ses_complaint",
                    }, at=when)
            # A complaint unsubscribes the contact from every campaign, permanently (BR-DF-08)
            for tenant_id in self._tenants_for(uow, email_message if internal_message_id else None, email):
                suppress(uow.session, tenant_id, email, f"Spam complaint: {complaint_type or 'unknown'}",
                         kind="COMPLAINT")
        uow.commit()
        sent = email_message if internal_message_id else None
        on_risk_event(uow.session, sent.campaign_id if sent else None, sender_domain, "spam complaint",
                      tenant_id=tenant_for_message(uow.session, sent) or tenant_for_sender(uow.session, source_email))
        logger.warning(f"[SES-WEBHOOK] Processed complaint for {len(processed_emails)} recipients")
        return {"status": "processed", "type": "complaint", "emails": processed_emails}

    # delivery / engagement ----------------------------------------------------
    def _delivery(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        delivery_info = message.get("delivery", {})
        recipients = delivery_info.get("recipients", [])
        _, email_message = self._message_for(uow, message.get("mail", {}))
        # Confirmed delivery is recorded without touching `status` (reports count status == "SENT" as
        # sent) via delivered_at + an event. A Delivery that races ahead of the scheduler's commit
        # (status still SENDING) is recorded too; the scheduler then moves SENDING to SENT itself.
        if email_message and email_message.status in OUTCOME_STATUSES and not email_message.delivered_at:
            email_message.delivered_at = utcnow()
            set_final_status(email_message, "DELIVERED", email_message.delivered_at)
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_DELIVERED, {
                "dedupe_key": message.get("_dedupe_key"),
                "smtp_response": delivery_info.get("smtpResponse"),
                "processing_time_ms": delivery_info.get("processingTimeMillis"),
            })
            uow.commit()
        logger.info(f"[SES-WEBHOOK] Delivery confirmed for {len(recipients)} recipients")
        return {"status": "processed", "type": "delivery", "emails": recipients}

    def _open(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        open_info = message.get("open", {})
        internal_message_id, email_message = self._message_for(uow, message.get("mail", {}), header_fallback=True)
        if email_message:
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_OPEN, {
                "dedupe_key": message.get("_dedupe_key"),
                "ip_address": open_info.get("ipAddress"),
                "user_agent": open_info.get("userAgent"),
                "timestamp": open_info.get("timestamp"),
                "source": "ses_tracking",
            })
            uow.commit()
            logger.info(f"[SES-WEBHOOK] Open tracked for message {internal_message_id}")
            _trigger_automation(uow.session, TriggerType.EMAIL_OPENED, email_message,
                                {"user_agent": open_info.get("userAgent")})
            return {"status": "processed", "type": "open", "message_id": internal_message_id}
        logger.warning("[SES-WEBHOOK] Open event received but message not found")
        return {"status": "ignored", "type": "open", "reason": "message_not_found"}

    def _click(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        click_info = message.get("click", {})
        internal_message_id, email_message = self._message_for(uow, message.get("mail", {}), header_fallback=True)
        if email_message:
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_CLICK, {
                "dedupe_key": message.get("_dedupe_key"),
                "ip_address": click_info.get("ipAddress"),
                "user_agent": click_info.get("userAgent"),
                "link": click_info.get("link"),
                "link_tags": click_info.get("linkTags"),
                "timestamp": click_info.get("timestamp"),
                "source": "ses_tracking",
            })
            uow.commit()
            logger.info(f"[SES-WEBHOOK] Click tracked for message {internal_message_id}")
            _trigger_automation(uow.session, TriggerType.EMAIL_CLICKED, email_message,
                                {"link": click_info.get("link")})
            return {"status": "processed", "type": "click", "message_id": internal_message_id}
        logger.warning("[SES-WEBHOOK] Click event received but message not found")
        return {"status": "ignored", "type": "click", "reason": "message_not_found"}

    # failures ------------------------------------------------------------------
    def _reject(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        reject_info = message.get("reject", {})
        internal_message_id, email_message = self._message_for(uow, message.get("mail", {}))
        if email_message:
            email_message.status = "REJECTED"
            set_final_status(email_message, "REJECTED")
            email_message.failure_reason = f"Rejected: {reject_info.get('reason', 'virus detected')}"
            email_message.last_error_code = "REJECT_VIRUS"
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_BOUNCE, {
                "dedupe_key": message.get("_dedupe_key"),
                "reason": reject_info.get("reason"),
                "source": "ses_reject",
            })
            uow.commit()
            return {"status": "processed", "type": "reject", "message_id": internal_message_id}
        return {"status": "ignored", "type": "reject", "reason": "message_not_found"}

    def _delivery_delay(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        delay_info = message.get("deliveryDelay", {})
        internal_message_id, email_message = self._message_for(uow, message.get("mail", {}))
        if email_message:
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_SENT, {
                "dedupe_key": message.get("_dedupe_key"),
                "delay_type": delay_info.get("delayType", "UNKNOWN"),
                "expiration_time": delay_info.get("expirationTime"),
                "source": "ses_delivery_delay",
            })
            uow.commit()
            return {"status": "processed", "type": "delivery_delay", "message_id": internal_message_id}
        return {"status": "ignored", "type": "delivery_delay", "reason": "message_not_found"}

    def _rendering_failure(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        failure_info = message.get("failure", {})
        error_message = failure_info.get("errorMessage", "Template rendering failed")
        internal_message_id, email_message = self._message_for(uow, message.get("mail", {}))
        if email_message:
            email_message.status = "FAILED"
            set_final_status(email_message, "FAILED")
            email_message.failure_reason = f"Rendering failure: {error_message}"
            email_message.last_error_code = "RENDER_FAILURE"
            uow.commit()
            return {"status": "processed", "type": "rendering_failure", "message_id": internal_message_id,
                    "error": error_message}
        return {"status": "ignored", "type": "rendering_failure", "reason": "message_not_found"}

    def _subscription(self, uow: EmailEventsUnitOfWork, message: dict) -> dict:
        contact_list = message.get("subscription", {}).get("contactList")
        internal_message_id, email_message = self._message_for(uow, message.get("mail", {}))
        if email_message:
            uow.events.add_event(email_message.message_id, EmailEvent.EVENT_UNSUBSCRIBE, {
                "dedupe_key": message.get("_dedupe_key"),
                "contact_list": contact_list,
                "source": "ses_list_unsubscribe",
            })
            prospect = uow.events.prospect(email_message.prospect_id) if email_message.prospect_id else None
            if prospect:
                suppress(uow.session, prospect.tenant_id, prospect.email, "List-Unsubscribe header",
                         kind="UNSUBSCRIBE", source="list_unsubscribe")
            uow.commit()
            return {"status": "processed", "type": "subscription", "message_id": internal_message_id}
        return {"status": "ignored", "type": "subscription", "reason": "message_not_found"}


# SES event kind -> handler method (looked up by name, so a test can replace one method)
_HANDLERS: dict[str, str] = {
    "Bounce": "_bounce",
    "Complaint": "_complaint",
    "Delivery": "_delivery",
    "Open": "_open",
    "Click": "_click",
    "Reject": "_reject",
    "DeliveryDelay": "_delivery_delay",
    "Rendering Failure": "_rendering_failure",
    "Subscription": "_subscription",
}
