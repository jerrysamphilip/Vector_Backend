
# app/routers/inbox_router.py
"""
API Router for Sending Inboxes management.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.core.database import get_db
from app.core.auth import require_role, require_permission
from app.models.user import User
from app.models.sending_inbox import SendingInbox
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from app.schemas.inbox_schema import (
    SendingInboxResponse,
    SendingInboxUpdate,
    SendingInboxCreate,
    SendingInboxCreatedResponse,
    WarmupOverviewResponse,
    InboxWarmupDetailResponse,
)
from app.services.imap_sync_service import IMAPSyncService
from app.services.deliverability_service import deliverability_service
from app.services.warmup_service import warmup_service
from app.services.email_sender_service import email_sender
from app.models.inbox_warmup_metric import InboxWarmupMetric
from app.models.inbox_warmup_event import InboxWarmupEvent
from datetime import datetime
import uuid

router = APIRouter(prefix="/inboxes", tags=["Inboxes"])

@router.post("", response_model=SendingInboxCreatedResponse, status_code=201, dependencies=[Depends(require_permission("manage_inboxes"))])
def create_inbox(
    data: SendingInboxCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """
    Add a new sending inbox. Automatically triggers domain verification logic.
    """
    # Check if exists
    existing = db.query(SendingInbox).filter(
        SendingInbox.email_address == data.email_address,
        SendingInbox.tenant_id == current_user.tenant_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Inbox with this email already exists")

    new_inbox = SendingInbox(
        inbox_id=str(uuid.uuid4()),
        tenant_id=current_user.tenant_id,
        email_address=data.email_address,
        provider=data.provider or "SMTP",
        daily_limit=data.daily_limit,
        status="ACTIVE",
        smtp_host=data.smtp_host,
        smtp_port=data.smtp_port or 587,
        smtp_username=data.smtp_username,
        smtp_password=data.smtp_password,
        smtp_use_ssl=data.smtp_use_ssl or False,
        imap_host=data.imap_host,
        imap_port=data.imap_port or 993,
        imap_username=data.imap_username,
        imap_password=data.imap_password,
        # Warm-up settings
        warmup_enabled=data.warmup_enabled if data.warmup_enabled is not None else True,
        warmup_start_date=datetime.utcnow() if (data.warmup_enabled is not False) else None,
        max_emails_per_day=data.max_emails_per_day,
        delay_between_emails=data.delay_between_emails or 60,
        warmup_auto_adjust=data.warmup_auto_adjust if data.warmup_auto_adjust is not None else True,
        warmup_randomize=data.warmup_randomize if data.warmup_randomize is not None else True,
        warmup_reply_rate_target=data.warmup_reply_rate_target or 35,
        warmup_max_target=data.warmup_max_target,
        warmup_identifier=data.warmup_identifier,
        warmup_status="ACTIVE" if (data.warmup_enabled is not False) else "DISABLED",
    )
    db.add(new_inbox)
    db.commit()
    db.refresh(new_inbox)
    
    # Trigger Domain Verification
    domain_verification = None
    if "@" in new_inbox.email_address:
        domain = new_inbox.email_address.split("@")[-1]
        try:
            domain_verification = deliverability_service.verify_domain_and_get_tokens(domain, db)
        except Exception as e:
            # Don't fail the inbox creation, just log error
            print(f"Domain verification failed: {e}")
            
    # Combine result
    # Pydantic v2/v1 compat: convert sqla model to dict then add field
    response = SendingInboxCreatedResponse.model_validate(new_inbox)
    response.domain_verification = domain_verification
    
    return response

@router.get("", response_model=List[SendingInboxResponse])
def list_inboxes(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """List all available sending inboxes."""
    inboxes = db.query(SendingInbox).filter(
        SendingInbox.tenant_id == current_user.tenant_id
    ).order_by(SendingInbox.email_address).all()
    return inboxes


@router.get("/ses/quota")
def get_ses_quota(
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """
    Live AWS SES account-level sending quota (not per-mailbox — this is a
    single ceiling shared across every sending inbox on this AWS account).

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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Get warmup pool summary for the current tenant."""
    return warmup_service.get_overview(db, current_user.tenant_id)


@router.post("/warmup/run", dependencies=[Depends(require_permission("manage_inboxes"))])
async def run_warmup_cycle(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Run a warmup cycle immediately for the current tenant."""
    stats = await warmup_service.run_cycle(db=db, tenant_id=current_user.tenant_id)
    return {"status": "success", "stats": stats}


