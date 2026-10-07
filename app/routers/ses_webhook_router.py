
# app/routers/ses_webhook_router.py
"""
AWS SNS Webhook handlers for SES bounces, complaints, and delivery notifications.
"""

import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException, Header
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.models import EmailMessage, EmailEvent, Prospect, GlobalUnsubscribe
from app.models.campaign import CampaignProspect
from app.models.conversation import Conversation
from app.services.automation_rule_service import automation_service
from app.models.automation_rule import TriggerType
from app.services.metrics_service import MetricsService
from app.services.deliverability_service import deliverability_service
from app.utils.campaign_prospect_status import set_prospect_status
from app.core.secrets_guard import is_deployed
from app.utils.sns_verify import is_sns_url, token_matches, verify_sns_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/ses", tags=["SES Webhooks"])


# Helper for Tag Extraction
def _extract_tags(mail_info: dict) -> dict:
    """
    Extract tags from SES mail object.
    
    Handles two formats:
    1. Dict of Lists (Standard SNS): {"tagName": ["value"]}
    2. List of Dicts (Configuration Sets): [{"name": "tagName", "value": "value"}]
    """
    tags_raw = mail_info.get("tags", {})
    
    if isinstance(tags_raw, dict):
        # Format 1: Dict of Lists -> Return flat dict using first value
        return {k: v[0] if isinstance(v, list) and v else v for k, v in tags_raw.items()}
    
    elif isinstance(tags_raw, list):
        # Format 2: List of Dicts
        return {tag.get("name"): tag.get("value") for tag in tags_raw if "name" in tag}
        
    return {}


# Diagnostic-code substrings indicating the recipient's server rejected the
# message because of OUR sending domain (auth/reputation), not because the
# recipient address itself is bad.
_SENDER_FAULT_DIAGNOSTIC_KEYWORDS = (
    "spf", "dkim", "dmarc", "not authorized", "unauthenticated",
    "blocked", "blacklist", "block list", "reputation",
    "5.7.1", "5.7.25", "5.7.26", "5.7.27",
)


def _is_sender_attributable_bounce(bounce_type: str, diagnostic_code: str) -> bool:
    """
    True when a bounce points at a problem with OUR sending side rather than a
    genuinely bad/nonexistent recipient address:
      - Transient bounces (soft/4xx-style — rate limiting, greylisting,
        temporary block) are, by definition, not "this address doesn't exist".
      - Permanent bounces whose diagnostic text names our domain's
        SPF/DKIM/DMARC/reputation as the rejection reason.
    """
    if (bounce_type or "").lower() == "transient":
        return True
    diagnostic = (diagnostic_code or "").lower()
    return any(keyword in diagnostic for keyword in _SENDER_FAULT_DIAGNOSTIC_KEYWORDS)


