# app/services/email_scheduler_service.py
"""
Email Scheduler Service
Processes scheduled emails from the queue and sends via AWS SES.
"""

import asyncio
import logging
import math
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta, time
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session, aliased
from sqlalchemy import Exists, and_, exists, func, or_

from app.core.clock import utcnow
from app.core.database import SessionLocal
from app.core.config import settings
from app.core.auth import BLOCKED_TENANT_STATUSES
from app.core.observability import record_email_sent
from app.services.metrics_service import MetricsService
from app.services.notifications import notify
from app.models.tenant import Tenant
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
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.user import User
from app.services.email_sender_service import (
    email_sender,
    DeferredEmailFailure,
    TransientEmailFailure,
    PermanentEmailFailure,
    AmbiguousEmailFailure,
)
from app.services import send_breaker
from app.services.send_guards import NO_INBOX_RETRY_MINUTES, active_inboxes
from app.services.send_pacing import next_gap, seconds_until_ready
from app.services.sequence_stop import PENDING as STOPPABLE_STATUSES, stop_reason, stops_sequence
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

    # Fair batching (B04): each sending lane (an inbox, or a campaign with no
    # inbox) contributes at most this many due rows per cycle, so one large
    # backlog can never starve other inboxes or tenants. The per-inbox pace
    # trims each lane further (see process_scheduled_emails).
    LANE_CAP = 120
    # Statuses of a not-yet-sent step that must go out before the next step (B01)
    PENDING_STATUSES = ("QUEUED", "SCHEDULED", "SENDING", "PAUSED_BY_CAMPAIGN")
    # A pass held back only by pacing is reported as work (so the job loop comes straight back)
    # when the soonest mailbox is ready within this many seconds; else the normal interval applies
    PACE_RECHECK_SECONDS = 30

    def __init__(self) -> None:
        # Hard ceiling on due rows considered per cycle (all lanes). Rows are
        # taken round-robin across lanes (ROW_NUMBER per lane), so the ceiling
        # never favours one inbox's backlog over another's.
        self.batch_size = 5000
        # Lanes sending at once; each holds a DB session, so stay inside the pool
        self.max_parallel_lanes = max(2, int(getattr(settings, "DB_POOL_SIZE", 10) or 10) // 2)
        self.is_running = False
        # When each inbox may send again (monotonic clock, jittered gap drawn at its last send in
        # this process); the DB's last_sent_at covers sends from other processes (send_pacing.py).
        self._ready_at: Dict[str, float] = {}
        from app.services.daily_limit import DailyLimiter
        self._daily = DailyLimiter()

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

        None when the campaign has no ACTIVE inbox: campaign mail is never sent from the
        platform address (item 12); the caller leaves the message queued.
        """
        inboxes = active_inboxes(campaign)
        active_by_id = {inbox.inbox_id: inbox for inbox in inboxes}
        active_by_email = {
            (inbox.email_address or "").strip().lower(): inbox
            for inbox in inboxes
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

        if inboxes:
            rotation_key = f"{email_msg.campaign_id}:{email_msg.prospect_id}"
            rotation_index = sum(ord(ch) for ch in rotation_key) % len(inboxes)
            selected_inbox = inboxes[rotation_index]
            email_msg.inbox_id = selected_inbox.inbox_id
            email_msg.from_email = selected_inbox.email_address
            logger.info(
                "[Scheduler] Bound sender inbox %s for message %s",
                selected_inbox.email_address,
                email_msg.message_id,
            )
            return selected_inbox.email_address

        logger.warning(
            "[Scheduler] Campaign %s has no active sending inbox: message %s stays queued",
            email_msg.campaign_id,
            email_msg.message_id,
        )
        return None
    
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
        
        stats: Dict[str, Any] = {
            "processed": 0,
            "sent": 0,
            "failed": 0,
            "suppressed": 0,
            "skipped": 0,
            "paced": 0,  # due, but the inbox's gap since its last send has not passed yet
        }
        
        try:
            # Query emails due for sending
            now = utcnow()
            
            from sqlalchemy import or_
            from app.models.campaign import Campaign
            from app.schemas.campaign_schema import CampaignStatus

            # ── BUG FIX: JOIN Campaign so emails from PAUSED/COMPLETED campaigns
            # are never picked up by the scheduler. Without this join the scheduler
            # would happily send queued messages even while the campaign is paused.
            self._recover_stuck_sends(db)

            # SES / the platform keeps failing: send nothing until the cool-down ends (item 14)
            if send_breaker.platform_breaker.is_open():
                logger.warning(f"[Scheduler] Sending stopped by the circuit breaker for another "
                               f"{send_breaker.platform_breaker.retry_in():.0f}s")
                return stats

            due = self._select_due(db, now)

            if not due:
                logger.debug("[Scheduler] No scheduled emails to process")
                self._check_completed_campaigns(db)
                return stats

            # One queue per sending lane (inbox), each capped to roughly one cycle
            # of sends at that inbox's pace; the lanes then send in parallel.
            queues = defaultdict(list)
            lane_inbox = {}
            for message_id, inbox_id, lane in due:
                queues[lane].append(message_id)
                lane_inbox[lane] = inbox_id
            inbox_ids = [i for i in lane_inbox.values() if i]
            delays = dict(db.query(SendingInbox.inbox_id, SendingInbox.delay_between_emails).filter(
                SendingInbox.inbox_id.in_(inbox_ids)).all()) if inbox_ids else {}
            for lane, ids in queues.items():
                delay = delays.get(lane_inbox.get(lane)) or 0
                cap = max(1, math.ceil(self.INBOX_CYCLE_SECONDS / delay)) if delay > 0 else 50
                del ids[cap:]

            logger.info(
                f"[Scheduler] Processing {sum(len(v) for v in queues.values())} due emails "
                f"across {len(queues)} inbox queue(s)"
            )
            gate = asyncio.Semaphore(self.max_parallel_lanes)

            async def _lane(ids):
                async with gate:
                    return await self._run_inbox_queue(ids)

            results = await asyncio.gather(*(_lane(ids) for ids in queues.values()))
            for queue_stats in results:
                for key, value in queue_stats.items():
                    stats[key] += value
            self._note_pacing(stats)

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
                

    def _earlier_step_pending(self, msg_entity, step_entity) -> Exists:
        """
        SQL condition: this prospect still has an earlier step of the same
        campaign that has not been sent yet (B01). A follow-up is only due once
        the step before it has actually gone out, whatever its scheduled_at says.
        """
        prev = aliased(EmailMessage)
        prev_step = aliased(EmailSequence)
        return exists().where(
            prev.campaign_id == msg_entity.campaign_id,
            prev.prospect_id == msg_entity.prospect_id,
            prev.message_id != msg_entity.message_id,
            prev.sent_at.is_(None),
            prev.status.in_(self.PENDING_STATUSES),
            prev_step.sequence_id == prev.sequence_id,
            prev_step.step_number < step_entity.step_number,
        )

    def _select_due(self, db: Session, now: datetime) -> Query:
        """
        Due rows as (message_id, inbox_id, lane), fair across lanes (B04).

        A lane is the sending inbox (or the campaign, for messages with no
        inbox). ROW_NUMBER() per lane caps each lane at LANE_CAP rows and the
        result is ordered round-robin (every lane's 1st row, then every lane's
        2nd, ...), so a big backlog in one inbox or tenant cannot crowd out the
        rest. Follow-ups whose previous step is still unsent are held (B01).
        Ownership is still taken per message by _claim (send_key), unchanged.
        """
        from app.models.campaign import Campaign
        from app.schemas.campaign_schema import CampaignStatus

        step = aliased(EmailSequence)
        lane = func.coalesce(EmailMessage.inbox_id, EmailMessage.campaign_id)
        rn = func.row_number().over(
            partition_by=lane, order_by=(EmailMessage.scheduled_at, EmailMessage.message_id)
        )
        inner = (
            db.query(
                EmailMessage.message_id.label("message_id"),
                EmailMessage.inbox_id.label("inbox_id"),
                lane.label("lane"),
                EmailMessage.scheduled_at.label("scheduled_at"),
                rn.label("rn"),
            )
            # JOIN Campaign so emails from PAUSED/COMPLETED campaigns are never picked up
            .join(Campaign, Campaign.campaign_id == EmailMessage.campaign_id)
            # A suspended / deleted workspace sends nothing until it is reactivated
            .join(Tenant, Tenant.tenant_id == Campaign.tenant_id)
            .outerjoin(step, step.sequence_id == EmailMessage.sequence_id)
            .filter(
                EmailMessage.status.in_(["QUEUED", "SCHEDULED"]),
                EmailMessage.scheduled_at <= now,
                # Guard: never re-process an email that was already sent
                EmailMessage.sent_at.is_(None),
                Campaign.status == CampaignStatus.ACTIVE.value,
                func.upper(func.coalesce(Tenant.status, "ACTIVE")).notin_(sorted(BLOCKED_TENANT_STATUSES)),
                # Respect retry backoff: either no retry scheduled, or retry time has passed
                or_(EmailMessage.next_retry_at.is_(None), EmailMessage.next_retry_at <= now),
                ~self._earlier_step_pending(EmailMessage, step),
            )
            .subquery()
        )
        return (
            db.query(inner.c.message_id, inner.c.inbox_id, inner.c.lane)
            .filter(inner.c.rn <= self.LANE_CAP)
            # Round-robin across lanes; oldest first within a lane (BR-DF-01)
            .order_by(inner.c.rn, inner.c.scheduled_at, inner.c.message_id)
            .limit(self.batch_size)
            .all()
        )

    def earlier_step_unsent(self, email_msg: EmailMessage, db: Session) -> bool:
        """Python twin of _earlier_step_pending, for a single message."""
        if not (email_msg.campaign_id and email_msg.sequence_id):
            return False
        step = aliased(EmailSequence)
        return db.query(EmailMessage.message_id).join(
            step, step.sequence_id == email_msg.sequence_id
        ).filter(
            EmailMessage.message_id == email_msg.message_id,
            self._earlier_step_pending(EmailMessage, step),
        ).first() is not None

    async def _run_inbox_queue(self, message_ids: List[str]) -> dict:
        """
        Send one inbox's due messages in order, in its own session, without waiting (F021): once
        the inbox is not ready (its pacing gap since the last send has not passed) or the circuit
        breaker is open, the rest of the lane stays due for a later pass, still in order.
        """
        stats: Dict[str, int] = defaultdict(int)
        db = SessionLocal()
        try:
            for message_id in message_ids:
                if send_breaker.platform_breaker.is_open():
                    break
                email_msg = db.query(EmailMessage).filter(EmailMessage.message_id == message_id).first()
                if not email_msg or email_msg.status not in ("QUEUED", "SCHEDULED") or email_msg.sent_at:
                    continue
                if email_msg.inbox_id and self.pace_wait(db, email_msg.inbox_id) > 0:
                    stats["paced"] += 1
                    break
                stats["processed"] += 1
                try:
                    result = await self._process_single_email(email_msg, db)
                    if result == "paced":
                        stats["processed"] -= 1
                        stats["paced"] += 1
                    if email_msg.status in ("QUEUED", "SCHEDULED", "CANCELLED") and not email_msg.sent_at:
                        email_msg.send_key = None  # not sent: free the step for a later attempt
                    db.commit()
                except Exception as exc:  # one bad message must not stop the queue
                    logger.exception(f"[Scheduler] Message {message_id} errored: {exc}")
                    db.rollback()
                    result = "failed"
                if result == "paced":
                    break
                stats[result if result in ("sent", "failed", "suppressed") else "skipped"] += 1
        finally:
            db.close()
        return stats

    def pace_wait(self, db: Session, inbox_id: str) -> float:
        """Seconds until this inbox may send again (0: ready now). Never sleeps (F021)."""
        row = db.query(SendingInbox.delay_between_emails, SendingInbox.last_sent_at).filter(
            SendingInbox.inbox_id == inbox_id).first()
        if not row:
            return 0.0
        return seconds_until_ready(
            row[0], settings.SENDING_JITTER_MAX_SECONDS, now=utcnow(), last_sent_at=row[1],
            now_mono=_time.monotonic(), ready_at_mono=self._ready_at.get(inbox_id))

    def _note_sent_for_pacing(self, inbox_id: Optional[str], delay: Optional[int]) -> None:
        """Draw this inbox's next gap now, once, so its readiness does not flicker between checks."""
        if inbox_id:
            self._ready_at[inbox_id] = _time.monotonic() + next_gap(
                delay, settings.SENDING_JITTER_MIN_SECONDS, settings.SENDING_JITTER_MAX_SECONDS)

    def _note_pacing(self, stats: Dict[str, Any]) -> None:
        """
        recheck_in: seconds until the soonest paced inbox is ready. A pass held back only by
        pacing counts as work when that is within PACE_RECHECK_SECONDS, so the job loop comes
        straight back instead of waiting its full interval (which would slow every paced inbox).
        """
        if not stats.get("paced"):
            return
        now_mono = _time.monotonic()
        pending = [at - now_mono for at in self._ready_at.values() if at > now_mono]
        stats["recheck_in"] = min(pending) if pending else 0.0
        if stats["recheck_in"] <= self.PACE_RECHECK_SECONDS:
            stats["processed"] += stats["paced"]

    def _recover_stuck_sends(self, db: Session) -> None:
        """
        A message claimed (SENDING) by a worker that then died is never retried
        automatically: SES may already have accepted it, and resending could
        email the contact twice (BR-DF-06).

        If there is evidence SES did accept it (a provider/SES message id was
        recorded, or SES has already reported an event for it) it is marked SENT
        with no final status, so delivery reconciliation gives it DELIVERED /
        UNCONFIRMED like any other send (B08). Otherwise it is marked FAILED
        with the reason, instead of hanging in SENDING forever.

        Only claims older than STUCK_SENDING_MINUTES are reaped, so a send that is
        in flight right now (scheduler or send-now) is never touched. A SENDING row
        without a claim time (written before every claimant stamped one) gets one
        now and is reaped a full threshold later if it is still SENDING then.
        """
        self._date_undated_claims(db)
        cutoff = utcnow() - timedelta(minutes=self.STUCK_SENDING_MINUTES)
        stuck = db.query(EmailMessage).filter(
            EmailMessage.status == "SENDING", EmailMessage.sent_at.is_(None),
            EmailMessage.claimed_at < cutoff,
        ).all()
        if not stuck:
            return
        provider_events = {
            mid for (mid,) in db.query(EmailEvent.message_id).filter(
                EmailEvent.message_id.in_([m.message_id for m in stuck]),
                EmailEvent.event_type != EmailEvent.EVENT_SENT,
            ).distinct()
        }
        accepted = failed = 0
        now = utcnow()
        for msg in stuck:
            if self._provider_accepted(msg, provider_events):
                msg.status = "SENT"
                msg.sent_at = msg.claimed_at or now
                msg.failure_reason = ("Sending was interrupted after the provider accepted the "
                                      "message; recorded as sent.")
                accepted += 1
            else:
                msg.status = "FAILED"
                msg.final_status, msg.final_status_at = "FAILED", now
                msg.failure_reason = ("Sending was interrupted and the outcome is unknown; "
                                      "not retried so the contact cannot receive it twice.")
                failed += 1
        db.commit()
        logger.warning(f"[Scheduler] Interrupted sends: {accepted} recorded as sent "
                       f"(provider accepted), {failed} marked failed")

    # Outcomes the SES webhook may record on a row that is still SENDING (delivery or bounce
    # arrived before the sender committed SENT): proof the provider accepted the message
    PROVIDER_OUTCOMES = ("DELIVERED", "BOUNCED", "COMPLAINED")

    @classmethod
    def _provider_accepted(cls, msg: EmailMessage, provider_events: set) -> bool:
        """Evidence SES accepted a stuck SENDING message: a provider id, a delivery, an outcome or an event."""
        return bool(msg.ses_message_id or msg.provider_message_id or msg.delivered_at
                    or msg.final_status in cls.PROVIDER_OUTCOMES or msg.message_id in provider_events)

    @staticmethod
    def _date_undated_claims(db: Session) -> None:
        """Start the stuck-send clock for legacy SENDING rows that carry no claimed_at."""
        dated = db.query(EmailMessage).filter(
            EmailMessage.status == "SENDING", EmailMessage.sent_at.is_(None),
            EmailMessage.claimed_at.is_(None),
        ).update({"claimed_at": utcnow()}, synchronize_session=False)
        # Commit even when nothing matched: under REPEATABLE READ the UPDATE still holds next-key locks on
        # the scanned status='SENDING' index range, and this session stays open for the whole batch, so
        # every lane's claim (QUEUED -> SENDING) would wait on it until the read timeout.
        db.commit()
        if dated:
            logger.info(f"[Scheduler] {dated} SENDING message(s) had no claim time; dated now")

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
            ).update({"status": "SENDING", "send_key": key, "claimed_at": utcnow()},
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

    def _check_completed_campaigns(self, db: Session) -> None:
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
                email_msg.scheduled_at = utcnow() + timedelta(hours=1)
                db.commit()
                return "skipped"

            # A follow-up never goes out before the step ahead of it (B01). The due
            # query already holds these back; this covers a race with a re-queue.
            if self.earlier_step_unsent(email_msg, db):
                logger.info(f"[Scheduler] Holding {email_msg.message_id}: the previous step "
                            f"for this contact has not been sent yet")
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
            # steps are queued at launch, so check the contact's state before each send,
            # under this step's own stop_on_reply / stop_on_bounce flags (item 20).
            stopped = self.stopped_by_enrollment(email_msg, db)
            if stopped:
                email_msg.status = "CANCELLED"
                email_msg.failure_reason = stop_reason(stopped)
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
                    email_msg.scheduled_at = utcnow() + timedelta(hours=1)
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
                     current_utc=utcnow(),
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
                     email_msg.scheduled_at = utcnow() + timedelta(minutes=30)
                     return "skipped"

            # ---------------------------------------------------------
            # INBOX ROTATION & SELECTION LOGIC
            # ---------------------------------------------------------
            from_email_address = self._resolve_sender_for_message(email_msg, campaign, db)
            if not from_email_address:
                return self._hold_without_inbox(email_msg)

            # ---------------------------------------------------------
            # PER-INBOX WARMUP & THROTTLE CHECK
            # ---------------------------------------------------------
            if email_msg.inbox_id:
                inbox = db.query(SendingInbox).filter(
                    SendingInbox.inbox_id == email_msg.inbox_id
                ).first()
                if inbox:
                    # Reset daily counter if new day
                    today = utcnow().date()
                    if not inbox.last_daily_reset or inbox.last_daily_reset.date() < today:
                        inbox.emails_sent_today = 0
                        inbox.last_daily_reset = utcnow()
                        # Advance warmup day based on start date
                        if inbox.warmup_enabled and inbox.warmup_start_date:
                            inbox.warmup_day = (utcnow() - inbox.warmup_start_date).days

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
                        email_msg.scheduled_at = utcnow() + timedelta(
                            hours=inbox.cooling_period_hours
                        )
                        return "skipped"

                    # Per-inbox pace (BR-DF-01, F021): the next send from this inbox is due
                    # delay_between_emails (jittered) after the previous one. Not ready yet:
                    # give the claim back and leave the email due for a later pass; never
                    # sleep here, so no inbox holds up another (send_pacing.py).
                    if self.pace_wait(db, inbox.inbox_id) > 0:
                        email_msg.status = "QUEUED"
                        email_msg.send_key = None
                        return "paced"

            # ---------------------------------------------------------
            # DAILY NEW-CONTACT LIMIT (BR-OV-01): first steps only; follow-ups always go
            # ---------------------------------------------------------
            limit_user = None
            from app.services import daily_limit
            if email_msg.direction != "INBOUND" and daily_limit.is_first_step(db, email_msg):
                limit_user = prospect.owner_id or (campaign.created_by if campaign_data else None)
                if limit_user and not self._daily.reserve(db, limit_user):
                    email_msg.status = "QUEUED"
                    email_msg.send_key = None
                    email_msg.scheduled_at = daily_limit.next_day_start()
                    email_msg.last_error_code = daily_limit.HELD_CODE
                    email_msg.failure_reason = (f"Held for tomorrow: the daily limit of "
                                                f"{settings.DAILY_NEW_CONTACT_LIMIT} new contacts was reached")
                    logger.info(f"[Scheduler] Daily new-contact limit reached for user {limit_user}; "
                                f"message {email_msg.message_id} moved to tomorrow")
                    return "skipped"

            # Ensure Unified Inbox threading context exists once inbox is known.
            self._ensure_conversation(email_msg, prospect, db)

            # Restart the stuck-send clock now: pacing waits above happen after the
            # claim, and the SENDING sweeper must only see time actually spent in SES.
            email_msg.claimed_at = utcnow()
            db.commit()

            # Send via SES. Permanent errors (rejected / invalid address) raise
            # PermanentEmailFailure and are never retried; temporary ones are
            # retried inside send_with_retry and raise TransientEmailFailure when
            # those attempts run out (B06).
            try:
                result = await email_sender.send_with_retry(
                    email_message=email_msg,
                    email_template=template,
                    prospect=prospect,
                    max_retries=email_msg.max_retries,
                    sender_name=sender_name,
                    from_email_address=from_email_address
                )
            except (TransientEmailFailure, PermanentEmailFailure):
                if limit_user:
                    self._daily.release(limit_user)  # nothing went out
                raise

            if not result["success"] and limit_user:
                self._daily.release(limit_user)
            if result["success"]:
                if email_msg.last_error_code == daily_limit.HELD_CODE:
                    email_msg.last_error_code, email_msg.failure_reason = None, None
                return await self._record_sent(email_msg, result, from_email_address, prospect, db)
            else:
                # Refused before reaching SES (personal address, spam lint): the
                # same content would be refused again, so fail now with the reason.
                if str(result.get("status", "")).startswith("BLOCKED"):
                    email_msg.status = "FAILED"
                    email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
                    email_msg.failure_reason = result.get("error", "Blocked before sending")
                    email_msg.last_error_code = str(result.get("status"))[:50]
                    logger.warning(f"[Scheduler] Email blocked for {prospect.email}: {result.get('error')}")
                    return "failed"

                # Update with failure
                email_msg.retry_count = (email_msg.retry_count or 0) + 1

                if email_msg.retry_count >= (email_msg.max_retries or 3):
                    email_msg.status = "FAILED"
                    email_msg.failure_reason = result.get("error", "Unknown error")
                else:
                    # Schedule retry with exponential backoff
                    email_msg.status = "QUEUED"
                    email_msg.send_key = None
                    email_msg.next_retry_at = utcnow() + timedelta(
                        minutes=2 ** email_msg.retry_count
                    )
                    email_msg.last_error_code = result.get("error", "")[:50]
                if email_msg.status == "FAILED":
                    email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
                
                logger.warning(
                    f"[Scheduler] Email failed for {prospect.email}: {result.get('error')}"
                )
                return "failed"
                
        except AmbiguousEmailFailure as e:
            # SES may have accepted the message before the connection failed:
            # retrying could send it twice (BR-DF-06), so stop here and say why.
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
            email_msg.failure_reason = f"Send outcome unknown, not retried to avoid a duplicate: {e}"[:1000]
            logger.error(f"[Scheduler] Ambiguous send failure for {email_msg.message_id}: {e}")
            return "failed"

        except DeferredEmailFailure as e:
            # Platform or sender-identity problem: not this message's fault (item 14)
            return self._defer(email_msg, e, db)

        except TransientEmailFailure as e:
            # Temporary error (throttling, SES 5xx, no connection): send_with_retry
            # already retried quickly; come back later with backoff, and give up
            # with a clear reason once the message's retry budget is used.
            email_msg.retry_count = (email_msg.retry_count or 0) + 1
            email_msg.last_error_code = str(e)[:50]
            if email_msg.retry_count >= (email_msg.max_retries or 3):
                email_msg.status = "FAILED"
                email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
                email_msg.failure_reason = (f"Temporary sending error persisted after "
                                            f"{email_msg.retry_count} attempts: {e}")[:1000]
                logger.error(f"[Scheduler] Giving up on {email_msg.message_id} after transient errors: {e}")
                record_email_sent("failed")
            else:
                record_email_sent("retry")
                email_msg.status = "QUEUED"
                email_msg.send_key = None
                email_msg.next_retry_at = utcnow() + timedelta(
                    minutes=2 ** email_msg.retry_count
                )
                logger.warning(f"[Scheduler] Transient failure, will retry: {e}")
            return "failed"

        except PermanentEmailFailure as e:
            # Non-retryable error: fail fast, record why (B06)
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
            email_msg.failure_reason = str(e)[:1000]
            email_msg.last_error_code = (getattr(e, "code", None) or "PERMANENT")[:50]
            logger.error(f"[Scheduler] Permanent failure: {e}")
            record_email_sent("bounced" if getattr(e, "recipient_fault", False) else "failed")

            # --- Hard Bounce Auto-Suppression ---
            # A recipient-level rejection (invalid / blacklisted address) must
            # auto-purge the recipient to keep bounce rate < 2%. Account- or
            # configuration-level rejections are not the contact's fault.
            if settings.AUTO_SUPPRESS_HARD_BOUNCES and getattr(e, "recipient_fault", False):
                self._auto_suppress_hard_bounce(email_msg, prospect, db, str(e))

            return "failed"
            
        except Exception as e:
            logger.error(f"[Scheduler] Unexpected error: {e}")
            record_email_sent("error")
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
            email_msg.failure_reason = f"Unexpected: {str(e)[:200]}"
            return "failed"

    async def _record_sent(self, email_msg: EmailMessage, result: Dict[str, Any],
                           from_email_address: str, prospect: Prospect, db: Session) -> str:
        """SES accepted the message: record it (without overwriting a mid-send bounce) and move on."""
        record_email_sent("sent")
        send_breaker.platform_breaker.record_success()
        self.mark_sent(
            db, email_msg, sent_at=utcnow(), from_email=from_email_address,
            # The MIME Message-ID (not the SES ID) so IMAP sync can match prospect
            # replies via the In-Reply-To header.
            provider_message_id=result.get("internet_message_id") or result.get("ses_message_id"),
            # SES's id, so delivery events without our tag still match (BR-DF-04)
            ses_message_id=result.get("ses_message_id"),
        )
        if email_msg.inbox_id:
            delay = db.query(SendingInbox.delay_between_emails).filter(
                SendingInbox.inbox_id == email_msg.inbox_id).scalar()
            self._note_sent_for_pacing(email_msg.inbox_id, delay)
            # Update inbox warmup counters (in SQL, so parallel queues can't lose a count)
            db.query(SendingInbox).filter(SendingInbox.inbox_id == email_msg.inbox_id).update({
                SendingInbox.emails_sent_today: func.coalesce(SendingInbox.emails_sent_today, 0) + 1,
                SendingInbox.last_sent_at: utcnow(),
            }, synchronize_session=False)

        db.add(EmailEvent(
            message_id=email_msg.message_id,
            event_type=EmailEvent.EVENT_SENT,
            event_time=utcnow(),
            event_metadata={"ses_message_id": result.get("ses_message_id")},
        ))
        # Commit the send immediately, so no later re-queue guard in this batch can
        # overwrite it through session state.
        db.commit()

        if email_msg.campaign_id:
            MetricsService(db).process_event(email_msg.campaign_id, EmailEvent.EVENT_SENT)
        await self._update_prospect_step(email_msg, db)
        logger.info(f"[Scheduler] Email sent to {prospect.email}, SES ID: {result.get('ses_message_id')}")
        return "sent"

    @staticmethod
    def mark_sent(db: Session, email_msg: EmailMessage, *, sent_at: datetime, from_email: Optional[str],
                  provider_message_id: Optional[str], ses_message_id: Optional[str]) -> bool:
        """
        Record a send accepted by SES with a conditional UPDATE (item 15a): status becomes SENT
        only while the row is still SENDING. A bounce / complaint the SES webhook recorded while
        the send was in flight is kept; the send itself (sent_at, provider ids) is still
        recorded. Returns True when the row became SENT. Does not commit; refreshes email_msg.
        """
        values = {"sent_at": sent_at, "from_email": from_email,
                  "provider_message_id": provider_message_id, "ses_message_id": ses_message_id}
        rows = db.query(EmailMessage).filter(
            EmailMessage.message_id == email_msg.message_id, EmailMessage.status == "SENDING",
        ).update({**values, "status": "SENT"}, synchronize_session=False)
        if not rows:
            db.query(EmailMessage).filter(
                EmailMessage.message_id == email_msg.message_id, EmailMessage.sent_at.is_(None),
            ).update(values, synchronize_session=False)
            logger.warning(f"[Scheduler] Message {email_msg.message_id} changed state while it was being "
                           f"sent (provider event); keeping that status, recording the send only")
        db.refresh(email_msg)
        return bool(rows)

    def _defer(self, email_msg: EmailMessage, exc: DeferredEmailFailure, db: Session) -> str:
        """
        Platform / sender-identity failure (item 14): re-queue without spending the message's
        retry budget, feed the circuit breaker / mailbox auto-pause, and fail with the reason
        only after MAX_DEFERRED_ATTEMPTS (about 4 hours of attempts).
        """
        kind = exc.kind.value
        record_deferred_failure(db, kind, email_msg.inbox_id, str(exc))
        email_msg.deferred_count = (email_msg.deferred_count or 0) + 1
        email_msg.last_error_code = (exc.code or kind)[:50]
        if email_msg.deferred_count >= send_breaker.MAX_DEFERRED_ATTEMPTS:
            email_msg.status = "FAILED"
            email_msg.final_status, email_msg.final_status_at = "FAILED", utcnow()
            email_msg.failure_reason = (f"Sending kept failing for a {kind.lower()} reason after "
                                        f"{email_msg.deferred_count} attempts: {exc}")[:1000]
            record_email_sent("failed")
            logger.error(f"[Scheduler] Giving up on {email_msg.message_id} after {kind} failures: {exc}")
            return "failed"
        email_msg.status = "QUEUED"
        email_msg.send_key = None
        email_msg.next_retry_at = utcnow() + timedelta(minutes=send_breaker.DEFER_MINUTES)
        email_msg.failure_reason = f"Waiting to retry ({kind.lower()} problem): {exc}"[:1000]
        record_email_sent("retry")
        logger.warning(f"[Scheduler] {kind} failure for {email_msg.message_id}, re-queued: {exc}")
        return "deferred"

    @staticmethod
    def _hold_without_inbox(email_msg: EmailMessage) -> str:
        """No ACTIVE inbox on the campaign (item 12): keep the email queued and look again later."""
        email_msg.status = "QUEUED"
        email_msg.send_key = None
        email_msg.scheduled_at = utcnow() + timedelta(minutes=NO_INBOX_RETRY_MINUTES)
        email_msg.last_error_code = "NO_ACTIVE_INBOX"
        email_msg.failure_reason = ("Waiting: the campaign has no active sending inbox. "
                                    "Connect or resume one to send this email.")
        return "skipped"

    @staticmethod
    def stopped_by_enrollment(email_msg: EmailMessage, db: Session) -> Optional[str]:
        """
        The contact's enrollment status when it stops this step (BR-DF-05), under the step's own
        stop_on_reply / stop_on_bounce flags (sequence_stop.py); None when the step may go.
        """
        if not (email_msg.campaign_id and email_msg.sequence_id) or email_msg.direction == "INBOUND":
            return None
        enrollment = db.query(CampaignProspect.status).filter(
            CampaignProspect.campaign_id == email_msg.campaign_id,
            CampaignProspect.prospect_id == email_msg.prospect_id,
        ).first()
        status = enrollment[0] if enrollment else None
        if not status or status == "ACTIVE":
            return None
        step = db.query(EmailSequence).filter(EmailSequence.sequence_id == email_msg.sequence_id).first()
        return status if stops_sequence(step, status) else None

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
                    last_message_at=utcnow()
                )
                db.add(conv)
                db.flush()

        email_msg.conversation_id = conv.id
    
    async def _update_prospect_step(
        self, 
        email_msg: EmailMessage, 
        db: Session
    ) -> None:
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

            # Follow-up spacing counts from this send (B01), whatever the
            # enrollment's display status (e.g. OPENED) currently is.
            sent_step = db.query(EmailSequence).filter(
                EmailSequence.sequence_id == email_msg.sequence_id
            ).first() if email_msg.sequence_id else None
            if sent_step:
                following = db.query(EmailSequence).filter(
                    EmailSequence.campaign_id == email_msg.campaign_id,
                    EmailSequence.step_number == sent_step.step_number + 1,
                ).first()
                if following:
                    self.schedule_next_step(db, email_msg, following, email_msg.sent_at or utcnow())

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
                campaign_prospect.next_scheduled_at = self.schedule_next_step(
                    db, email_msg, next_step, email_msg.sent_at or utcnow()
                ) or utcnow() + timedelta(days=next_step.wait_days or 0)
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

    @staticmethod
    def schedule_next_step(db: Session, sent_msg: EmailMessage, next_step, sent_at: datetime) -> Optional[datetime]:
        """
        B01: the follow-up's gap counts from when this step was actually sent,
        not from launch. Every step is pre-scheduled at launch from launch time;
        if this step went out late (pacing, daily limits, send window, pause),
        its follow-up is pushed to sent_at + wait_days so the configured spacing
        always holds. It is never pulled earlier than its planned time.
        Returns the follow-up's scheduled time, or None if it has no message.
        """
        if not (sent_msg.campaign_id and next_step):
            return None
        earliest = sent_at + timedelta(days=next_step.wait_days or 0)
        follow_ups = db.query(EmailMessage).filter(
            EmailMessage.campaign_id == sent_msg.campaign_id,
            EmailMessage.prospect_id == sent_msg.prospect_id,
            EmailMessage.sequence_id == next_step.sequence_id,
            EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]),
        ).all()
        when = None
        for m in follow_ups:
            if not m.scheduled_at or m.scheduled_at < earliest:
                logger.info(f"[Scheduler] Follow-up {m.message_id} moved from {m.scheduled_at} to "
                            f"{earliest} ({next_step.wait_days} day(s) after the previous step was sent)")
                m.scheduled_at = earliest
            when = m.scheduled_at if when is None else min(when, m.scheduled_at)
        return when

    def _auto_suppress_hard_bounce(
        self,
        email_msg: EmailMessage,
        prospect: Prospect,
        db: Session,
        error_details: str,
    ) -> None:
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
    
    async def run_continuous(self, interval_seconds: int = 30) -> None:
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
            await asyncio.sleep(2 if stats.get("processed") else min(
                interval_seconds, max(2.0, stats.get("recheck_in") or interval_seconds)))
    
    def stop(self) -> None:
        """Stop continuous scheduler."""
        self.is_running = False
        logger.info("[Scheduler] Stopping")


