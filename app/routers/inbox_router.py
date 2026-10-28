# app/routers/inbox_router.py
"""
API Router for Sending Inboxes management (email accounts page, F-20; Microsoft 365, BR-DF-05).

Layered (ADR 0009): each handler makes one call to ``InboxService`` (app/services/inbox_service.py),
injected through the ``inbox_service`` provider so tests swap it with ``app.dependency_overrides``.
Warm-up routes call the warm-up service (a worker-side aggregate) on the request session.
"""
from typing import Any, Dict, List
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.core.auth import require_permission, require_role
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import BadRequestError
from app.schemas.inbox_schema import (
    InboxWarmupDetailResponse,
    SendingInboxCreate,
    SendingInboxCreatedResponse,
    SendingInboxResponse,
    SendingInboxUpdate,
    WarmupOverviewResponse,
)
from app.services import ms365_oauth, oauth_binding
from app.services.email_sender_service import email_sender
from app.services.inbox_service import InboxService, check_mail_host
from app.services.warmup_service import warmup_service

router = APIRouter(prefix="/inboxes", tags=["Inboxes"])

admin_user = require_role("SUPER_ADMIN", "ADMIN")


def inbox_service(db=Depends(get_db)) -> InboxService:
    return InboxService.for_session(db)


Inboxes = Depends(inbox_service)


def _check_mail_host(host: Any) -> None:
    """The mail-host safety check (SSRF, F007) as an HTTP 400 (inbox_service.check_mail_host)."""
    try:
        check_mail_host(host)
    except BadRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


# ── Microsoft 365 OAuth (BR-DF-05) ─────────────────────────────

@router.get("/oauth/microsoft/status")
def ms365_status(current_user: Any = Depends(admin_user)):
    return {"configured": ms365_oauth.is_configured()}


@router.post("/{inbox_id}/oauth/microsoft/start", dependencies=[Depends(require_permission("manage_inboxes"))])
def ms365_start(
    inbox_id: str,
    response: Response,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
):
    """Begin connecting a Microsoft 365 mailbox; the browser is sent to the returned URL."""
    return service.start_microsoft(current_user, inbox_id, lambda: oauth_binding.issue(response))


def _back(**params: str) -> RedirectResponse:
    url = settings.MS365_POST_CONNECT_URL or "/"
    return RedirectResponse(f"{url}{'&' if '?' in url else '?'}{urlencode(params)}", status_code=302)


@router.get("/oauth/microsoft/callback", include_in_schema=False)
def ms365_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
    service: InboxService = Inboxes,
) -> RedirectResponse:
    """Microsoft redirects the browser here after sign-in; we store the tokens and go back to the app."""
    try:
        data = ms365_oauth.read_state(state)
    except ms365_oauth.OAuthError as exc:
        return _back(ms365="error", message=str(exc))
    if not oauth_binding.matches(request, data.get("nonce")):
        return _back(ms365="error", message=oauth_binding.MISMATCH)
    if error:
        return _back(ms365="error", message=error_description or error)
    return _back(**service.finish_microsoft(data["tenant_id"], data["inbox_id"], code))


@router.post("", response_model=SendingInboxCreatedResponse, status_code=201,
             dependencies=[Depends(require_permission("manage_inboxes"))])
def create_inbox(
    data: SendingInboxCreate,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
) -> SendingInboxCreatedResponse:
    """
    Add a new sending inbox. Automatically triggers domain verification logic.
    """
    return service.create(current_user, data)


@router.get("", response_model=List[SendingInboxResponse])
def list_inboxes(
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
) -> List[SendingInboxResponse]:
    """List all available sending inboxes, with pause reason and today's capacity (F-20)."""
    return service.list(current_user)


@router.post("/{inbox_id}/resume", response_model=SendingInboxResponse,
             dependencies=[Depends(require_permission("manage_inboxes"))])
def resume_inbox(
    inbox_id: str,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
) -> SendingInboxResponse:
    """Resume sending from a paused mailbox (F-20); logged in its activity. No-op when already active."""
    return service.resume(current_user, inbox_id)


@router.get("/ses/quota")
def get_ses_quota(
    current_user: Any = Depends(require_role("PLATFORM_ADMIN")),
):
    """
    Live AWS SES account-level sending quota (not per-mailbox — this is a
    single ceiling shared across every sending inbox on this AWS account).
    Platform admins only: the figures cover every tenant's mail.

    Returns Max24HourSend, MaxSendRate and SentLast24Hours straight from
    SES's GetSendQuota API so account-level capacity can be verified
    against per-mailbox daily_limit / max_emails_per_day settings, instead
    of assuming AWS allows whatever the app is configured to send.
    """
    quota = email_sender.get_send_quota()
    if not quota:
        raise HTTPException(
            status_code=502,
            detail="Could not retrieve SES send quota (check AWS credentials/region/permissions).",
        )
    return quota


@router.get("/warmup/overview", response_model=WarmupOverviewResponse)
def get_warmup_overview(
    db=Depends(get_db),
    current_user: Any = Depends(admin_user),
) -> Any:
    """Get warmup pool summary for the current tenant."""
    return warmup_service.get_overview(db, current_user.tenant_id)


@router.post("/warmup/run", dependencies=[Depends(require_permission("manage_inboxes"))])
async def run_warmup_cycle(
    db=Depends(get_db),
    current_user: Any = Depends(admin_user),
):
    """Run a warmup cycle immediately for the current tenant."""
    stats = await warmup_service.run_cycle(db=db, tenant_id=current_user.tenant_id)
    return {"status": "success", "stats": stats}


@router.get("/{inbox_id}/warmup", response_model=InboxWarmupDetailResponse)
def get_inbox_warmup_detail(
    inbox_id: str,
    db=Depends(get_db),
    current_user: Any = Depends(admin_user),
) -> Any:
    """Return warmup settings, recent metrics, and recent activity for one mailbox."""
    try:
        return warmup_service.get_inbox_activity(db, inbox_id, current_user.tenant_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Inbox not found")


@router.get("/{inbox_id}", response_model=SendingInboxResponse)
def get_inbox(
    inbox_id: str,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
) -> SendingInboxResponse:
    """Get a single inbox by ID."""
    return service.get(current_user, inbox_id)


@router.put("/{inbox_id}", response_model=SendingInboxResponse,
            dependencies=[Depends(require_permission("manage_inboxes"))])
def update_inbox(
    inbox_id: str,
    data: SendingInboxUpdate,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
) -> Any:
    """Update inbox details (like IMAP credentials)."""
    return service.update(current_user, inbox_id, data)


@router.post("/{inbox_id}/sync")
def sync_inbox_history(
    inbox_id: str,
    days: int = 30,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
):
    """Trigger a historical IMAP sync."""
    return service.sync_history(current_user, inbox_id, days)


@router.get("/{inbox_id}/test-imap")
def test_imap_connection(
    inbox_id: str,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
):
    """
    Test IMAP connectivity and list recent inbox emails.
    Use this to diagnose why replies are not appearing.
    """
    return service.test_imap(current_user, inbox_id)


@router.post("/{inbox_id}/test-smtp")
def test_smtp_connection(
    inbox_id: str,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
):
    """Sign in to the inbox's SMTP server (no email is sent) to check the sending credentials."""
    return service.test_smtp(current_user, inbox_id)


@router.delete("/{inbox_id}", status_code=204, dependencies=[Depends(require_permission("manage_inboxes"))])
def delete_inbox(
    inbox_id: str,
    service: InboxService = Inboxes,
    current_user: Any = Depends(admin_user),
):
    """Delete an inbox and remove it from system."""
    service.delete(current_user, inbox_id)
    return None
