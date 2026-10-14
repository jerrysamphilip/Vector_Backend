# app/services/campaign_send_now_service.py
"""
"Send now" for a campaign: immediately send its due queued emails in sequence order, at most
``limit`` per call, resuming from a cursor so a large queue never times out one HTTP request.

Applies the same send-time safety guards as the background scheduler (suppression, stop on
reply/bounce/unsubscribe, per-inbox daily cap, daily new-contact limit) and advances each
contact's CampaignProspect state after a successful send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.core.errors import BadRequestError
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.services import daily_limit
from app.services.email_scheduler_service import email_scheduler
from app.services.email_sender_service import PermanentEmailFailure, TransientEmailFailure, email_sender
from app.services.suppression import blocked_reason

logger = logging.getLogger(__name__)

SEND_NOW_BATCH = 25
PENDING_STATUSES = ["QUEUED", "SCHEDULED"]
STOPPED_ENROLLMENT_STATUSES = ("REPLIED", "BOUNCED", "UNSUBSCRIBED")
MAX_CURSOR_MESSAGE_ID = 64
MAX_SKIPPED_DETAILS = 50
MAX_ERRORS_RETURNED = 5

Cursor = Tuple[int, str]


# ──────────────────────────────────────────────────────────────────────────────
# Cursor helpers (pure)
# ──────────────────────────────────────────────────────────────────────────────

def parse_cursor(cursor: Optional[str]) -> Optional[Cursor]:
    """'<step_number>:<message_id>' -> (step, message_id); None/'' -> None; garbage -> BadRequestError."""
    if not cursor:
        return None
    step, sep, message_id = cursor.partition(":")
    if not sep or not step.isdigit() or not message_id or len(message_id) > MAX_CURSOR_MESSAGE_ID:
        raise BadRequestError("Invalid cursor")
    return int(step), message_id


def format_cursor(step_number: int, message_id: str) -> str:
    return f"{int(step_number or 0)}:{message_id}"


def normalize_run_start(since: Optional[datetime], now: Optional[datetime] = None) -> datetime:
    """The run's start as naive UTC in whole seconds (sent_at has no fractions, so a send in this
    run is never "before" it). An aware ``since`` is converted to UTC; no ``since`` means now."""
    started_at = since.astimezone(timezone.utc).replace(tzinfo=None) if since and since.tzinfo else since
    return (started_at or now or datetime.utcnow()).replace(microsecond=0)


# ──────────────────────────────────────────────────────────────────────────────
# Query building
# ──────────────────────────────────────────────────────────────────────────────

def _not_unlocked_during_run(started_at: datetime) -> Any:
    """Skip follow-ups whose previous step was sent during this run (left to the scheduler)."""
    prev, prev_step = aliased(EmailMessage), aliased(EmailSequence)
    return ~exists().where(
        prev.campaign_id == EmailMessage.campaign_id, prev.prospect_id == EmailMessage.prospect_id,
        prev.message_id != EmailMessage.message_id, prev.sent_at >= started_at,
        prev_step.sequence_id == prev.sequence_id, prev_step.step_number < EmailSequence.step_number)


def _after_cursor(after: Cursor) -> Any:
    return or_(EmailSequence.step_number > after[0],
               and_(EmailSequence.step_number == after[0], EmailMessage.message_id > after[1]))


def _due_criteria(campaign_id: str) -> List[Any]:
    # A contact's follow-up only goes once the step before it was sent (B01)
    return [
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.status.in_(PENDING_STATUSES),
        ~email_scheduler._earlier_step_pending(EmailMessage, EmailSequence),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────────

class CampaignSendNowService:
    def __init__(self, db: Session) -> None:
        self.db = db

    async def send_now(
        self,
        campaign: Campaign,
        limit: int,
        cursor: Optional[str],
        since: Optional[datetime],
    ) -> Dict[str, Any]:
        """Send one batch of the campaign's due emails; returns counts plus the cursor to continue."""
        db = self.db
        campaign_id = campaign.campaign_id
        if campaign.status != "ACTIVE":
            raise BadRequestError("Campaign must be ACTIVE to send emails")

        after = parse_cursor(cursor)
        started_at = normalize_run_start(since)
        criteria = _due_criteria(campaign_id)
        if since:
            criteria.append(_not_unlocked_during_run(started_at))
        if after:
            criteria.append(_after_cursor(after))

        batch = db.query(EmailMessage).join(EmailSequence).filter(*criteria).with_entities(
            EmailMessage.message_id, EmailSequence.step_number
        ).order_by(EmailSequence.step_number, EmailMessage.message_id).limit(limit).all()
        step_of = {mid: step for mid, step in batch}
        queued_emails = (
            db.query(EmailMessage).filter(EmailMessage.message_id.in_(list(step_of))).all() if batch else []
        )
        queued_emails.sort(key=lambda m: (step_of[m.message_id], m.message_id))
        next_cursor = format_cursor(batch[-1][1], batch[-1][0]) if batch else cursor
        run_started = started_at.isoformat()

        if not queued_emails:
            return {"status": "no_emails", "message": "No queued emails to send", "sent": 0, "failed": 0,
                    "skipped": 0, "total": 0, "remaining": 0, "has_more": False, "cursor": next_cursor,
                    "started_at": run_started, "errors": []}

        sender_name = self._sender_name(campaign_id)
        tally = _Tally()
        for email_msg in queued_emails:
            try:
                await self._send_one(campaign, email_msg, sender_name, tally)
            except Exception as e:  # safety net: one bad message must not abort the whole batch
                logger.error(f"[SendNow] Error processing email {email_msg.message_id}: {e}")
                try:
                    email_msg.status = "FAILED"
                    email_msg.failure_reason = str(e)[:200]
                    db.add(email_msg)
                    db.commit()
                except SQLAlchemyError:
                    db.rollback()
                tally.failed += 1
                tally.errors.append(str(e)[:100])

        last_in_batch: Cursor = (int(batch[-1][1] or 0), batch[-1][0])  # == parse_cursor(next_cursor)
        remaining = self._remaining_after(campaign_id, started_at, last_in_batch)
        return {
            "status": "completed",
            "sent": tally.sent,
            "failed": tally.failed,
            "skipped": len(tally.skipped),
            "skipped_details": tally.skipped[:MAX_SKIPPED_DETAILS],
            "total": len(queued_emails),
            "remaining": int(remaining),
            "has_more": remaining > 0,
            "cursor": next_cursor,
            "started_at": run_started,
            "errors": tally.errors[:MAX_ERRORS_RETURNED],
        }

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _sender_name(self, campaign_id: str) -> Optional[str]:
        """Campaign's sender name, else the creator's full name."""
        row = self.db.query(Campaign, User).join(
            User, Campaign.created_by == User.user_id
        ).filter(Campaign.campaign_id == campaign_id).first()
        if not row:
            return None
        campaign, user = row
        return campaign.sender_name or f"{user.first_name} {user.last_name}"

    def _remaining_after(self, campaign_id: str, started_at: datetime, last: Cursor) -> int:
        """How many due emails are left for the next request of this run (after this batch's cursor)."""
        rest = _due_criteria(campaign_id) + [_not_unlocked_during_run(started_at), _after_cursor(last)]
        return self.db.query(func.count(EmailMessage.message_id)).join(EmailSequence).filter(*rest).scalar() or 0

    def _skip(self, email_msg: EmailMessage, status: str, tally: "_Tally", email: str, reason: str,
              failure_reason: Optional[str] = None) -> None:
        email_msg.status = status
        if failure_reason is not None:
            email_msg.failure_reason = failure_reason
        self.db.add(email_msg)
        self.db.commit()
        tally.skipped.append({"email": email, "reason": reason})

    def _guard_reason(self, email_msg: EmailMessage, prospect: Prospect) -> Optional[str]:
        """Why this email must not go: suppression/unsubscribe (BR-DF-08) or the contact already
        replied/bounced/unsubscribed in this campaign (BR-DF-05)."""
        reason = blocked_reason(self.db, prospect)
        if not reason and email_msg.sequence_id:
            enrollment = self.db.query(CampaignProspect.status).filter(
                CampaignProspect.campaign_id == email_msg.campaign_id,
                CampaignProspect.prospect_id == email_msg.prospect_id,
            ).first()
            if enrollment and enrollment[0] in STOPPED_ENROLLMENT_STATUSES:
                reason = f"Stopped: contact {enrollment[0].lower()}"
        return reason

    def _inbox_at_cap(self, email_msg: EmailMessage) -> Optional[SendingInbox]:
        """The message's inbox if it already reached its daily cap (warmup schedule + override)."""
        if not email_msg.inbox_id:
            return None
        inbox = self.db.query(SendingInbox).filter(SendingInbox.inbox_id == email_msg.inbox_id).first()
        if not inbox:
            return None
        sent_today = inbox.emails_sent_today or 0
        if not inbox.last_daily_reset or inbox.last_daily_reset.date() < datetime.utcnow().date():
            sent_today = 0
        return inbox if sent_today >= email_sender.get_inbox_daily_limit(inbox) else None

    async def _send_one(self, campaign: Campaign, email_msg: EmailMessage, sender_name: Optional[str],
                        tally: "_Tally") -> None:
        db = self.db
        # Atomic check-and-set to SENDING so the background scheduler cannot pick it too
        rows_updated = db.query(EmailMessage).filter(
            EmailMessage.message_id == email_msg.message_id,
            EmailMessage.status.in_(PENDING_STATUSES)
        ).update({"status": "SENDING"}, synchronize_session=False)
        db.commit()  # commit the lock acquisition immediately
        if rows_updated == 0:
            logger.info(f"[SendNow] Skipping email {email_msg.message_id}, not in QUEUED state "
                        f"(likely picked by scheduler).")
            return

        template = db.query(EmailTemplate).filter(EmailTemplate.template_id == email_msg.template_id).first()
        prospect = db.query(Prospect).filter(Prospect.prospect_id == email_msg.prospect_id).first()
        if not template or not prospect:
            email_msg.status = "FAILED"
            email_msg.failure_reason = "Template or prospect not found"
            db.add(email_msg)
            db.commit()
            tally.failed += 1
            return

        reason = self._guard_reason(email_msg, prospect)
        if reason:
            self._skip(email_msg, "CANCELLED", tally, prospect.email, reason, failure_reason=reason)
            return

        from_email_address = email_scheduler._resolve_sender_for_message(email_msg=email_msg, campaign=campaign, db=db)

        cap_inbox = self._inbox_at_cap(email_msg)
        if cap_inbox:
            self._skip(email_msg, "QUEUED", tally, prospect.email,
                       f"Inbox {cap_inbox.email_address} reached its daily limit; left queued")
            return

        # Daily new-contact limit (BR-OV-01): first steps only
        limit_user = None
        if daily_limit.is_first_step(db, email_msg):
            limit_user = prospect.owner_id or campaign.created_by
            if limit_user and not email_scheduler._daily.reserve(db, limit_user):
                self._skip(email_msg, "QUEUED", tally, prospect.email, "Daily new-contact limit reached; left queued")
                return

        # Permanent errors fail fast with the reason; a temporary error leaves it queued (B06)
        try:
            result = await email_sender.send_with_retry(
                email_message=email_msg,
                email_template=template,
                prospect=prospect,
                max_retries=1,  # only 1 retry for an immediate send
                sender_name=sender_name,
                from_email_address=from_email_address,
            )
        except (TransientEmailFailure, PermanentEmailFailure) as send_exc:
            if limit_user:
                email_scheduler._daily.release(limit_user)
            if isinstance(send_exc, TransientEmailFailure):
                email_msg.status = "QUEUED"
                email_msg.last_error_code = str(send_exc)[:50]
                tally.skipped.append({"email": prospect.email,
                                      "reason": f"Temporary sending error, left queued: {send_exc}"})
            else:
                email_msg.status = "FAILED"
                email_msg.final_status, email_msg.final_status_at = "FAILED", datetime.utcnow()
                email_msg.failure_reason = str(send_exc)[:1000]
                tally.failed += 1
                tally.errors.append(f"{prospect.email}: {send_exc}")
            db.commit()
            return

        if result["success"]:
            self._record_sent(email_msg, result, from_email_address)
            tally.sent += 1
        else:
            if limit_user:
                email_scheduler._daily.release(limit_user)
            email_msg.status = "FAILED"
            email_msg.failure_reason = result.get("error", "Unknown error")
            tally.failed += 1
            tally.errors.append(f"{prospect.email}: {result.get('error')}")
        db.commit()  # the send result + prospect state advance

    def _record_sent(self, email_msg: EmailMessage, result: Dict[str, Any], from_email_address: Optional[str]) -> None:
        db = self.db
        email_msg.status = "SENT"
        email_msg.sent_at = datetime.utcnow()
        email_msg.provider_message_id = result.get("ses_message_id")
        email_msg.from_email = from_email_address

        if email_msg.inbox_id:
            sent_inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == email_msg.inbox_id).first()
            if sent_inbox:
                sent_inbox.emails_sent_today = (sent_inbox.emails_sent_today or 0) + 1
                sent_inbox.last_sent_at = datetime.utcnow()

        db.add(EmailEvent(message_id=email_msg.message_id, event_type=EmailEvent.EVENT_SENT,
                          event_time=datetime.utcnow()))
        self._advance_prospect(email_msg)

    def _advance_prospect(self, email_msg: EmailMessage) -> None:
        """Move the contact to the next step (its gap counts from this send, B01) or complete it.
        Mirrors execution_service.py."""
        db = self.db
        seq_step = db.query(EmailSequence).filter(EmailSequence.sequence_id == email_msg.sequence_id).first()
        if not seq_step:
            return
        cp = db.query(CampaignProspect).filter(
            CampaignProspect.campaign_id == email_msg.campaign_id,
            CampaignProspect.prospect_id == email_msg.prospect_id
        ).first()
        if not cp or cp.status != "ACTIVE":
            return
        next_step_number = seq_step.step_number + 1
        next_step = db.query(EmailSequence).filter(
            EmailSequence.campaign_id == email_msg.campaign_id,
            EmailSequence.step_number == next_step_number
        ).first()
        if next_step:
            cp.current_step = next_step_number
            cp.next_scheduled_at = email_scheduler.schedule_next_step(
                db, email_msg, next_step, email_msg.sent_at
            ) or datetime.utcnow() + timedelta(days=next_step.wait_days)
        else:
            cp.status = "COMPLETED"
            cp.next_scheduled_at = None


class _Tally:
    """Per-batch counters."""

    def __init__(self) -> None:
        self.sent = 0
        self.failed = 0
        self.errors: List[str] = []
        self.skipped: List[Dict[str, str]] = []  # prospects blocked by a send-time safety guard