@router.get("/{inbox_id}/warmup", response_model=InboxWarmupDetailResponse)
def get_inbox_warmup_detail(
    inbox_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Return warmup settings, recent metrics, and recent activity for one mailbox."""
    try:
        return warmup_service.get_inbox_activity(db, inbox_id, current_user.tenant_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Inbox not found")

@router.get("/{inbox_id}", response_model=SendingInboxResponse)
def get_inbox(
    inbox_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Get a single inbox by ID."""
    inbox = db.query(SendingInbox).filter(
        SendingInbox.inbox_id == inbox_id,
        SendingInbox.tenant_id == current_user.tenant_id,
    ).first()
    if not inbox:
        raise HTTPException(status_code=404, detail="Inbox not found")
    return inbox

@router.put("/{inbox_id}", response_model=SendingInboxResponse, dependencies=[Depends(require_permission("manage_inboxes"))])
def update_inbox(
    inbox_id: str,
    data: SendingInboxUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Update inbox details (like IMAP credentials)."""
    inbox = db.query(SendingInbox).filter(
        SendingInbox.inbox_id == inbox_id,
        SendingInbox.tenant_id == current_user.tenant_id,
    ).first()
    if not inbox:
        raise HTTPException(status_code=404, detail="Inbox not found")

    updates = data.model_dump(exclude_unset=True)
    # Keep existing credentials when edit form submits blank (avoids wiping saved values).
    for field in ("imap_password", "imap_username", "smtp_password", "smtp_username"):
        if field in updates and not updates[field]:
            updates.pop(field)

    if "warmup_enabled" in updates:
        enabling = bool(updates["warmup_enabled"])
        if enabling and not inbox.warmup_start_date:
            inbox.warmup_start_date = datetime.utcnow()
            inbox.warmup_day = 0
        inbox.warmup_status = "ACTIVE" if enabling else "DISABLED"
        if enabling:
            inbox.warmup_issue_code = None
            inbox.warmup_issue_message = None
        else:
            inbox.warmup_issue_code = "WARMUP_DISABLED"
            inbox.warmup_issue_message = "Warmup is disabled for this inbox."

    for key, value in updates.items():
        setattr(inbox, key, value)

    db.commit()
    db.refresh(inbox)
    return inbox

@router.post("/{inbox_id}/sync")
def sync_inbox_history(
    inbox_id: str,
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Trigger a historical IMAP sync."""
    inbox = db.query(SendingInbox).filter(
        SendingInbox.inbox_id == inbox_id,
        SendingInbox.tenant_id == current_user.tenant_id,
    ).first()
    if not inbox:
        raise HTTPException(status_code=404, detail="Inbox not found")

    if not inbox.imap_host or not inbox.imap_password:
        raise HTTPException(
            status_code=400,
            detail=(
                f"IMAP is not configured for {inbox.email_address}. "
                "Please set imap_host, imap_username, and imap_password in the inbox settings."
            )
        )

    service = IMAPSyncService(db)
    success = service.sync_inbox(inbox, days_back=days)
    if not success:
        raise HTTPException(
            status_code=500,
            detail=(
                f"IMAP sync failed for {inbox.email_address}. "
                f"Check that imap_host={inbox.imap_host} is reachable and credentials are correct."
            )
        )
    return {
        "status": "success",
        "message": f"IMAP sync for last {days} days completed.",
        "inbox": inbox.email_address,
        "imap_host": inbox.imap_host,
        "last_sync_at": inbox.last_sync_at.isoformat() if inbox.last_sync_at else None,
    }


@router.get("/{inbox_id}/test-imap")
def test_imap_connection(
    inbox_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """
    Test IMAP connectivity and list recent inbox emails.
    Use this to diagnose why replies are not appearing.
    """
    import imaplib
    import email as email_lib
    from email.header import decode_header
    from email.utils import parseaddr
    from datetime import timedelta

    inbox = db.query(SendingInbox).filter(
        SendingInbox.inbox_id == inbox_id,
        SendingInbox.tenant_id == current_user.tenant_id,
    ).first()
    if not inbox:
        raise HTTPException(status_code=404, detail="Inbox not found")

    if not inbox.imap_host:
        return {
            "status": "not_configured",
            "inbox": inbox.email_address,
            "error": "imap_host is not set. Set it to imap.zoho.in (Zoho India) or imap.zoho.com",
        }
    if not inbox.imap_password:
        return {
            "status": "not_configured",
            "inbox": inbox.email_address,
            "error": "imap_password is not set. For Zoho, use an App Password (not your login password).",
        }

    result = {
        "inbox": inbox.email_address,
        "imap_host": inbox.imap_host,
        "imap_port": inbox.imap_port or 993,
        "imap_username": inbox.imap_username or inbox.email_address,
        "last_sync_at": inbox.last_sync_at.isoformat() if inbox.last_sync_at else None,
        "status": None,
        "error": None,
        "recent_inbox_emails": [],
    }

    try:
        mail = imaplib.IMAP4_SSL(inbox.imap_host, inbox.imap_port or 993)
        mail.login(inbox.imap_username or inbox.email_address, inbox.imap_password)
        result["status"] = "connected"

        # Check INBOX folder
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            result["error"] = "Could not select INBOX folder"
            mail.logout()
            return result

        # Search last 7 days
        past_date = datetime.utcnow() - timedelta(days=7)
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        date_cutoff = f"{past_date.day}-{months[past_date.month - 1]}-{past_date.year}"
        status, data = mail.search(None, f'(SINCE "{date_cutoff}")')

        message_ids = data[0].split() if status == "OK" else []
        result["inbox_message_count_last_7d"] = len(message_ids)

        # Fetch up to 10 most recent
        for msg_id in message_ids[-10:]:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822.HEADER)")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                subject_raw = msg.get("Subject", "")
                decoded_parts = decode_header(subject_raw)
                subject = ""
                for part, enc in decoded_parts:
                    subject += part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part

                from_addr = parseaddr(msg.get("From", ""))[1]
                to_addr = parseaddr(msg.get("To", ""))[1]

                result["recent_inbox_emails"].append({
                    "from": from_addr,
                    "to": to_addr,
                    "subject": subject[:80],
                    "date": msg.get("Date", ""),
                    "message_id": msg.get("Message-ID", ""),
                })
            except Exception as e:
                result["recent_inbox_emails"].append({"error": str(e)})

        mail.logout()

    except imaplib.IMAP4.error as e:
        result["status"] = "auth_failed"
        result["error"] = (
            f"IMAP login failed: {e}. "
            "For Zoho: enable IMAP in Zoho Mail Settings → Mail Accounts → IMAP Access, "
            "and use an App Password (Settings → Security → App Passwords)."
        )
    except Exception as e:
        result["status"] = "connection_failed"
        result["error"] = str(e)

    return result


@router.delete("/{inbox_id}", status_code=204, dependencies=[Depends(require_permission("manage_inboxes"))])
def delete_inbox(
    inbox_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Delete an inbox and remove it from system."""
    inbox = db.query(SendingInbox).filter(
        SendingInbox.inbox_id == inbox_id,
        SendingInbox.tenant_id == current_user.tenant_id,
    ).first()
    if not inbox:
        raise HTTPException(status_code=404, detail="Inbox not found")

    conversation_ids = [
        conversation_id
        for (conversation_id,) in db.query(Conversation.id).filter(Conversation.inbox_id == inbox_id).all()
    ]

    if conversation_ids:
        # Preserve historical email records while removing inbox-bound conversation threads.
        db.query(EmailMessage).filter(
            EmailMessage.conversation_id.in_(conversation_ids)
        ).update(
            {EmailMessage.conversation_id: None},
            synchronize_session=False,
        )

    db.query(EmailMessage).filter(EmailMessage.inbox_id == inbox_id).update(
        {EmailMessage.inbox_id: None},
        synchronize_session=False,
    )

    db.query(Conversation).filter(Conversation.inbox_id == inbox_id).delete(synchronize_session=False)

    # Delete child warmup records first to avoid FK constraint violations.
    # Events can reference this inbox as either source or peer.
    db.query(InboxWarmupEvent).filter(
        or_(
            InboxWarmupEvent.inbox_id == inbox_id,
            InboxWarmupEvent.peer_inbox_id == inbox_id
        )
    ).delete(synchronize_session=False)
    db.query(InboxWarmupMetric).filter(InboxWarmupMetric.inbox_id == inbox_id).delete(synchronize_session=False)
    db.delete(inbox)
    db.commit()
    return None