def rebase_overdue_on_resume(db: Session, campaign) -> int:
    """
    B02: when a paused campaign resumes, everything that fell due while it was
    paused would otherwise be released at once. Instead each contact whose next
    step is overdue gets a fresh start time, spread from now at the sending
    inbox's pace (one contact per gap per inbox), and every later pending step
    of that contact moves by the same amount, so the gaps between steps are kept.
    Contacts whose next step is still in the future are left alone.
    Returns the number of messages moved. Does not commit.
    """
    now = utcnow()
    pending = db.query(EmailMessage).filter(
        EmailMessage.campaign_id == campaign.campaign_id,
        EmailMessage.status.in_(["PAUSED_BY_CAMPAIGN", "QUEUED", "SCHEDULED"]),
        EmailMessage.sent_at.is_(None),
    ).all()
    if not pending:
        return 0

    by_prospect = defaultdict(list)
    for m in pending:
        by_prospect[m.prospect_id].append(m)

    overdue = []
    for msgs in by_prospect.values():
        first = min(msgs, key=lambda m: m.scheduled_at or now)
        if (first.scheduled_at or now) <= now:
            overdue.append((first.scheduled_at or now, first, msgs))
    if not overdue:
        return 0
    overdue.sort(key=lambda t: (t[0], t[1].message_id))

    inbox_ids = {first.inbox_id for _, first, _ in overdue if first.inbox_id}
    delays = dict(db.query(SendingInbox.inbox_id, SendingInbox.delay_between_emails).filter(
        SendingInbox.inbox_id.in_(inbox_ids)).all()) if inbox_ids else {}
    campaign_gap = max(int(campaign.min_gap_minutes or 0), 0) * 60

    slot: Dict[Any, int] = defaultdict(int)
    moved = 0
    for planned, first, msgs in overdue:
        lane = first.inbox_id or campaign.campaign_id
        gap = max(int(delays.get(first.inbox_id) or 0), campaign_gap) or 60
        start = now + timedelta(seconds=slot[lane] * gap)
        slot[lane] += 1
        delta = start - planned
        for m in msgs:
            m.scheduled_at = (m.scheduled_at or planned) + delta
            m.next_retry_at = None
            moved += 1
    logger.info(f"[Scheduler] Resume of campaign {campaign.campaign_id}: {len(overdue)} overdue "
                f"contact(s) re-spread from now across {len(slot)} inbox lane(s), {moved} message(s) moved")
    return moved


