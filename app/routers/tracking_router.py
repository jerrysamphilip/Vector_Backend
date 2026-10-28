# app/routers/tracking_router.py
"""
Email tracking endpoints for open and click tracking.
"""

import base64
import html
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Response
from fastapi.responses import RedirectResponse, HTMLResponse

from app.core.database import get_db
from app.services.tracking_service import TrackingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tracking", tags=["Tracking"])


def tracking_service(db=Depends(get_db)) -> TrackingService:
    return TrackingService.for_session(db)

# 1x1 transparent GIF
TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


@router.get("/open/{message_id}.png")
def track_open(
    message_id: str,
    service: TrackingService = Depends(tracking_service)
):
    """
    Track email opens via 1x1 transparent pixel.
    Always returns the pixel to not break email rendering.
    """
    service.record_open(message_id)

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


_PAGE_STYLE = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background-color: #f8fafc; color: #334155; }
    .card { background: white; padding: 2rem; border-radius: 12px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); text-align: center; max-width: 420px; width: 90%; overflow-wrap: anywhere; }
    h1 { color: #0f172a; margin-bottom: 0.5rem; font-size: 1.5rem; }
    p { margin-top: 0; line-height: 1.5; }
    .icon { color: #10b981; font-size: 3rem; margin-bottom: 1rem; }
    .btn { display: inline-block; background: #0f172a; color: white; border: 0; padding: 0.7rem 1.4rem; border-radius: 8px; font-size: 1rem; cursor: pointer; text-decoration: none; }
"""


def _page(title: str, inner: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            f"<title>{html.escape(title)}</title><style>{_PAGE_STYLE}</style></head>"
            f"<body><div class=\"card\">{inner}</div></body></html>"
        ),
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/click/{message_id}")
def track_click(
    message_id: str,
    url: str,
    sig: Optional[str] = None,
    service: TrackingService = Depends(tracking_service)
):
    """
    Track email link clicks and redirect to the original URL.

    Only signed links (HMAC over message id + url, added when the link was wrapped) or
    links that appear in the tracked email are redirected automatically (SEC-12). Any
    other http(s) destination gets a "you are leaving" page instead of a 302, so this
    endpoint can't be used as an open redirect; other schemes are refused.
    """
    # `url` is already query-decoded by FastAPI and is exactly what was signed
    destination = html.unescape(url)
    parsed = urlparse(destination)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return _page("Invalid link", "<h1>Invalid link</h1><p>This link cannot be opened.</p>", 400)

    if service.record_click(message_id, url, destination, sig):
        return RedirectResponse(url=destination, status_code=302)

    host = html.escape(parsed.hostname or parsed.netloc)
    return _page(
        "Leaving this site",
        f"<h1>You are leaving to {host}</h1>"
        f"<p>This link was not recognised. Only continue if you trust this site.</p>"
        f"<p><a class=\"btn\" rel=\"noopener noreferrer nofollow\" href=\"{html.escape(destination, quote=True)}\">"
        f"Continue to {host}</a></p>",
    )


_INVALID_UNSUBSCRIBE = "<h1>Invalid unsubscribe link</h1>"


@router.get("/unsubscribe/{message_id}")
def track_unsubscribe(
    message_id: str,
    service: TrackingService = Depends(tracking_service)
):
    """
    Unsubscribe link from the email body. GET never changes anything (link scanners and
    prefetchers follow GETs); it shows a confirmation page whose button POSTs back here.
    """
    try:
        found = service.message_exists(message_id)
    except Exception as e:
        logger.error(f"[TRACKING] Error loading unsubscribe page: {e}")
        return HTMLResponse(content="<h1>An error occurred. Please try again.</h1>", status_code=500)

    if not found:
        return HTMLResponse(content=_INVALID_UNSUBSCRIBE, status_code=404)

    # An empty form action posts back to this same URL, whatever path prefix the proxy uses
    return _page(
        "Unsubscribe",
        "<h1>Unsubscribe?</h1>"
        "<p>Confirm that you no longer wish to receive these emails.</p>"
        "<form method=\"post\" action=\"\">"
        "<input type=\"hidden\" name=\"confirm\" value=\"1\">"
        "<button class=\"btn\" type=\"submit\">Unsubscribe</button>"
        "</form>",
    )


@router.post("/unsubscribe/{message_id}")
def track_unsubscribe_post(
    message_id: str,
    list_unsubscribe: Optional[str] = Form(None, alias="List-Unsubscribe"),
    confirm: Optional[str] = Form(None),
    service: TrackingService = Depends(tracking_service)
):
    """
    Perform the unsubscribe: the confirmation page's button, or RFC 8058 one-click
    (mail clients POST "List-Unsubscribe=One-Click" from the List-Unsubscribe-Post header).
    """
    one_click = (list_unsubscribe or "").strip().lower() == "one-click"
    try:
        if not service.unsubscribe(message_id, one_click):
            return HTMLResponse(content=_INVALID_UNSUBSCRIBE, status_code=404)
    except Exception as e:
        logger.error(f"[TRACKING] Error tracking unsubscribe: {e}")
        return HTMLResponse(content="<h1>An error occurred. Please try again.</h1>", status_code=500)

    return _page(
        "Unsubscribed",
        "<div class=\"icon\">&#10003;</div>"
        "<h1>Unsubscribed successfully</h1>"
        "<p>You have been removed from our mailing list. You will no longer receive emails from this campaign.</p>",
    )
