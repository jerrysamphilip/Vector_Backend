# app/routers/tracking_router.py
"""
Email tracking endpoints for open and click tracking.
"""

import base64
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models import EmailMessage, EmailEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tracking", tags=["Tracking"])

# 1x1 transparent GIF
TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


@router.get("/open/{message_id}.png")
def track_open(
    message_id: str,
    db: Session = Depends(get_db)
):
    """
    Track email opens via 1x1 transparent pixel.
    Always returns the pixel to not break email rendering.
    """
    try:
        # Find email message
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == message_id
        ).first()
        
        if email_message:
            # Create open event
            event = EmailEvent(
                message_id=message_id,
                event_type=EmailEvent.EVENT_OPEN,
                event_time=datetime.utcnow(),
                event_metadata={"source": "tracking_pixel"}
            )
            db.add(event)
            db.commit()
            
            logger.info(f"[TRACKING] Open tracked for message {message_id}")
        else:
            logger.warning(f"[TRACKING] Open: Message {message_id} not found")
            
    except Exception as e:
        logger.error(f"[TRACKING] Error tracking open: {e}")
        # Don't rollback - we still want to return the pixel
    
    # Always return the tracking pixel
    return Response(
        content=TRACKING_PIXEL,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@router.get("/click/{message_id}")
def track_click(
    message_id: str,
    url: str,
    db: Session = Depends(get_db)
):
    """
    Track email link clicks and redirect to original URL.
    """
    # Decode URL
    original_url = unquote_plus(url)
    
    try:
        # Find email message
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == message_id
        ).first()
        
        if email_message:
            # Create click event
            event = EmailEvent(
                message_id=message_id,
                event_type=EmailEvent.EVENT_CLICK,
                event_time=datetime.utcnow(),
                event_metadata={
                    "url": original_url,
                    "source": "click_tracking"
                }
            )
            db.add(event)
            db.commit()
            
            logger.info(f"[TRACKING] Click tracked for message {message_id}, URL: {original_url[:50]}...")
        else:
            logger.warning(f"[TRACKING] Click: Message {message_id} not found")
            
    except Exception as e:
        logger.error(f"[TRACKING] Error tracking click: {e}")
    
    # Always redirect to the original URL
    return RedirectResponse(url=original_url, status_code=302)


@router.get("/unsubscribe/{message_id}")
def track_unsubscribe(
    message_id: str,
    db: Session = Depends(get_db)
):
    """
    Handle unsubscribe link clicks.
    """
    try:
        email_message = db.query(EmailMessage).filter(
            EmailMessage.message_id == message_id
        ).first()
        
        if email_message:
            # Create unsubscribe event
            event = EmailEvent(
                message_id=message_id,
                event_type=EmailEvent.EVENT_UNSUBSCRIBE,
                event_time=datetime.utcnow()
            )
            db.add(event)
            
            # Update Prospect's consent status
            # Local import to avoid circular dependency
            from app.models.prospect import Prospect, GlobalUnsubscribe
            
            if email_message.prospect_id:
                prospect = db.query(Prospect).filter(Prospect.prospect_id == email_message.prospect_id).first()
                if prospect:
                    prospect.consent_status = "UNSUBSCRIBED"
                    prospect.consent_source = f"email_link:{message_id}"
                    prospect.consent_timestamp = datetime.utcnow()
                    db.add(prospect)
                    
                    # Add to Global Unsubscribe list with a suppression expiry date
                    if prospect.email and prospect.tenant_id:
                        # Calculate suppression expiry
                        now = datetime.utcnow()
                        if settings.UNSUBSCRIBE_SUPPRESSION_HOURS > 0:
                            suppression_expires = now + timedelta(hours=settings.UNSUBSCRIBE_SUPPRESSION_HOURS)
                        else:
                            suppression_expires = now + timedelta(days=settings.UNSUBSCRIBE_SUPPRESSION_DAYS)
                        
                        global_unsub = GlobalUnsubscribe(
                            tenant_id=prospect.tenant_id,
                            email=prospect.email,
                            unsubscribed_at=now,
                            suppression_expires_at=suppression_expires,
                            reason="User clicked unsubscribe link"
                        )
                        db.merge(global_unsub)  # merge handles insert or update
            
            db.commit()
            
            logger.info(f"[TRACKING] Unsubscribe tracked for message {message_id}")
            
            
            html_content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Unsubscribed</title>
                <style>
                    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background-color: #f8fafc; color: #334155; }
                    .card { background: white; padding: 2rem; border-radius: 12px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); text-align: center; max-width: 400px; width: 90%; }
                    h1 { color: #0f172a; margin-bottom: 0.5rem; font-size: 1.5rem; }
                    p { margin-top: 0; line-height: 1.5; }
                    .icon { color: #10b981; font-size: 3rem; margin-bottom: 1rem; }
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="icon">✓</div>
                    <h1>Unsubscribed successfully</h1>
                    <p>You have been removed from our mailing list. You will no longer receive emails from this campaign.</p>
                </div>
            </body>
            </html>
            """
            
            return HTMLResponse(content=html_content, status_code=200)
        else:
            return HTMLResponse(content="<h1>Invalid unsubscribe link</h1>", status_code=404)
            
    except Exception as e:
        logger.error(f"[TRACKING] Error tracking unsubscribe: {e}")
        return HTMLResponse(content="<h1>An error occurred. Please try again.</h1>", status_code=500)


@router.post("/unsubscribe/{message_id}")
def track_unsubscribe_post(
    message_id: str,
    db: Session = Depends(get_db)
):
    """
    Handle RFC 8058 one-click unsubscribe via POST.
    Email clients (Gmail, Yahoo, Outlook) send a POST request when the
    'Unsubscribe' button is clicked from the List-Unsubscribe-Post header.
    """
    return track_unsubscribe(message_id, db)