# =============================
# Send-failure protection (item 14) and per-step stop rules (item 20)
# =============================

INBOX_PAUSED_KIND = "INBOX_PAUSED"


def pause_inbox_for_sender_rejections(db: Session, inbox_id: str, error: str) -> bool:
    """
    Set the mailbox PAUSED with the reason (its activity log), and tell the workspace admins.
    The alert centre shows it as an INBOX_PAUSED alert. Caller commits. Never raises.
    """
    try:
        inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).first()
        if not inbox or (inbox.status or "").upper() != "ACTIVE":
            return False
        reason = (f"Paused automatically: SES refused {send_breaker.SENDER_REJECTION_THRESHOLD} sends from "
                  f"{inbox.email_address} within {int(send_breaker.SENDER_REJECTION_WINDOW // 60)} minutes "
                  f"because the sending identity is not usable ({error[:300]}). Verify the address / "
                  f"MAIL FROM domain, then resume the mailbox.")
        inbox.status = "PAUSED"
        db.add(InboxWarmupEvent(
            tenant_id=inbox.tenant_id, inbox_id=inbox.inbox_id, event_type="ISSUE", status="FAILED",
            subject="Mailbox paused: sender identity rejected", detail=reason,
            event_metadata={"code": "SENDER_IDENTITY_REJECTED"}, event_time=utcnow(),
        ))
        admins = [uid for (uid,) in db.query(User.user_id).filter(
            User.tenant_id == inbox.tenant_id, User.role.in_(("SUPER_ADMIN", "ADMIN")),
            User.status == "ACTIVE").all()]
        notify(db, inbox.tenant_id, admins, INBOX_PAUSED_KIND, f"Mailbox paused: {inbox.email_address}",
               reason, "/app/email-accounts", dedupe_key=f"inbox-paused:{inbox.inbox_id}", dedupe_days=1)
        logger.warning(f"[SendBreaker] Inbox {inbox.email_address} paused: {error[:200]}")
        return True
    except Exception as exc:  # pausing is protective; a failure here must not break the send loop
        logger.error(f"[SendBreaker] Could not pause inbox {inbox_id}: {exc}")
        return False


