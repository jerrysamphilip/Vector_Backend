# app/services/email_scheduler_service.py
"""
Email Scheduler Service
Processes scheduled emails from the queue and sends via AWS SES.
"""

import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, time
from typing import List, Optional
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, or_

from app.core.database import SessionLocal
from app.core.config import settings
from app.models import (
    EmailMessage, 
    EmailTemplate, 
    Prospect, 
    EmailEvent,
    GlobalUnsubscribe,
    CampaignProspect,
    SendingInbox,
    Conversation,
    EmailSequence,
)
import random
from app.services.email_sender_service import (
    email_sender,
    TransientEmailFailure,
    PermanentEmailFailure,
    AmbiguousEmailFailure,
)
from app.utils.campaign_prospect_status import set_prospect_status
from app.utils.conversation_lock import conversation_creation_lock

logger = logging.getLogger(__name__)


class EmailSchedulerService:
    """
    Service for processing and sending scheduled emails.
    
    Workflow:
    1. Query emails with status=QUEUED and scheduled_at <= now
    2. Check suppression list
    3. Get template and prospect data
    4. Send via SES
    5. Update status
    """
    
    # Each inbox works through its own queue concurrently (BR-DF-01). Per cycle an
    # inbox takes about this many seconds of sends at its configured pace, so one
    # slow inbox never holds the others back and the loop comes round quickly.
    INBOX_CYCLE_SECONDS = 120
    # A message left in SENDING this long belongs to a worker that died mid-send
    STUCK_SENDING_MINUTES = 15

    def __init__(self):
        self.batch_size = 500  # Due messages considered per cycle (all inboxes)
        self.is_running = False

    def _resolve_sender_for_message(
        self,
        email_msg: EmailMessage,
        campaign,
        db: Session,
    ) -> Optional[str]:
        """
        Resolve and persist the best sender inbox/email for a message.

        Priority:
        1. Existing valid inbox_id on the message
        2. Existing from_email that matches an active campaign inbox
        3. Sticky sender from previously sent messages for this prospect
        4. Deterministic campaign inbox rotation for this prospect
        5. System default sender as last fallback
        """
        active_inboxes = sorted(
            [i for i in (campaign.inboxes or []) if (i.status or "").upper() == "ACTIVE"],
            key=lambda inbox: (inbox.email_address or "").lower(),
        )
        active_by_id = {inbox.inbox_id: inbox for inbox in active_inboxes}
        active_by_email = {
            (inbox.email_address or "").strip().lower(): inbox
            for inbox in active_inboxes
            if inbox.email_address
        }

        if email_msg.inbox_id:
            bound_inbox = active_by_id.get(email_msg.inbox_id)
            if bound_inbox:
                email_msg.from_email = bound_inbox.email_address
                return bound_inbox.email_address

        if email_msg.from_email:
            matched_inbox = active_by_email.get(email_msg.from_email.strip().lower())
            if matched_inbox:
                email_msg.inbox_id = matched_inbox.inbox_id
                email_msg.from_email = matched_inbox.email_address
                return matched_inbox.email_address

        previous_messages = (
            db.query(EmailMessage)
            .filter(
                EmailMessage.campaign_id == email_msg.campaign_id,
                EmailMessage.prospect_id == email_msg.prospect_id,
                EmailMessage.sent_at.isnot(None),
            )
            .order_by(EmailMessage.sent_at.desc())
            .all()
        )
        for previous_msg in previous_messages:
            sticky_inbox = None
            if previous_msg.inbox_id:
                sticky_inbox = active_by_id.get(previous_msg.inbox_id)
            if not sticky_inbox and previous_msg.from_email:
                sticky_inbox = active_by_email.get(previous_msg.from_email.strip().lower())
            if sticky_inbox:
                email_msg.inbox_id = sticky_inbox.inbox_id
                email_msg.from_email = sticky_inbox.email_address
                logger.info(
                    "[Scheduler] Sticky sender rebound: %s for prospect %s",
                    sticky_inbox.email_address,
                    email_msg.prospect_id,
                )
                return sticky_inbox.email_address

        if active_inboxes:
            rotation_key = f"{email_msg.campaign_id}:{email_msg.prospect_id}"
            rotation_index = sum(ord(ch) for ch in rotation_key) % len(active_inboxes)
            selected_inbox = active_inboxes[rotation_index]
            email_msg.inbox_id = selected_inbox.inbox_id
            email_msg.from_email = selected_inbox.email_address
            logger.info(
                "[Scheduler] Bound sender inbox %s for message %s",
                selected_inbox.email_address,
                email_msg.message_id,
            )
            return selected_inbox.email_address

        fallback_sender = settings.SENDER_EMAIL or email_msg.from_email
        email_msg.inbox_id = None
        email_msg.from_email = fallback_sender
        logger.warning(
            "[Scheduler] No active campaign inboxes for %s. Falling back to system sender %s",
            email_msg.message_id,
            fallback_sender,
        )
        return fallback_sender
    
    async def process_scheduled_emails(self, db: Optional[Session] = None) -> dict:
        """
        Process all scheduled emails that are due.
        
        Returns:
            Dict with processing stats
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        stats = {
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "suppressed": 0,
            "skipped": 0
        }
        
        try:
            # Query emails due for sending
            now = datetime.utcnow()
            
            from sqlalchemy import or_
            from app.models.campaign import Campaign
            from app.schemas.campaign_schema import CampaignStatus

            # ── BUG FIX: JOIN Campaign so emails from PAUSED/COMPLETED campaigns
            # are never picked up by the scheduler. Without this join the scheduler
            # would happily send queued messages even while the campaign is paused.
            self._recover_stuck_sends(db)

            due = db.query(EmailMessage.message_id, EmailMessage.inbox_id).join(
                Campaign, Campaign.campaign_id == EmailMessage.campaign_id
            ).filter(
                and_(
                    EmailMessage.status.in_(["QUEUED", "SCHEDULED"]),
                    EmailMessage.scheduled_at <= now,
                    # Guard: never re-process an email that was already sent
                    EmailMessage.sent_at == None,
                    Campaign.status == CampaignStatus.ACTIVE.value,
                    # Respect retry backoff: either no retry scheduled, or retry time has passed
                    or_(
                        EmailMessage.next_retry_at == None,
                        EmailMessage.next_retry_at <= now
                    )
                )
            # Oldest first, so nothing waits behind newer messages (BR-DF-01)
            ).order_by(EmailMessage.scheduled_at, EmailMessage.message_id).limit(self.batch_size).all()

            if not due:
                logger.debug("[Scheduler] No scheduled emails to process")
                self._check_completed_campaigns(db)
                return stats

            # One queue per sending inbox, each capped to roughly one cycle of
            # sends at that inbox's pace; the inboxes then send in parallel.
            queues = defaultdict(list)
            for message_id, inbox_id in due:
                queues[inbox_id].append(message_id)
            delays = dict(db.query(SendingInbox.inbox_id, SendingInbox.delay_between_emails).filter(
                SendingInbox.inbox_id.in_([i for i in queues if i])).all()) if any(queues) else {}
            for inbox_id, ids in queues.items():
                delay = delays.get(inbox_id) or 0
                cap = max(1, math.ceil(self.INBOX_CYCLE_SECONDS / delay)) if delay > 0 else 50
                del ids[cap:]

            logger.info(
                f"[Scheduler] Processing {sum(len(v) for v in queues.values())} due emails "
                f"across {len(queues)} inbox queue(s)"
            )
            results = await asyncio.gather(*(self._run_inbox_queue(ids) for ids in queues.values()))
            for queue_stats in results:
                for key, value in queue_stats.items():
                    stats[key] += value

            db.expire_all()
            # Mark campaigns as completed if objective met
            self._check_completed_campaigns(db)
            
            logger.info(
                f"[Scheduler] Batch complete: "
                f"sent={stats['sent']}, failed={stats['failed']}, "
                f"suppressed={stats['suppressed']}"
            )
            
            return stats
            
        except Exception as e:
            logger.error(f"[Scheduler] Error processing batch: {e}")
            db.rollback()
            raise
        finally:
            if close_db:
                db.close()
                

    async def _run_inbox_queue(self, message_ids: List[str]) -> dict:
        """Send one inbox's due messages in order, at its pace, in its own session."""
        stats = defaultdict(int)
        db = SessionLocal()
        try:
            for message_id in message_ids:
                email_msg = db.query(EmailMessage).filter(EmailMessage.message_id == message_id).first()
                if not email_msg or email_msg.status not in ("QUEUED", "SCHEDULED") or email_msg.sent_at:
                    continue
                stats["processed"] += 1
                try:
                    result = await self._process_single_email(email_msg, db)
                    if email_msg.status in ("QUEUED", "SCHEDULED", "CANCELLED") and not email_msg.sent_at:
                        email_msg.send_key = None  # not sent: free the step for a later attempt
                    db.commit()
                except Exception as exc:  # one bad message must not stop the queue
                    logger.exception(f"[Scheduler] Message {message_id} errored: {exc}")
                    db.rollback()
                    result = "failed"
                stats[result if result in ("sent", "failed", "suppressed") else "skipped"] += 1
        finally:
            db.close()
        return stats

    def _recover_stuck_sends(self, db: Session) -> None:
        """
        A message claimed (SENDING) by a worker that then died is never retried
        automatically: SES may already have accepted it, and resending could
        email the contact twice (BR-DF-06). It is marked failed with the reason,
        so it shows a final status instead of hanging in SENDING forever.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=self.STUCK_SENDING_MINUTES)
        stuck = db.query(EmailMessage).filter(
            EmailMessage.status == "SENDING", EmailMessage.sent_at.is_(None),
            or_(EmailMessage.claimed_at < cutoff, EmailMessage.claimed_at.is_(None)),
        ).all()
        for msg in stuck:
            msg.status = "FAILED"
            msg.final_status, msg.final_status_at = "FAILED", datetime.utcnow()
            msg.failure_reason = ("Sending was interrupted and the outcome is unknown; "
                                  "not retried so the contact cannot receive it twice.")
        if stuck:
            db.commit()
            logger.warning(f"[Scheduler] Marked {len(stuck)} interrupted send(s) as failed")

    @staticmethod
    def send_key_for(email_msg: EmailMessage) -> Optional[str]:
        """Identity of 'this campaign step to this address' (BR-DF-06); None for ad-hoc mail."""
        if not (email_msg.campaign_id and email_msg.sequence_id and email_msg.to_email):
            return None
        return f"{email_msg.campaign_id}:{email_msg.sequence_id}:{email_msg.to_email.strip().lower()}"[:330]

    def _claim(self, email_msg: EmailMessage, db: Session) -> bool:
        """
        Atomically take ownership of a message for sending. Fails if another
        worker has it, or if this campaign step was already sent (or is being
        sent) to the same address, in which case the message is cancelled.
        """
        key = self.send_key_for(email_msg)
        if key:
            already = db.query(EmailMessage.message_id).filter(
                EmailMessage.campaign_id == email_msg.campaign_id,
                EmailMessage.sequence_id == email_msg.sequence_id,
                func.lower(EmailMessage.to_email) == email_msg.to_email.strip().lower(),
                EmailMessage.message_id != email_msg.message_id,
                or_(EmailMessage.sent_at.isnot(None), EmailMessage.status.in_(["SENDING", "SENT"])),
            ).first()
            if already:
                self._cancel_duplicate(email_msg, db, already[0])
                return False
        try:
            rows = db.query(EmailMessage).filter(
                EmailMessage.message_id == email_msg.message_id,
                EmailMessage.status.in_(["QUEUED", "SCHEDULED"]),
            ).update({"status": "SENDING", "send_key": key, "claimed_at": datetime.utcnow()},
                     synchronize_session=False)
            db.commit()
        except IntegrityError:
            # The unique send_key: a concurrent worker claimed the same step for this address
            db.rollback()
            self._cancel_duplicate(email_msg, db, None)
            return False
        if rows:
            db.refresh(email_msg)
        return bool(rows)

    def _cancel_duplicate(self, email_msg: EmailMessage, db: Session, original_id: Optional[str]) -> None:
        db.query(EmailMessage).filter(
            EmailMessage.message_id == email_msg.message_id,
            EmailMessage.status.in_(["QUEUED", "SCHEDULED"]),
        ).update({"status": "CANCELLED",
                  "failure_reason": "Duplicate: this step was already sent to this address"
                                    + (f" (message {original_id})" if original_id else "")},
                 synchronize_session=False)
        db.commit()
        logger.warning(f"[Scheduler] Cancelled duplicate send {email_msg.message_id} to {email_msg.to_email}")

    def _check_completed_campaigns(self, db: Session):
        """
        Automatically triggers when every lead in the campaign has reached a terminal state.
        Signals to the user that the objective is met and no further action is required.
        Terminal states: COMPLETED, REPLIED, BOUNCED, UNSUBSCRIBED.
        """
        try:
            from app.models.campaign import Campaign, CampaignProspect
            from app.schemas.campaign_schema import CampaignStatus
            from sqlalchemy import func

            # 1. Find all active campaigns
            active_campaigns = db.query(Campaign).filter(
                Campaign.status == CampaignStatus.ACTIVE.value
            ).all()

            # Non-terminal states (anything else implies objective is met)
            non_terminal_states = ["ACTIVE", "PAUSED", "OPENED", "CLICKED", "QUEUED", "SCHEDULED", "SENDING", "FAILED"]

            for c in active_campaigns:
                # Check total enrolled prospects
                total_prospects = db.query(func.count(CampaignProspect.id)).filter(
                    CampaignProspect.campaign_id == c.campaign_id
                ).scalar()
                
                if total_prospects == 0:
                    continue

                # 2. Check if there are any non-terminal prospects left
                active_prospects = db.query(func.count(CampaignProspect.id)).filter(
                    CampaignProspect.campaign_id == c.campaign_id,
                    CampaignProspect.status.in_(non_terminal_states)
                ).scalar()

                if active_prospects == 0:
                    logger.info(f"[Scheduler] Campaign {c.campaign_id} objective met. All {total_prospects} prospects hit terminal states. Marking COMPLETED.")
                    c.status = CampaignStatus.COMPLETED.value
            
            db.commit()
        except Exception as e:
            logger.error(f"[Scheduler] Error checking completed campaigns: {e}")
            db.rollback()
    
    async def _process_single_email(
        self, 
        email_msg: EmailMessage, 
        db: Session
    ) -> str:
        """
        Process a single email message.
        
        Returns:
            Status string: 'sent', 'failed', 'suppressed', 'skipped'
        """
        try:
            # ── PRE-FLIGHT: Check campaign status BEFORE claiming ownership.
            # This prevents the race condition where a campaign is paused
            # between the batch query and the atomic SENDING claim below.
            from app.models.campaign import Campaign
            from app.schemas.campaign_schema import CampaignStatus

            pre_campaign = db.query(Campaign).populate_existing().filter(
                Campaign.campaign_id == email_msg.campaign_id
            ).first()

            if pre_campaign and pre_campaign.status != CampaignStatus.ACTIVE.value:
                logger.info(
                    f"[Scheduler] Campaign {email_msg.campaign_id} is "
                    f"{pre_campaign.status} — skipping message {email_msg.message_id}, "
                    f"re-queuing for 1 hour."
                )
                email_msg.status = "QUEUED"
                email_msg.scheduled_at = datetime.utcnow() + timedelta(hours=1)
                db.commit()
                return "skipped"

            # Atomic ownership claim, with the duplicate-send guard (BR-DF-06)
            if not self._claim(email_msg, db):
                return "skipped"

            # Get prospect
            prospect = db.query(Prospect).filter(
                Prospect.prospect_id == email_msg.prospect_id
            ).first()
            
            if not prospect:
                logger.warning(f"[Scheduler] Prospect not found for message {email_msg.message_id}")
                email_msg.status = "FAILED"
                email_msg.failure_reason = "Prospect not found"
                return "failed"
            
            # Same suppression rule as enrollment (BR-DF-08)
            from app.services.suppression import blocked_reason
            reason = blocked_reason(db, prospect)
            if reason:
                logger.info(f"[Scheduler] {prospect.email} is suppressed ({reason}), skipping")
                email_msg.status = "CANCELLED"
                email_msg.failure_reason = reason
                return "suppressed"

            # Stop on reply / bounce / unsubscribe in this campaign (BR-DF-05): later
            # steps are queued at launch, so check the contact's state before each send.
            if email_msg.campaign_id and email_msg.direction != "INBOUND" and email_msg.sequence_id:
                enrollment = db.query(CampaignProspect.status).filter(
                    CampaignProspect.campaign_id == email_msg.campaign_id,
                    CampaignProspect.prospect_id == email_msg.prospect_id,
                ).first()
                if enrollment and enrollment[0] in ("REPLIED", "BOUNCED", "UNSUBSCRIBED"):
                    email_msg.status = "CANCELLED"
                    email_msg.failure_reason = f"Stopped: contact {enrollment[0].lower()}"
                    return "suppressed"
            
            # Get template. Manual unified-inbox replies can be queued without template_id,
            # using the message snapshot subject/body directly.
            template = None
            if email_msg.template_id:
                template = db.query(EmailTemplate).filter(
                    EmailTemplate.template_id == email_msg.template_id
                ).first()
                if not template:
                    logger.warning(f"[Scheduler] Template not found for message {email_msg.message_id}")
                    email_msg.status = "FAILED"
                    email_msg.failure_reason = "Template not found"
                    return "failed"
            else:
                template = SimpleNamespace(
                    subject=email_msg.subject or "Reply",
                    body=email_msg.body_text or ""
                )
            
            # Get sender name from campaign creator
            # Need to join campaign and user
            from app.models.campaign import Campaign
            from app.models.user import User
            
            sender_name = None
            campaign_data = db.query(Campaign, User).join(
                User, Campaign.created_by == User.user_id
            ).filter(
                Campaign.campaign_id == email_msg.campaign_id
            ).first()
            
            if campaign_data:
                campaign, user = campaign_data

                # ── BUG FIX (secondary guard): campaign status may have changed
                # between the batch query and now (e.g. paused mid-batch).
                # Re-queue for 1 hour instead of dropping/sending.
                from app.schemas.campaign_schema import CampaignStatus
                if campaign.status != CampaignStatus.ACTIVE.value:
                    logger.info(
                        f"[Scheduler] Campaign {campaign.campaign_id} is {campaign.status}, "
                        f"re-queuing message {email_msg.message_id} for 1 hour."
                    )
                    email_msg.status = "QUEUED"
                    email_msg.send_key = None
                    email_msg.scheduled_at = datetime.utcnow() + timedelta(hours=1)
                    return "skipped"

                sender_name = campaign.sender_name or f"{user.first_name} {user.last_name}"
            
            # ---------------------------------------------------------
            # STRICT SEND WINDOW GUARD
            # ---------------------------------------------------------
            if campaign_data:
                 from app.utils.business_calendar import is_within_send_window, get_timezone_for_state
                 
                 # Determine prospect timezone
                 prospect_tz = None
                 if campaign.respect_timezone and prospect.poc_state:
                     prospect_tz = get_timezone_for_state(prospect.poc_state, campaign.campaign_timezone)
                 
                 effective_tz = prospect_tz or campaign.campaign_timezone or "UTC"
                 
                 # Defensive defaults: campaigns may have NULL window fields.
                 window_start = campaign.send_window_start or time(9, 0)
                 window_end = campaign.send_window_end or time(17, 0)

                 is_valid_time = is_within_send_window(
                     current_utc=datetime.utcnow(),
                     timezone_str=effective_tz,
                     send_window_start=window_start,
                     send_window_end=window_end
                 )
                 
                 if not is_valid_time:
                     logger.info(
                         f"[Scheduler] Msg {email_msg.message_id} is outside send window "
                         f"for {effective_tz}. Re-queuing."
                     )
                     # Re-queue for next hour check
                     email_msg.status = "QUEUED"
                     email_msg.send_key = None
                     # Add small delay so we don't spam the checks immediately
                     email_msg.scheduled_at = datetime.utcnow() + timedelta(minutes=30)
                     return "skipped"

            # ---------------------------------------------------------
            # INBOX ROTATION & SELECTION LOGIC
            # ---------------------------------------------------------
            from_email_address = self._resolve_sender_for_message(email_msg, campaign, db)
            paced = False

            # ---------------------------------------------------------
            # PER-INBOX WARMUP & THROTTLE CHECK
            # ---------------------------------------------------------
            if from_email_address and email_msg.inbox_id:
                inbox = db.query(SendingInbox).filter(
                    SendingInbox.inbox_id == email_msg.inbox_id
                ).first()
                if inbox:
                    # Reset daily counter if new day
                    today = datetime.utcnow().date()
                    if not inbox.last_daily_reset or inbox.last_daily_reset.date() < today:
                        inbox.emails_sent_today = 0
                        inbox.last_daily_reset = datetime.utcnow()
                        # Advance warmup day based on start date
                        if inbox.warmup_enabled and inbox.warmup_start_date:
                            inbox.warmup_day = (datetime.utcnow() - inbox.warmup_start_date).days

                        # Visibility check (once/day): warn if the configured
                        # per-email pacing can't mathematically reach the
                        # configured daily cap within 24h, so a mismatch
                        # between delay_between_emails and daily_limit /
                        # max_emails_per_day shows up in logs instead of
                        # silently under-delivering (this was the exact
                        # failure mode behind the earlier ~330-contact cap).
                        configured_limit = email_sender.get_inbox_daily_limit(inbox)
                        if inbox.delay_between_emails and inbox.delay_between_emails > 0:
                            max_reachable = int(86400 / inbox.delay_between_emails)
                            if max_reachable < configured_limit:
                                logger.warning(
                                    f"[Scheduler] Inbox {inbox.email_address}: delay_between_emails="
                                    f"{inbox.delay_between_emails}s allows at most ~{max_reachable}/day, "
                                    f"below its configured limit of {configured_limit}/day. "
                                    f"Lower delay_between_emails (or the campaign send window) to actually "
                                    f"reach this cap."
                                )

                    # Get effective daily limit (respects warmup schedule + user override)
                    effective_limit = email_sender.get_inbox_daily_limit(inbox)

                    if (inbox.emails_sent_today or 0) >= effective_limit:
                        logger.info(
                            f"[Scheduler] Inbox {inbox.email_address} hit daily limit "
                            f"({effective_limit}), re-queuing message {email_msg.message_id}"
                        )
                        email_msg.status = "QUEUED"
                        email_msg.send_key = None
                        email_msg.scheduled_at = datetime.utcnow() + timedelta(
                            hours=inbox.cooling_period_hours
                        )
                        return "skipped"

                    # Per-inbox pace (BR-DF-01): the next send from this inbox waits
                    # delay_between_emails after the previous one, varied by up to
                    # +/- the jitter so the rhythm isn't machine-regular. The jitter
                    # averages out, so the configured pace is what is achieved.
                    if inbox.delay_between_emails and inbox.delay_between_emails > 0:
                        spread = min(settings.SENDING_JITTER_MAX_SECONDS, inbox.delay_between_emails * 0.25)
                        gap = inbox.delay_between_emails + random.uniform(-spread, spread)
                        if inbox.last_sent_at:
                            wait = gap - (datetime.utcnow() - inbox.last_sent_at).total_seconds()
                            if wait > 0:
                                await asyncio.sleep(wait)
                        paced = True

            # ---------------------------------------------------------
            # HUMAN-PACED SENDING JITTER (inboxes without their own pace)
            # ---------------------------------------------------------
            jitter_min = settings.SENDING_JITTER_MIN_SECONDS
            jitter_max = settings.SENDING_JITTER_MAX_SECONDS
            if not paced and jitter_max > 0 and jitter_min < jitter_max:
                jitter_delay = random.uniform(jitter_min, jitter_max)
                logger.debug(
                    f"[Scheduler] Applying {jitter_delay:.1f}s jitter for msg {email_msg.message_id}"
                )
                await asyncio.sleep(jitter_delay)

            # Ensure Unified Inbox threading context exists once inbox is known.
            self._ensure_conversation(email_msg, prospect, db)

            # Send via SES
            result = await email_sender.send_with_retry(
                email_message=email_msg,
                email_template=template,
                prospect=prospect,
                max_retries=email_msg.max_retries,
                sender_name=sender_name,
                from_email_address=from_email_address
            )
            
            if result["success"]:
                # Update message — commit IMMEDIATELY so no subsequent code in
                # this batch can overwrite status back to QUEUED via session state.
                email_msg.status = "SENT"
                email_msg.sent_at = datetime.utcnow()
                email_msg.from_email = from_email_address
                # Store the MIME Message-ID (not the SES ID) so IMAP sync can
                # match prospect replies via the In-Reply-To header.
                email_msg.provider_message_id = (
                    result.get("internet_message_id") or result.get("ses_message_id")
                )
                # SES's id, so delivery events without our tag still match (BR-DF-04)
                email_msg.ses_message_id = result.get("ses_message_id")

                # Update inbox warmup counters (in SQL, so parallel queues can't lose a count)
                if email_msg.inbox_id:
                    db.query(SendingInbox).filter(SendingInbox.inbox_id == email_msg.inbox_id).update({
                        SendingInbox.emails_sent_today: func.coalesce(SendingInbox.emails_sent_today, 0) + 1,
                        SendingInbox.last_sent_at: datetime.utcnow(),
                    }, synchronize_session=False)

                # Create sent event
                event = EmailEvent(
                    message_id=email_msg.message_id,
                    event_type=EmailEvent.EVENT_SENT,
                    event_time=datetime.utcnow(),
                    event_metadata={"ses_message_id": result.get("ses_message_id")}
                )
                db.add(event)

                # Commit SENT status immediately — prevents race condition where
                # a later email in the same batch hits a re-queue guard and the
                # outer db.commit() would overwrite this email's status to QUEUED.
                db.commit()

                # Update metrics
                from app.services.metrics_service import MetricsService
                if email_msg.campaign_id:
                    ms = MetricsService(db)
                    ms.process_event(email_msg.campaign_id, EmailEvent.EVENT_SENT)

                # Update campaign prospect step
                await self._update_prospect_step(email_msg, db)

                logger.info(
                    f"[Scheduler] Email sent to {prospect.email}, "
                    f"SES ID: {result.get('ses_message_id')}"
                )
                return "sent"
            else:
                # Update with failure
                email_msg.retry_count += 1
                
                if email_msg.retry_count >= email_msg.max_retries:
                    email_msg.status = "FAILED"
                    email_msg.failure_reason = result.get("error", "Unknown error")
                else:
                    # Schedule retry with exponential backoff
                    email_msg.status = "QUEUED"
                    email_msg.send_key = None
                    email_msg.next_retry_at = datetime.utcnow() + timedelta(
                        minutes=2 ** email_msg.retry_count
                    )
                    email_msg.last_error_code = result.get("error", "")[:50]
                if email_msg.status == "FAILED":
                    email_msg.final_status, email_msg.final_status_at = "FAILED", datetime.utcnow()
                
                logger.warning(
                    f"[Scheduler] Email failed for {prospect.email}: {result.get('error')}"
                )
                return "failed"
                
        except AmbiguousEmailFailure as e:
            # SES may have accepted the message before the connection failed:
            # retrying could send it twice (BR-DF-06), so stop here and say why.
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", datetime.utcnow()
            email_msg.failure_reason = f"Send outcome unknown, not retried to avoid a duplicate: {e}"[:1000]
            logger.error(f"[Scheduler] Ambiguous send failure for {email_msg.message_id}: {e}")
            return "failed"

        except TransientEmailFailure as e:
            # Retryable error
            email_msg.retry_count += 1
            email_msg.status = "QUEUED"
            email_msg.send_key = None
            email_msg.next_retry_at = datetime.utcnow() + timedelta(
                minutes=2 ** email_msg.retry_count
            )
            email_msg.last_error_code = str(e)[:50]
            logger.warning(f"[Scheduler] Transient failure: {e}")
            return "failed"
            
        except PermanentEmailFailure as e:
            # Non-retryable error
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", datetime.utcnow()
            email_msg.failure_reason = str(e)
            logger.error(f"[Scheduler] Permanent failure: {e}")

            # --- Hard Bounce Auto-Suppression ---
            # Permanent failures (MessageRejected, invalid address, etc.) must
            # auto-purge the recipient to keep bounce rate < 2%.
            if settings.AUTO_SUPPRESS_HARD_BOUNCES:
                self._auto_suppress_hard_bounce(email_msg, prospect, db, str(e))

            return "failed"
            
        except Exception as e:
            logger.error(f"[Scheduler] Unexpected error: {e}")
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", datetime.utcnow()
            email_msg.failure_reason = f"Unexpected: {str(e)[:200]}"
            return "failed"

    def _ensure_conversation(self, email_msg: EmailMessage, prospect: Prospect, db: Session) -> None:
        """
        Ensure message is linked to a conversation once inbox_id is known.
        This keeps Unified Inbox populated for campaign-generated messages.
        """
        if email_msg.conversation_id or not email_msg.inbox_id:
            return

        # Serialized against imap_sync_service's own find-or-create for the same
        # (prospect_id, inbox_id) key — see conversation_lock.py for why.
        with conversation_creation_lock:
            conv = db.query(Conversation).filter(
                Conversation.prospect_id == email_msg.prospect_id,
                Conversation.inbox_id == email_msg.inbox_id
            ).first()

            if not conv:
                conv = Conversation(
                    tenant_id=prospect.tenant_id,
                    prospect_id=email_msg.prospect_id,
                    inbox_id=email_msg.inbox_id,
                    subject=email_msg.subject,
                    status="OPEN",
                    is_unread=False,
                    last_message_at=datetime.utcnow()
                )
                db.add(conv)
                db.flush()

        email_msg.conversation_id = conv.id
    
    async def _update_prospect_step(
        self, 
        email_msg: EmailMessage, 
        db: Session
    ):
        """
        Update campaign prospect state after a successful send.
        - If there is a next sequence step, advance current_step and schedule next send.
        - If there is no next step, mark prospect as COMPLETED.
        Mirrors execution_service.py logic exactly.
        """
        try:
            campaign_prospect = db.query(CampaignProspect).filter(
                and_(
                    CampaignProspect.campaign_id == email_msg.campaign_id,
                    CampaignProspect.prospect_id == email_msg.prospect_id
                )
            ).first()

            if not campaign_prospect:
                return
            if campaign_prospect.status != "ACTIVE":
                # Something else (bounce, reply, unsubscribe) already changed
                # this prospect's status since the send was queued — don't
                # blindly advance/complete over it.
                return

            # Find the step number for the email that was just sent
            sent_seq_step = db.query(EmailSequence).filter(
                EmailSequence.sequence_id == email_msg.sequence_id
            ).first()

            if sent_seq_step:
                next_step_number = sent_seq_step.step_number + 1
            else:
                # Fallback: increment from current tracked step
                next_step_number = campaign_prospect.current_step + 1

            # Check if a next step actually exists for this campaign
            next_step = db.query(EmailSequence).filter(
                EmailSequence.campaign_id == email_msg.campaign_id,
                EmailSequence.step_number == next_step_number
            ).first()

            if next_step:
                # More steps remain — advance to next step and schedule
                campaign_prospect.current_step = next_step_number
                campaign_prospect.next_scheduled_at = datetime.utcnow() + timedelta(days=next_step.wait_days)
                logger.info(
                    f"[Scheduler] Prospect {email_msg.prospect_id} advanced to step {next_step_number}, "
                    f"next send in {next_step.wait_days} day(s)"
                )
            else:
                # No more steps — prospect has completed the entire sequence
                set_prospect_status(campaign_prospect, "COMPLETED")
                campaign_prospect.next_scheduled_at = None
                logger.info(
                    f"[Scheduler] Prospect {email_msg.prospect_id} completed all sequence steps "
                    f"for campaign {email_msg.campaign_id}"
                )

        except Exception as e:
            logger.exception(f"[Scheduler] Failed to update prospect step: {e}")

    def _auto_suppress_hard_bounce(
        self,
        email_msg: EmailMessage,
        prospect: Prospect,
        db: Session,
        error_details: str,
    ):
        """
        Auto-suppress a recipient after a permanent/hard bounce.
        - Adds the email to GlobalUnsubscribe with reason HARD_BOUNCE_AUTO_SUPPRESSED
        - Cancels all remaining queued emails for this prospect in this campaign
        - Logs the action for audit

        This keeps the sending domain's bounce rate under the 2% threshold
        required by major mailbox providers (Microsoft, Google).
        """
        try:
            # Applies across every campaign in the workspace (BR-DF-08)
            from app.services.suppression import suppress
            suppress(db, prospect.tenant_id, prospect.email,
                     f"HARD_BOUNCE_AUTO_SUPPRESSED: {error_details[:200]}", kind="HARD_BOUNCE")
            db.flush()
            from app.services.send_safety import check_campaign_health
            check_campaign_health(db, email_msg.campaign_id, trigger="hard bounce at send time")
        except Exception as e:
            logger.error(f"[Scheduler] Failed to auto-suppress hard bounce: {e}")
    
    async def run_continuous(self, interval_seconds: int = 30):
        """
        Run scheduler continuously in background.
        
        Args:
            interval_seconds: Seconds between processing batches
        """
        if self.is_running:
            logger.warning("[Scheduler] Already running")
            return
        
        self.is_running = True
        logger.info(f"[Scheduler] Starting continuous mode, interval={interval_seconds}s")
        
        while self.is_running:
            stats = {}
            try:
                stats = await self.process_scheduled_emails() or {}
            except Exception as e:
                logger.error(f"[Scheduler] Error in continuous run: {e}")
            
            # Come straight back while there is work, so pace is set by the inboxes (BR-DF-01)
            await asyncio.sleep(2 if stats.get("processed") else interval_seconds)
    
    def stop(self):
        """Stop continuous scheduler."""
        self.is_running = False
        logger.info("[Scheduler] Stopping")


# Create singleton instance
email_scheduler = EmailSchedulerService()


# =============================
# Manual Trigger Functions
# =============================

async def trigger_email_processing():
    """
    Manually trigger email processing.
    Can be called from an API endpoint or cron job.
    """
    return await email_scheduler.process_scheduled_emails()


def process_emails_sync():
    """
    Synchronous wrapper for email processing.
    Useful for cron jobs or management commands.
    """
    return asyncio.run(email_scheduler.process_scheduled_emails())