@router.post("/notifications")
async def handle_ses_notification(
    request: Request,
    db: Session = Depends(get_db),
    x_amz_sns_message_type: str = Header(None, alias="x-amz-sns-message-type")
):
    """
    Handle SES notifications via SNS (bounces, complaints, deliveries).
    
    Flow:
    1. SES sends event to SNS Topic
    2. SNS forwards to this webhook
    3. We process and update email status
    """
    try:
        body = await request.json()

        # ── Authenticity (BR-DF-09) ──
        # Wrapped SNS messages must carry a valid AWS signature. Raw-delivery messages have
        # none, so they must present the shared SES_WEBHOOK_TOKEN instead.
        if isinstance(body, dict) and body.get("Type"):
            if not await verify_sns_signature(body, settings.AWS_SNS_TOPIC_ARN):
                raise HTTPException(status_code=403, detail="Invalid SNS signature")
            x_amz_sns_message_type = body.get("Type")
        else:
            presented = request.query_params.get("token") or request.headers.get("x-webhook-token")
            if settings.SES_WEBHOOK_TOKEN:
                if not token_matches(presented, settings.SES_WEBHOOK_TOKEN):
                    raise HTTPException(status_code=403, detail="Invalid webhook token")
            elif is_deployed():
                logger.error("[SES-WEBHOOK] Rejected raw SES event: SES_WEBHOOK_TOKEN is not configured. "
                             "Set it and add ?token=<value> to the SNS subscription URL.")
                raise HTTPException(status_code=403, detail="Webhook token not configured")
            else:
                logger.warning("[SES-WEBHOOK] Accepting unauthenticated raw event (local run, no SES_WEBHOOK_TOKEN)")

        # Handle SNS subscription confirmation
        if x_amz_sns_message_type == "SubscriptionConfirmation":
            subscribe_url = body.get("SubscribeURL")
            if not is_sns_url(subscribe_url):
                raise HTTPException(status_code=400, detail="SubscribeURL is not an SNS URL")
            logger.info(f"[SES-WEBHOOK] SNS Subscription confirmation required: {subscribe_url}")
            
            # AUTO-CONFIRM: Visit the URL to confirm the subscription
            import httpx
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(subscribe_url, timeout=10.0)
                    if response.status_code == 200:
                        logger.info("[SES-WEBHOOK] SNS Subscription confirmed successfully!")
                        return {"status": "confirmed", "message": "Subscription confirmed"}
                    else:
                        logger.error(f"[SES-WEBHOOK] Failed to confirm subscription: {response.status_code}")
                        return {"status": "error", "message": f"Confirmation failed: {response.status_code}"}
            except Exception as e:
                logger.error(f"[SES-WEBHOOK] Error confirming subscription: {e}")
                return {"status": "error", "message": str(e)}
        
        # Handle notification.
        #
        # The SNS subscription behind this webhook has RawMessageDelivery
        # enabled, which means SNS delivers the underlying SES event directly
        # as the POST body instead of wrapping it in the standard
        # {"Type": "Notification", "Message": "<json string>"} envelope.
        # Detect whichever shape actually arrived instead of assuming the
        # wrapped one — assuming wrong means `body.get("Message")` is always
        # missing, `message` ends up `{}`, and every single notification gets
        # silently dropped (200 OK returned, so SNS never retries or errors,
        # and nothing shows up in logs as broken).
        message = None
        if isinstance(body, dict) and "Message" in body:
            # Standard (non-raw) SNS delivery — the SES event is JSON-encoded
            # inside the "Message" field.
            try:
                message = json.loads(body.get("Message", "{}"))
            except (TypeError, json.JSONDecodeError):
                message = None
        elif isinstance(body, dict) and ("notificationType" in body or "eventType" in body):
            # Raw delivery — the body IS the SES event already.
            message = body

        if message is not None:
            # SES's older Notification API uses "notificationType"; the newer
            # Configuration-Set Event Publishing API uses "eventType" for
            # event kinds that didn't exist in the legacy format (Send,
            # Reject, Open, Click, RenderingFailure, DeliveryDelay). Bounce/
            # Complaint/Delivery carry both for backward compatibility.
            notification_type = message.get("notificationType") or message.get("eventType")

            logger.info(f"[SES-WEBHOOK] Received {notification_type} notification")
            
            if notification_type == "Bounce":
                return await _handle_bounce(message, db)
            
            elif notification_type == "Complaint":
                return await _handle_complaint(message, db)
            
            elif notification_type == "Delivery":
                return await _handle_delivery(message, db)
            
            elif notification_type == "Open":
                return await _handle_open(message, db)
            
            elif notification_type == "Click":
                return await _handle_click(message, db)
            
            elif notification_type == "Reject":
                return await _handle_reject(message, db)
            
            elif notification_type == "DeliveryDelay":
                return await _handle_delivery_delay(message, db)
            
            elif notification_type == "Rendering Failure":
                return await _handle_rendering_failure(message, db)
            
            elif notification_type == "Subscription":
                return await _handle_subscription(message, db)
            
            else:
                logger.warning(f"[SES-WEBHOOK] Unknown notification type: {notification_type}")
                return {"status": "ignored", "type": notification_type}
        
        return {"status": "ok"}
        
    except json.JSONDecodeError as e:
        logger.error(f"[SES-WEBHOOK] Invalid JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    except HTTPException:
        raise
    
    except Exception as e:
        logger.error(f"[SES-WEBHOOK] Error processing notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _handle_bounce(message: dict, db: Session) -> dict:
    """
    Handle SES bounce notification.
    """
    bounce_info = message.get("bounce", {})
    bounce_type = bounce_info.get("bounceType")  # Permanent or Transient
    bounce_subtype = bounce_info.get("bounceSubType")
    bounced_recipients = bounce_info.get("bouncedRecipients", [])
    
    # Get message ID from mail headers
    mail_info = message.get("mail", {})
    ses_message_id = mail_info.get("messageId")
    source_email = (mail_info.get("source") or "").lower()
    sender_domain = source_email.split("@", 1)[1] if "@" in source_email else None
    
    # Extract tags using helper
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    processed_emails = []
    
    for recipient in bounced_recipients:
        email = recipient.get("emailAddress")
        processed_emails.append(email)
        
        # Find and update email message
        email_message = None
        if internal_message_id:
            email_message = db.query(EmailMessage).filter(
                EmailMessage.message_id == internal_message_id
            ).first()
        
        if email_message:
            # Update message status
            email_message.status = "BOUNCED"
            email_message.failure_reason = f"{bounce_type}: {bounce_subtype}"
            email_message.last_error_code = f"BOUNCE_{bounce_type}"

            # Create bounce event
            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_BOUNCE,
                event_time=datetime.utcnow(),
                event_metadata={
                    "bounce_type": bounce_type,
                    "bounce_subtype": bounce_subtype,
                    "diagnostic_code": recipient.get("diagnosticCode"),
                    "ses_message_id": ses_message_id
                }
            )
            db.add(event)

            # Additionally tag sender-attributable bounces (soft/transient, or
            # a permanent rejection citing our domain's SPF/DKIM/DMARC/reputation)
            # for the "Sender Bounced" metric — additive to EVENT_BOUNCE, not a
            # replacement, so total bounce_rate is unaffected.
            if _is_sender_attributable_bounce(bounce_type, recipient.get("diagnosticCode")):
                db.add(EmailEvent(
                    message_id=email_message.message_id,
                    event_type=EmailEvent.EVENT_SENDER_BOUNCE,
                    event_time=datetime.utcnow(),
                    event_metadata={
                        "bounce_type": bounce_type,
                        "bounce_subtype": bounce_subtype,
                        "diagnostic_code": recipient.get("diagnosticCode"),
                    }
                ))
                metrics_service = MetricsService(db)
                metrics_service.process_event(email_message.campaign_id, EmailEvent.EVENT_SENDER_BOUNCE)

            # Inject a visible INBOUND notification into the conversation so the
            # bounce appears in the campaign inbox thread (not just analytics).
            if email_message.conversation_id:
                diagnostic = recipient.get("diagnosticCode") or f"{bounce_type} - {bounce_subtype}"
                bounce_msg = EmailMessage(
                    message_id=str(uuid.uuid4()),
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
                    from_email=email or "",
                    status="SENT",
                    sent_at=datetime.utcnow(),
                )
                db.add(bounce_msg)

                conv = db.query(Conversation).filter(
                    Conversation.id == email_message.conversation_id
                ).first()
                if conv:
                    conv.is_unread = True
                    conv.last_message_at = datetime.utcnow()

            # Update metrics
            metrics_service = MetricsService(db)
            metrics_service.process_event(email_message.campaign_id, EmailEvent.EVENT_BOUNCE)
        
        # For PERMANENT bounces, add to global unsubscribe and mark the
        # prospect's status in THIS campaign as BOUNCED — a hard bounce means
        # the address is dead, so it stops the sequence the same way a reply
        # or unsubscribe would. (Transient/soft bounces don't change status:
        # the address may still be reachable on a later attempt.)
        if bounce_type == "Permanent":
            prospect = db.query(Prospect).filter(
                Prospect.email == email
            ).first()

            if prospect:
                existing = db.query(GlobalUnsubscribe).filter(
                    GlobalUnsubscribe.tenant_id == prospect.tenant_id,
                    GlobalUnsubscribe.email == email
                ).first()

                if not existing:
                    unsubscribe = GlobalUnsubscribe(
                        tenant_id=prospect.tenant_id,
                        email=email,
                        reason=f"Hard bounce: {bounce_subtype}"
                    )
                    db.add(unsubscribe)
                    logger.warning(f"[SES-WEBHOOK] Added {email} to global unsubscribe (hard bounce)")

                if email_message and email_message.campaign_id:
                    campaign_prospect = db.query(CampaignProspect).filter(
                        CampaignProspect.campaign_id == email_message.campaign_id,
                        CampaignProspect.prospect_id == prospect.prospect_id
                    ).first()
                    set_prospect_status(
                        campaign_prospect,
                        "BOUNCED",
                        stopped_reason=f"Hard bounce: {bounce_subtype}"
                    )
    
    db.commit()

    if sender_domain:
        try:
            deliverability_service.update_domain_stats(sender_domain, "BOUNCE", db)
        except Exception as e:
            logger.error(f"[SES-WEBHOOK] Failed to update domain stats for bounce ({sender_domain}): {e}")
    
    logger.info(f"[SES-WEBHOOK] Processed {bounce_type} bounce for {len(processed_emails)} recipients")
    
    # TRIGGER AUTOMATION
    if email_message and email_message.campaign_id and email_message.prospect_id:
        try:
            automation_service.process_event(
                event_type=TriggerType.EMAIL_BOUNCED,
                campaign_id=email_message.campaign_id,
                prospect_id=email_message.prospect_id,
                metadata={"bounce_type": bounce_type},
                db=db
            )
        except Exception as e:
            logger.error(f"[SES-WEBHOOK] Automation trigger failed: {e}")

    return {
        "status": "processed",
        "type": "bounce",
        "bounce_type": bounce_type,
        "emails": processed_emails
    }


async def _handle_complaint(message: dict, db: Session) -> dict:
    """
    Handle SES complaint notification.
    """
    complaint_info = message.get("complaint", {})
    complaint_type = complaint_info.get("complaintFeedbackType")
    complained_recipients = complaint_info.get("complainedRecipients", [])
    
    mail_info = message.get("mail", {})
    source_email = (mail_info.get("source") or "").lower()
    sender_domain = source_email.split("@", 1)[1] if "@" in source_email else None
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    processed_emails = []
    
    for recipient in complained_recipients:
        email = recipient.get("emailAddress")
        processed_emails.append(email)
        
        if internal_message_id:
            email_message = db.query(EmailMessage).filter(
                EmailMessage.message_id == internal_message_id
            ).first()
            
            if email_message:
                email_message.status = "COMPLAINED"
                email_message.failure_reason = f"Spam complaint: {complaint_type}"
                
                event = EmailEvent(
                    message_id=email_message.message_id,
                    event_type=EmailEvent.EVENT_UNSUBSCRIBE,
                    event_time=datetime.utcnow(),
                    event_metadata={
                        "complaint_type": complaint_type,
                        "source": "ses_complaint"
                    }
                )
                db.add(event)
        
        prospect = db.query(Prospect).filter(
            Prospect.email == email
        ).first()
        
        if prospect:
            existing = db.query(GlobalUnsubscribe).filter(
                GlobalUnsubscribe.tenant_id == prospect.tenant_id,
                GlobalUnsubscribe.email == email
            ).first()
            
            if not existing:
                unsubscribe = GlobalUnsubscribe(
                    tenant_id=prospect.tenant_id,
                    email=email,
                    reason=f"Spam complaint: {complaint_type or 'unknown'}"
                )
                db.add(unsubscribe)
                logger.warning(f"[SES-WEBHOOK] Added {email} to global unsubscribe (complaint)")
            
            prospect.consent_status = "UNSUBSCRIBED"
    
    db.commit()
    if sender_domain:
        try:
            deliverability_service.update_domain_stats(sender_domain, "COMPLAINT", db)
        except Exception as e:
            logger.error(f"[SES-WEBHOOK] Failed to update domain stats for complaint ({sender_domain}): {e}")
    logger.warning(f"[SES-WEBHOOK] Processed complaint for {len(processed_emails)} recipients")
    return {"status": "processed", "type": "complaint", "emails": processed_emails}


async def _handle_delivery(message: dict, db: Session) -> dict:
    """
    Handle SES delivery confirmation.
    """
    mail_info = message.get("mail", {})
    delivery_info = message.get("delivery", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    recipients = delivery_info.get("recipients", [])
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()

        # Record confirmed delivery without touching `status` — many existing
        # reports/metrics filter on status == "SENT" as "successfully sent",
        # so delivery confirmation is tracked via delivered_at + an event
        # instead of transitioning status away from SENT.
        if email_message and email_message.status == "SENT" and not email_message.delivered_at:
            email_message.delivered_at = datetime.utcnow()

            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_DELIVERED,
                event_time=datetime.utcnow(),
                event_metadata={
                    "smtp_response": delivery_info.get("smtpResponse"),
                    "processing_time_ms": delivery_info.get("processingTimeMillis")
                }
            )
            db.add(event)
            db.commit()
    
    logger.info(f"[SES-WEBHOOK] Delivery confirmed for {len(recipients)} recipients")
    return {"status": "processed", "type": "delivery", "emails": recipients}


async def _handle_open(message: dict, db: Session) -> dict:
    """
    Handle SES open notification.
    """
    mail_info = message.get("mail", {})
    open_info = message.get("open", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    if not internal_message_id:
        headers = {h["name"]: h["value"] for h in mail_info.get("headers", [])}
        internal_message_id = headers.get("X-Message-Id")
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()
        
        if email_message:
            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_OPEN,
                event_time=datetime.utcnow(),
                event_metadata={
                    "ip_address": open_info.get("ipAddress"),
                    "user_agent": open_info.get("userAgent"),
                    "timestamp": open_info.get("timestamp"),
                    "source": "ses_tracking"
                }
            )
            db.add(event)
            db.commit()
            
            logger.info(f"[SES-WEBHOOK] Open tracked for message {internal_message_id}")
            
            # TRIGGER AUTOMATION
            if email_message.campaign_id and email_message.prospect_id:
                try:
                    automation_service.process_event(
                        event_type=TriggerType.EMAIL_OPENED,
                        campaign_id=email_message.campaign_id,
                        prospect_id=email_message.prospect_id,
                        metadata={"user_agent": open_info.get("userAgent")},
                        db=db
                    )
                except Exception as e:
                    logger.error(f"[SES-WEBHOOK] Automation trigger failed: {e}")

            return {"status": "processed", "type": "open", "message_id": internal_message_id}
    
    logger.warning(f"[SES-WEBHOOK] Open event received but message not found")
    return {"status": "ignored", "type": "open", "reason": "message_not_found"}


async def _handle_click(message: dict, db: Session) -> dict:
    """
    Handle SES click notification.
    """
    mail_info = message.get("mail", {})
    click_info = message.get("click", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    if not internal_message_id:
        headers = {h["name"]: h["value"] for h in mail_info.get("headers", [])}
        internal_message_id = headers.get("X-Message-Id")
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()
        
        if email_message:
            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_CLICK,
                event_time=datetime.utcnow(),
                event_metadata={
                    "ip_address": click_info.get("ipAddress"),
                    "user_agent": click_info.get("userAgent"),
                    "link": click_info.get("link"),
                    "link_tags": click_info.get("linkTags"),
                    "timestamp": click_info.get("timestamp"),
                    "source": "ses_tracking"
                }
            )
            db.add(event)
            db.commit()
            
            logger.info(f"[SES-WEBHOOK] Click tracked for message {internal_message_id}")
            
            # TRIGGER AUTOMATION
            if email_message.campaign_id and email_message.prospect_id:
                try:
                    automation_service.process_event(
                        event_type=TriggerType.EMAIL_CLICKED,
                        campaign_id=email_message.campaign_id,
                        prospect_id=email_message.prospect_id,
                        metadata={"link": click_info.get("link")},
                        db=db
                    )
                except Exception as e:
                    logger.error(f"[SES-WEBHOOK] Automation trigger failed: {e}")

            return {"status": "processed", "type": "click", "message_id": internal_message_id}
    
    logger.warning(f"[SES-WEBHOOK] Click event received but message not found")
    return {"status": "ignored", "type": "click", "reason": "message_not_found"}


async def _handle_reject(message: dict, db: Session) -> dict:
    """
    Handle SES reject notification.
    """
    mail_info = message.get("mail", {})
    reject_info = message.get("reject", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()
        
        if email_message:
            email_message.status = "REJECTED"
            email_message.failure_reason = f"Rejected: {reject_info.get('reason', 'virus detected')}"
            email_message.last_error_code = "REJECT_VIRUS"
            
            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_BOUNCE,
                event_time=datetime.utcnow(),
                event_metadata={
                    "reason": reject_info.get("reason"),
                    "source": "ses_reject"
                }
            )
            db.add(event)
            db.commit()
            return {"status": "processed", "type": "reject", "message_id": internal_message_id}
    
    return {"status": "ignored", "type": "reject", "reason": "message_not_found"}


async def _handle_delivery_delay(message: dict, db: Session) -> dict:
    """
    Handle SES delivery delay notification.
    """
    mail_info = message.get("mail", {})
    delay_info = message.get("deliveryDelay", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    delayed_recipients = delay_info.get("delayedRecipients", [])
    delay_type = delay_info.get("delayType", "UNKNOWN")
    expiration_time = delay_info.get("expirationTime")
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()
        
        if email_message:
            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_SENT,
                event_time=datetime.utcnow(),
                event_metadata={
                    "delay_type": delay_type,
                    "expiration_time": expiration_time,
                    "source": "ses_delivery_delay"
                }
            )
            db.add(event)
            db.commit()
            
            return {
                "status": "processed",
                "type": "delivery_delay",
                "message_id": internal_message_id
            }
    
    return {"status": "ignored", "type": "delivery_delay", "reason": "message_not_found"}


async def _handle_rendering_failure(message: dict, db: Session) -> dict:
    """
    Handle SES rendering failure notification.
    """
    mail_info = message.get("mail", {})
    failure_info = message.get("failure", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    error_message = failure_info.get("errorMessage", "Template rendering failed")
    template_name = failure_info.get("templateName", "unknown")
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()
        
        if email_message:
            email_message.status = "FAILED"
            email_message.failure_reason = f"Rendering failure: {error_message}"
            email_message.last_error_code = "RENDER_FAILURE"
            db.commit()
            
            return {
                "status": "processed",
                "type": "rendering_failure",
                "message_id": internal_message_id,
                "error": error_message
            }
    
    return {"status": "ignored", "type": "rendering_failure", "reason": "message_not_found"}


async def _handle_subscription(message: dict, db: Session) -> dict:
    """
    Handle SES subscription notification.
    """
    mail_info = message.get("mail", {})
    subscription_info = message.get("subscription", {})
    
    tags = _extract_tags(mail_info)
    internal_message_id = tags.get("message_id")
    
    contact_list = subscription_info.get("contactList")
    
    if internal_message_id:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == internal_message_id
        ).first()
        
        if email_message:
            event = EmailEvent(
                message_id=email_message.message_id,
                event_type=EmailEvent.EVENT_UNSUBSCRIBE,
                event_time=datetime.utcnow(),
                event_metadata={
                    "contact_list": contact_list,
                    "source": "ses_list_unsubscribe"
                }
            )
            db.add(event)
            
            if email_message.prospect_id:
                prospect = db.query(Prospect).filter(
                    Prospect.prospect_id == email_message.prospect_id
                ).first()
                if prospect:
                    prospect.consent_status = "UNSUBSCRIBED"
                    existing = db.query(GlobalUnsubscribe).filter(
                        GlobalUnsubscribe.tenant_id == prospect.tenant_id,
                        GlobalUnsubscribe.email == prospect.email
                    ).first()
                    if not existing:
                        db.add(GlobalUnsubscribe(
                            tenant_id=prospect.tenant_id,
                            email=prospect.email,
                            reason="List-Unsubscribe header"
                        ))
            
            db.commit()
            return {"status": "processed", "type": "subscription", "message_id": internal_message_id}
    
    return {"status": "ignored", "type": "subscription", "reason": "message_not_found"}
