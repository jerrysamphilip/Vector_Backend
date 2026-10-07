# app/services/email_scheduler_service.py
"""
Email Scheduler Service
Processes scheduled emails from the queue and sends via AWS SES.
"""

import asyncio
import logging
from datetime import datetime, timedelta, time
from typing import List, Optional
from types import SimpleNamespace

from sqlalchemy.orm import Session
from sqlalchemy import and_

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
    PermanentEmailFailure
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
    
    def __init__(self):
        self.batch_size = 100  # Process N emails per batch
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
            scheduled_emails = db.query(EmailMessage).join(
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
            ).limit(self.batch_size).all()

            # ── CRITICAL: Expire ALL objects loaded by the JOIN above.
            # SQLAlchemy caches Campaign rows in the session identity map with
            # the status they had at query time (ACTIVE). If the campaign gets
            # paused AFTER this point, any subsequent db.query(Campaign) inside
            # _process_single_email will return the stale cached ACTIVE object
            # instead of hitting the DB again. expire_all() forces a fresh SELECT
            # next time any attribute is accessed on any cached object.
            db.expire_all()
            
            if not scheduled_emails:
                logger.debug("[Scheduler] No scheduled emails to process")
                self._check_completed_campaigns(db)
                return stats
            
            logger.info(f"[Scheduler] Processing {len(scheduled_emails)} scheduled emails")
            
            for email_msg in scheduled_emails:
                stats["processed"] += 1
                result = await self._process_single_email(email_msg, db)
                
                if result == "sent":
                    stats["sent"] += 1
                elif result == "failed":
                    stats["failed"] += 1
                elif result == "suppressed":
                    stats["suppressed"] += 1
                else:
                    stats["skipped"] += 1
            
            db.commit()
            
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

            # Atomic ownership claim to prevent race conditions
            rows = db.query(EmailMessage).filter(
                EmailMessage.message_id == email_msg.message_id,
                EmailMessage.status.in_(["QUEUED", "SCHEDULED"])
            ).update({"status": "SENDING"}, synchronize_session=False)
            db.commit()
            
            if rows == 0:
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
            
            # Check suppression list
            is_suppressed = db.query(GlobalUnsubscribe).filter(
                and_(
                    GlobalUnsubscribe.email == prospect.email,
                    GlobalUnsubscribe.tenant_id == prospect.tenant_id
                )
            ).first()
            
            if is_suppressed:
                logger.info(f"[Scheduler] Email {prospect.email} is suppressed, skipping")
                email_msg.status = "CANCELLED"
                email_msg.failure_reason = f"Suppressed: {is_suppressed.reason}"
                return "suppressed"
            
            # Check prospect consent
            if prospect.consent_status == "UNSUBSCRIBED":
                logger.info(f"[Scheduler] Prospect {prospect.email} unsubscribed, skipping")
                email_msg.status = "CANCELLED"
                email_msg.failure_reason = "Prospect unsubscribed"
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
                     # Add small delay so we don't spam the checks immediately
                     email_msg.scheduled_at = datetime.utcnow() + timedelta(minutes=30)
                     return "skipped"

            # ---------------------------------------------------------
            # INBOX ROTATION & SELECTION LOGIC
            # ---------------------------------------------------------
            from_email_address = self._resolve_sender_for_message(email_msg, campaign, db)

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
                        email_msg.scheduled_at = datetime.utcnow() + timedelta(
                            hours=inbox.cooling_period_hours
                        )
                        return "skipped"

                    # Per-email delay (throttle between sends for this inbox)
                    if inbox.delay_between_emails and inbox.delay_between_emails > 0 and inbox.last_sent_at:
                        seconds_since_last = (datetime.utcnow() - inbox.last_sent_at).total_seconds()
                        if seconds_since_last < inbox.delay_between_emails:
                            remaining = inbox.delay_between_emails - seconds_since_last
                            await asyncio.sleep(remaining)

            # ---------------------------------------------------------
            # HUMAN-PACED SENDING JITTER
            # ---------------------------------------------------------
            # Add random delay between sends to mimic human behaviour.
            # Without jitter, sending 50 emails exactly N seconds apart
            # creates a machine-like pattern that spam filters detect.
            jitter_min = settings.SENDING_JITTER_MIN_SECONDS
            jitter_max = settings.SENDING_JITTER_MAX_SECONDS
            if jitter_max > 0 and jitter_min < jitter_max:
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

                # Update inbox warmup counters
                if email_msg.inbox_id:
                    sent_inbox = db.query(SendingInbox).filter(
                        SendingInbox.inbox_id == email_msg.inbox_id
                    ).first()
                    if sent_inbox:
                        sent_inbox.emails_sent_today = (sent_inbox.emails_sent_today or 0) + 1
                        sent_inbox.last_sent_at = datetime.utcnow()

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
                    email_msg.next_retry_at = datetime.utcnow() + timedelta(
                        minutes=2 ** email_msg.retry_count
                    )
                    email_msg.last_error_code = result.get("error", "")[:50]
                
                logger.warning(
                    f"[Scheduler] Email failed for {prospect.email}: {result.get('error')}"
                )
                return "failed"
                
        except TransientEmailFailure as e:
            # Retryable error
            email_msg.retry_count += 1
            email_msg.status = "QUEUED"
            email_msg.next_retry_at = datetime.utcnow() + timedelta(
                minutes=2 ** email_msg.retry_count
            )
            email_msg.last_error_code = str(e)[:50]
            logger.warning(f"[Scheduler] Transient failure: {e}")
            return "failed"
            
        except PermanentEmailFailure as e:
            # Non-retryable error
            email_msg.status = "FAILED"
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
            # 1. Add to GlobalUnsubscribe (skip if already exists)
            existing_unsub = db.query(GlobalUnsubscribe).filter(
                and_(
                    GlobalUnsubscribe.email == prospect.email,
                    GlobalUnsubscribe.tenant_id == prospect.tenant_id,
                )
            ).first()

            if not existing_unsub:
                unsub = GlobalUnsubscribe(
                    tenant_id=prospect.tenant_id,
                    email=prospect.email,
                    unsubscribed_at=datetime.utcnow(),
                    reason=f"HARD_BOUNCE_AUTO_SUPPRESSED: {error_details[:200]}",
                )
                db.merge(unsub)
                logger.warning(
                    f"[Scheduler] Hard bounce auto-suppressed: {prospect.email} "
                    f"(Error: {error_details[:100]})"
                )

            # 2. Cancel all remaining queued emails for this prospect in this campaign
            remaining = db.query(EmailMessage).filter(
                and_(
                    EmailMessage.campaign_id == email_msg.campaign_id,
                    EmailMessage.prospect_id == email_msg.prospect_id,
                    EmailMessage.status.in_(["QUEUED", "SCHEDULED"]),
                    EmailMessage.message_id != email_msg.message_id,
                )
            ).all()

            for msg in remaining:
                msg.status = "CANCELLED"
                msg.failure_reason = f"Hard bounce auto-suppression for {prospect.email}"

            if remaining:
                logger.info(
                    f"[Scheduler] Cancelled {len(remaining)} remaining emails "
                    f"for hard-bounced prospect {prospect.email}"
                )

            # Reflect the hard bounce on the prospect's status for this campaign
            # (this is the synchronous, send-time bounce path — SES rejected the
            # send outright rather than the async SNS bounce webhook).
            campaign_prospect = db.query(CampaignProspect).filter(
                and_(
                    CampaignProspect.campaign_id == email_msg.campaign_id,
                    CampaignProspect.prospect_id == email_msg.prospect_id,
                )
            ).first()
            set_prospect_status(
                campaign_prospect,
                "BOUNCED",
                stopped_reason=f"Hard bounce (send-time): {error_details[:200]}"
            )

            db.flush()
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
            try:
                await self.process_scheduled_emails()
            except Exception as e:
                logger.error(f"[Scheduler] Error in continuous run: {e}")
            
            await asyncio.sleep(interval_seconds)
    
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
