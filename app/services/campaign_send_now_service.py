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

from app.core.clock import utcnow
from app.core.errors import BadRequestError, ExternalServiceUnavailable
from app.domain.sending import SendAttempt, fail
from app.models.campaign import Campaign
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.repositories.sending import Session, SqlAlchemySendingQueueRepository
from app.repositories.sending_context import SqlAlchemySendingContextRepository
from app.services import daily_limit, send_breaker, suppression
from app.services.email_scheduler_service import email_scheduler, record_deferred_failure
from app.services.email_sender_service import (
    DeferredEmailFailure, PermanentEmailFailure, TransientEmailFailure, email_sender,
)
from app.services.send_guards import require_active_inbox
from app.services.sequence_stop import stop_reason

logger = logging.getLogger(__name__)

SEND_NOW_BATCH = 25
PENDING_STATUSES = ["QUEUED", "SCHEDULED"]
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
    """The run's start as naive UTC, microsecond precision like sent_at (DATETIME(6)), so a send in
    this run is never "before" it. An aware ``since`` is converted to UTC; no ``since`` means now."""
    started_at = since.astimezone(timezone.utc).replace(tzinfo=None) if since and since.tzinfo else since
    return started_at or now or utcnow()


# ──────────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────────

class CampaignSendNowService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.queue = SqlAlchemySendingQueueRepository(db)
        self.ctx = SqlAlchemySendingContextRepository(db)

    async def send_now(
        self,
        campaign: Campaign,
        limit: int,
        cursor: Optional[str],
        since: Optional[datetime],
    ) -> Dict[str, Any]:
        """Send one batch of the campaign's due emails; returns counts plus the cursor to continue."""
        self._require_sendable(campaign)
        campaign_id = campaign.campaign_id
        after = parse_cursor(cursor)
        started_at = normalize_run_start(since)
        criteria = self.queue.send_now_criteria(campaign_id, started_at if since else None, after)
        batch = self.queue.send_now_batch(criteria, limit)
        queued_emails = self._batch_messages(batch)
        next_cursor = format_cursor(batch[-1][1], batch[-1][0]) if batch else cursor
        run_started = started_at.isoformat()

        if not queued_emails:
            return {"status": "no_emails", "message": "No queued emails to send", "sent": 0, "failed": 0,
                    "skipped": 0, "total": 0, "remaining": 0, "has_more": False, "cursor": next_cursor,
                    "started_at": run_started, "errors": []}

        sender_name = self._sender_name(campaign_id)
        tally = _Tally()
        for email_msg in queued_emails:
            await self._send_safely(campaign, email_msg, sender_name, tally)

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

    @staticmethod
    def _require_sendable(campaign: Campaign) -> None:
        """ACTIVE, with an active inbox (item 12), and SES not tripped (item 14)."""
        if campaign.status != "ACTIVE":
            raise BadRequestError("Campaign must be ACTIVE to send emails")
        require_active_inbox(campaign, "send emails for")
        if send_breaker.platform_breaker.is_open():
            raise ExternalServiceUnavailable(
                f"Sending is paused because the email provider keeps failing; try again in "
                f"{max(1, round(send_breaker.platform_breaker.retry_in() / 60))} minute(s).",
                code="SENDING_CIRCUIT_OPEN")

    def _batch_messages(self, batch: List[Tuple[str, int]]) -> List[EmailMessage]:
        """The batch's messages in (step, message id) order."""
        step_of = {mid: step for mid, step in batch}
        messages = self.queue.messages(list(step_of))
        messages.sort(key=lambda m: (step_of[m.message_id], m.message_id))
        return messages

    async def _send_safely(self, campaign: Campaign, email_msg: EmailMessage, sender_name: Optional[str],
                           tally: "_Tally") -> None:
        """Safety net: one bad message must not abort the whole batch."""
        try:
            await self._send_one(campaign, email_msg, sender_name, tally)
        except Exception as e:
            logger.error(f"[SendNow] Error processing email {email_msg.message_id}: {e}")
            try:
                email_msg.status = "FAILED"
                email_msg.failure_reason = str(e)[:200]
                self.db.add(email_msg)
                self.db.commit()
            except Exception:  # the session itself failed: discard and keep going
                self.db.rollback()
            tally.failed += 1
            tally.errors.append(str(e)[:100])

    def _sender_name(self, campaign_id: str) -> Optional[str]:
        """Campaign's sender name, else the creator's full name."""
        row = self.ctx.campaign_with_creator(campaign_id)
        if not row:
            return None
        campaign, user = row
        return campaign.sender_name or f"{user.first_name} {user.last_name}"

    def _remaining_after(self, campaign_id: str, started_at: datetime, last: Cursor) -> int:
        """How many due emails are left for the next request of this run (after this batch's cursor)."""
        return self.queue.count_due(self.queue.send_now_criteria(campaign_id, started_at, last))

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
        replied/bounced/unsubscribed in this campaign and this step's stop flags say stop (BR-DF-05)."""
        reason = suppression.blocked_reason(self.db, prospect)
        if not reason:
            stopped = email_scheduler.stopped_by_enrollment(email_msg, self.db)
            reason = stop_reason(stopped) if stopped else None
        return reason

    def _inbox_at_cap(self, email_msg: EmailMessage) -> Optional[SendingInbox]:
        """The message's inbox if it already reached its daily cap (warmup schedule + override)."""
        inbox = self.ctx.inbox(email_msg.inbox_id) if email_msg.inbox_id else None
        if not inbox:
            return None
        sent_today = inbox.emails_sent_today or 0
        if not inbox.last_daily_reset or inbox.last_daily_reset.date() < utcnow().date():
            sent_today = 0
        return inbox if sent_today >= email_sender.get_inbox_daily_limit(inbox) else None

    def _claim(self, email_msg: EmailMessage) -> bool:
        """Atomic check-and-set to SENDING so the background scheduler cannot pick it too (committed now)."""
        rows = self.queue.claim_for_send_now(email_msg.message_id, utcnow())
        self.db.commit()
        if rows == 0:
            logger.info(f"[SendNow] Skipping email {email_msg.message_id}, not in QUEUED state "
                        f"(likely picked by scheduler).")
        return rows > 0

    def _gate(self, campaign: Campaign, email_msg: EmailMessage, prospect: Prospect,
              tally: "_Tally") -> Tuple[bool, Optional[str], Optional[str]]:
        """The send-time guards; (go, from address, daily-limit user). Skips are recorded and committed."""
        reason = self._guard_reason(email_msg, prospect)
        if reason:
            self._skip(email_msg, "CANCELLED", tally, prospect.email, reason, failure_reason=reason)
            return False, None, None
        from_email_address = email_scheduler._resolve_sender_for_message(email_msg=email_msg, campaign=campaign,
                                                                         db=self.db)
        if not from_email_address:
            self._skip(email_msg, "QUEUED", tally, prospect.email,
                       "The campaign has no active sending inbox; left queued")
            return False, None, None
        cap_inbox = self._inbox_at_cap(email_msg)
        if cap_inbox:
            self._skip(email_msg, "QUEUED", tally, prospect.email,
                       f"Inbox {cap_inbox.email_address} reached its daily limit; left queued")
            return False, None, None
        # Daily new-contact limit (BR-OV-01): first steps only
        limit_user = None
        if daily_limit.is_first_step(self.db, email_msg):
            limit_user = prospect.owner_id or campaign.created_by
            if limit_user and not email_scheduler._daily.reserve(self.db, limit_user):
                self._skip(email_msg, "QUEUED", tally, prospect.email, "Daily new-contact limit reached; left queued")
                return False, None, None
        return True, from_email_address, limit_user

    async def _send_one(self, campaign: Campaign, email_msg: EmailMessage, sender_name: Optional[str],
                        tally: "_Tally") -> None:
        db = self.db
        if not self._claim(email_msg):
            return
        template = self.ctx.template(email_msg.template_id) if email_msg.template_id else None
        prospect = self.ctx.prospect(email_msg.prospect_id)
        if not template or not prospect:
            email_msg.status = "FAILED"
            email_msg.failure_reason = "Template or prospect not found"
            db.add(email_msg)
            db.commit()
            tally.failed += 1
            return
        go, from_email_address, limit_user = self._gate(campaign, email_msg, prospect, tally)
        if not go:
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
            self._release(limit_user)
            self._on_send_error(email_msg, prospect, send_exc, tally)
            db.commit()
            return
        self._on_result(email_msg, prospect, SendAttempt.from_result(result), result, from_email_address,
                        limit_user, tally)
        db.commit()  # the send result + prospect state advance

    @staticmethod
    def _release(limit_user: Optional[str]) -> None:
        if limit_user:
            email_scheduler._daily.release(limit_user)

    def _on_send_error(self, email_msg: EmailMessage, prospect: Prospect, send_exc: Exception,
                       tally: "_Tally") -> None:
        if isinstance(send_exc, DeferredEmailFailure):
            # Platform / sender-identity problem (item 14): breaker + mailbox auto-pause
            record_deferred_failure(self.db, send_exc.kind.value, email_msg.inbox_id, str(send_exc))
        if isinstance(send_exc, TransientEmailFailure):
            email_msg.status = "QUEUED"
            email_msg.last_error_code = str(send_exc)[:50]
            tally.skipped.append({"email": prospect.email,
                                  "reason": f"Temporary sending error, left queued: {send_exc}"})
            return
        fail(email_msg, str(send_exc)[:1000], utcnow())
        tally.failed += 1
        tally.errors.append(f"{prospect.email}: {send_exc}")

    def _on_result(self, email_msg: EmailMessage, prospect: Prospect, attempt: SendAttempt,
                   result: Dict[str, Any], from_email_address: Optional[str], limit_user: Optional[str],
                   tally: "_Tally") -> None:
        if attempt.success:
            self._record_sent(email_msg, result, from_email_address)
            tally.sent += 1
            return
        self._release(limit_user)
        email_msg.status = "FAILED"
        email_msg.failure_reason = result.get("error", "Unknown error")
        tally.failed += 1
        tally.errors.append(f"{prospect.email}: {result.get('error')}")

    def _record_sent(self, email_msg: EmailMessage, result: Dict[str, Any], from_email_address: Optional[str]) -> None:
        db = self.db
        send_breaker.platform_breaker.record_success()
        # Conditional SENT (item 15a): a bounce the webhook recorded mid-send is kept
        email_scheduler.mark_sent(db, email_msg, sent_at=utcnow(), from_email=from_email_address,
                                  provider_message_id=result.get("ses_message_id"),
                                  ses_message_id=result.get("ses_message_id"))

        if email_msg.inbox_id:
            sent_inbox = self.ctx.inbox(email_msg.inbox_id)
            if sent_inbox:
                sent_inbox.emails_sent_today = (sent_inbox.emails_sent_today or 0) + 1
                sent_inbox.last_sent_at = utcnow()

        self.queue.record_sent_event(email_msg.message_id, utcnow(), with_metadata=False)
        self._advance_prospect(email_msg)

    def _advance_prospect(self, email_msg: EmailMessage) -> None:
        """Move the contact to the next step (its gap counts from this send, B01) or complete it.
        Mirrors the scheduler's _advance_enrollment."""
        seq_step = self.ctx.step(email_msg.sequence_id)
        if not seq_step:
            return
        cp = self.ctx.enrollment(email_msg.campaign_id, email_msg.prospect_id)
        if not cp or cp.status != "ACTIVE":
            return
        next_step_number = seq_step.step_number + 1
        next_step = self.ctx.step_number(email_msg.campaign_id, next_step_number)
        if next_step:
            cp.current_step = next_step_number
            cp.next_scheduled_at = email_scheduler.schedule_next_step(
                self.db, email_msg, next_step, email_msg.sent_at
            ) or utcnow() + timedelta(days=next_step.wait_days)
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
