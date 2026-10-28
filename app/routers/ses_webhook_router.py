# app/routers/ses_webhook_router.py
"""
AWS SNS Webhook handlers for SES bounces, complaints, and delivery notifications.

HTTP concerns only: the body cap, JSON parsing and the authenticity check (SNS signature or shared
token, BR-DF-09). What an event does lives in app/services/ses_event_service.py.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Request, Depends, HTTPException, Header

from app.core.database import get_db
from app.core.config import settings
from app.core.errors import DomainError
from app.core.secrets_guard import is_deployed
from app.services.ses_event_service import SesEventService
from app.utils.sns_verify import SigningCertificateUnavailable, token_matches, verify_sns_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/ses", tags=["SES Webhooks"])

# SES/SNS notifications are a few KiB; anything over this is not one (and is not buffered whole)
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
_TOO_LARGE = "Payload too large"


def ses_event_service(db=Depends(get_db)) -> SesEventService:
    return SesEventService.for_session(db)


def _declared_length(request: Request) -> Optional[int]:
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    try:
        return int(declared)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")


async def _read_json_body(request: Request, limit: int = MAX_WEBHOOK_BODY_BYTES) -> object:
    """The request body as JSON, read at most `limit` bytes: 413 when larger, 400 when not JSON."""
    declared = _declared_length(request)
    if declared is not None and declared > limit:
        raise HTTPException(status_code=413, detail=_TOO_LARGE)
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail=_TOO_LARGE)
    try:
        return json.loads(bytes(buf))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")


async def _authenticate(request: Request, body: object) -> Optional[str]:
    """
    Authenticity (BR-DF-09): wrapped SNS messages must carry a valid AWS signature (403 when not, 503
    when the signing certificate cannot be fetched, so SNS retries). Raw-delivery messages have none, so
    they must present the shared SES_WEBHOOK_TOKEN. Returns the SNS message Type, if any.
    """
    if isinstance(body, dict) and body.get("Type"):
        try:
            valid = await verify_sns_signature(body, settings.AWS_SNS_TOPIC_ARN)
        except SigningCertificateUnavailable as exc:
            logger.warning("[SES-WEBHOOK] SNS signing certificate unavailable: %s", exc)
            raise HTTPException(status_code=503, detail="Signing certificate unavailable")
        if not valid:
            raise HTTPException(status_code=403, detail="Invalid SNS signature")
        return str(body.get("Type"))
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
    return None


@router.post("/notifications")
async def handle_ses_notification(
    request: Request,
    service: SesEventService = Depends(ses_event_service),
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
        # Fail closed: production must pin the SNS topic the events come from
        if settings.is_production and not (settings.AWS_SNS_TOPIC_ARN or "").strip():
            logger.error("[SES-WEBHOOK] Rejected: AWS_SNS_TOPIC_ARN is not configured in production")
            raise HTTPException(status_code=403, detail="Webhook not configured")
        body = await _read_json_body(request)
        message_type = await _authenticate(request, body) or x_amz_sns_message_type
        return await service.handle(message_type, body, request.headers.get("x-amz-sns-message-id"))

    except json.JSONDecodeError as e:
        logger.error(f"[SES-WEBHOOK] Invalid JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    except (HTTPException, DomainError):
        raise

    except Exception:
        # Details stay in the server log: the response must not echo internals (SQL, paths, values)
        logger.exception("[SES-WEBHOOK] Error processing notification")
        raise HTTPException(status_code=500, detail="Error processing notification")