def record_deferred_failure(db: Session, kind: str, inbox_id: Optional[str], error: str) -> None:
    """Feed a PLATFORM failure to the circuit breaker, a SENDER failure to the mailbox's rejection count."""
    if kind == "PLATFORM":
        if send_breaker.platform_breaker.record_failure():
            logger.error(f"[SendBreaker] {send_breaker.PLATFORM_FAILURE_THRESHOLD} consecutive platform "
                         f"failures: sending stopped for {int(send_breaker.PLATFORM_COOLDOWN_SECONDS)}s. "
                         f"Last error: {error[:300]}")
    elif kind == "SENDER" and inbox_id and send_breaker.sender_rejections.record(inbox_id):
        pause_inbox_for_sender_rejections(db, inbox_id, error)


def stop_pending_steps(db: Session, campaign_id: str, prospect_id: str, status: str,
                       failure_reason: Optional[str] = None) -> int:
    """
    Cancel this contact's queued step emails in this campaign that ``status`` (REPLIED, BOUNCED,
    UNSUBSCRIBED, PAUSED) stops under each step's own stop_on_reply / stop_on_bounce flags
    (sequence_stop.stops_sequence). Emails with no step (manual replies) are left alone.
    Returns how many were cancelled. Does not commit.
    """
    rows = db.query(EmailMessage.message_id, EmailSequence).join(
        EmailSequence, EmailSequence.sequence_id == EmailMessage.sequence_id
    ).filter(
        EmailMessage.campaign_id == campaign_id,
        EmailMessage.prospect_id == prospect_id,
        EmailMessage.direction == "OUTBOUND",
        EmailMessage.sent_at.is_(None),
        EmailMessage.status.in_(STOPPABLE_STATUSES),
    ).all()
    ids = [message_id for message_id, step in rows if stops_sequence(step, status)]
    if not ids:
        return 0
    return db.query(EmailMessage).filter(
        EmailMessage.message_id.in_(ids), EmailMessage.status.in_(STOPPABLE_STATUSES),
    ).update({EmailMessage.status: "CANCELLED",
              EmailMessage.failure_reason: failure_reason or stop_reason(status)},
             synchronize_session=False)


# Create singleton instance
email_scheduler = EmailSchedulerService()


# =============================
# Manual Trigger Functions
# =============================

async def trigger_email_processing() -> Any:
    """
    Manually trigger email processing.
    Can be called from an API endpoint or cron job.
    """
    return await email_scheduler.process_scheduled_emails()


def process_emails_sync() -> Any:
    """
    Synchronous wrapper for email processing.
    Useful for cron jobs or management commands.
    """
    return asyncio.run(email_scheduler.process_scheduled_emails())
