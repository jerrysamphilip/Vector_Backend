# app/services/email_scheduler_service.py
"""
Email Scheduler Service: the worker that sends due campaign mail through the sender (SES).

One pass (process_scheduled_emails):
  1. reap stuck SENDING claims (_recover_stuck_sends) and stop while the SES circuit breaker is open;
  2. take the due batch, fair across sending lanes (B04), one queue per inbox capped at about one cycle
     of its pace; the lanes run in parallel, each in its own session (_run_inbox_queue);
  3. each message goes through named steps (_prepare): campaign still active, previous step sent (B01),
     atomic claim with the send_key (BR-DF-06), contact / suppression / per-step stop flags, template,
     campaign + send window, sender inbox (no platform fallback, item 12), inbox daily cap and pace
     (never sleeps, F021), daily new-contact limit (BR-OV-01); then _deliver sends and records the outcome;
  4. the pass reports ``next_run_in`` (app/domain/sending.py): straight back while there is work, at the
     soonest paced mailbox otherwise, so the loop's wake-up follows the inboxes' pace.

SQL lives in app/repositories/sending.py (queue) and sending_context.py (lookups); the value objects and
pure rules in app/domain/sending.py. The worker commits at the sending protocol's commit points (claim,
sent, outcome) by design, so a lane never holds a transaction across an SES call.
"""

import asyncio
import logging
import time as _time
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timedelta, time
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.domain.campaigns_edit import IN_SEQUENCE_STATUSES, wait_until
from app.services.campaign_calendar import schedule_settings
from app.core.clock import utcnow
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.observability import record_email_sent
from app.domain import sending as rules
from app.domain.sending import (
    DUE_STATUSES, FAILED, PACED, SENT, SKIPPED, SUPPRESSED, ClaimResult, PacingState, PassStats, SendAttempt,
    SendKeyConflict, fail, lane_queues, next_gap, requeue, retries_exhausted, retry_delay, tally_outcome,
)
from app.models import EmailEvent, EmailMessage, Prospect
from app.models.campaign import Campaign
from app.repositories.sending import Session, SqlAlchemySendingQueueRepository
from app.repositories.sending_context import SqlAlchemySendingContextRepository
from app.schemas.campaign_schema import CampaignStatus
from app.services import daily_limit, send_breaker, send_safety, suppression
from app.services.daily_limit import DailyLimiter
from app.services.email_sender_service import (
    email_sender,
    DeferredEmailFailure,
    TransientEmailFailure,
    PermanentEmailFailure,
    AmbiguousEmailFailure,
)
from app.services.metrics_service import MetricsService
from app.services.notifications import notify
from app.services.inbox_rotation import inbox_weight
from app.services.send_guards import NO_INBOX_RETRY_MINUTES, active_inboxes
from app.services.sequence_stop import stop_pending_steps as stop_pending_steps  # re-exported (one entry point)
from app.services.sequence_stop import stop_reason, stops_sequence
from app.utils import business_calendar
from app.utils.campaign_prospect_status import set_prospect_status
from app.utils.conversation_lock import conversation_creation_lock

logger = logging.getLogger(__name__)

ACTIVE = CampaignStatus.ACTIVE.value
CAMPAIGN_PAUSED_RECHECK = timedelta(hours=1)
OUTSIDE_WINDOW_RECHECK = timedelta(minutes=30)
DEFAULT_WINDOW = (time(9, 0), time(17, 0))


def _queue(db: Session) -> SqlAlchemySendingQueueRepository:
    return SqlAlchemySendingQueueRepository(db)


def _context(db: Session) -> SqlAlchemySendingContextRepository:
    return SqlAlchemySendingContextRepository(db)


@dataclass
class _SendJob:
    """One message on its way through the send steps, with what the steps found so far."""

    msg: EmailMessage
    db: Session
    prospect: Any = None  # Prospect, once _step_contact found it
    template: Any = None
    campaign: Any = None  # Campaign, once _step_campaign_window loaded it
    sender_name: Optional[str] = None
    from_email: Optional[str] = None
    limit_user: Optional[str] = None
    window: Optional[Tuple[str, Any, Any]] = None  # (time zone, start, end) once _step_campaign_window ran

    @property
    def ctx(self) -> SqlAlchemySendingContextRepository:
        return _context(self.db)


class EmailSchedulerService:
    """Processes and sends scheduled emails (see the module docstring for the steps)."""

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
    PENDING_STATUSES = rules.PENDING_STEP_STATUSES
    # A pass held back only by pacing wakes the loop when the soonest mailbox is ready, if that is
    # within this many seconds (domain next_run_in); else the normal interval applies
    PACE_RECHECK_SECONDS = rules.IDLE_RECHECK_CEILING
    # Outcomes the SES webhook may record on a row that is still SENDING (delivery or bounce
    # arrived before the sender committed SENT): proof the provider accepted the message
    PROVIDER_OUTCOMES = rules.PROVIDER_OUTCOMES

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
        self._daily = DailyLimiter()

    # ── Sender inbox (item 12: no platform-address fallback) ────────────────

    def _resolve_sender_for_message(self, email_msg: EmailMessage, campaign: Any, db: Session) -> Optional[str]:
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
        by_id = {inbox.inbox_id: inbox for inbox in inboxes}
        by_email = {(inbox.email_address or "").strip().lower(): inbox for inbox in inboxes if inbox.email_address}
        inbox = self._own_inbox(email_msg, by_id, by_email)
        if inbox is None:
            inbox = self._sticky_inbox(email_msg, db, by_id, by_email)
        if inbox is None and inboxes:
            inbox = self._rotated_inbox(email_msg, inboxes)
        if inbox is None:
            logger.warning("[Scheduler] Campaign %s has no active sending inbox: message %s stays queued",
                           email_msg.campaign_id, email_msg.message_id)
            return None
        email_msg.inbox_id = inbox.inbox_id
        email_msg.from_email = inbox.email_address
        return inbox.email_address

    @staticmethod
    def _own_inbox(email_msg: EmailMessage, by_id: Dict[str, Any], by_email: Dict[str, Any]) -> Any:
        """The inbox the message is already bound to (by id, else by from address), if still active."""
        if email_msg.inbox_id and by_id.get(email_msg.inbox_id):
            return by_id[email_msg.inbox_id]
        if email_msg.from_email:
            return by_email.get(email_msg.from_email.strip().lower())
        return None

    @staticmethod
    def _sticky_inbox(email_msg: EmailMessage, db: Session, by_id: Dict[str, Any], by_email: Dict[str, Any]) -> Any:
        """The active inbox this contact was last mailed from in this campaign."""
        for previous in _context(db).previous_sends(email_msg.campaign_id, email_msg.prospect_id):
            sticky = by_id.get(previous.inbox_id) if previous.inbox_id else None
            if not sticky and previous.from_email:
                sticky = by_email.get(previous.from_email.strip().lower())
            if sticky:
                logger.info("[Scheduler] Sticky sender rebound: %s for prospect %s",
                            sticky.email_address, email_msg.prospect_id)
                return sticky
        return None

    @staticmethod
    def _rotated_inbox(email_msg: EmailMessage, inboxes: List[Any]) -> Any:
        """Deterministic rotation: the same contact always lands on the same inbox."""
        rotation_key = f"{email_msg.campaign_id}:{email_msg.prospect_id}"
        selected = inboxes[sum(ord(ch) for ch in rotation_key) % len(inboxes)]
        logger.info("[Scheduler] Bound sender inbox %s for message %s", selected.email_address, email_msg.message_id)
        return selected

    # ── One pass ────────────────────────────────────────────────────────────

    async def process_scheduled_emails(self, db: Optional[Session] = None) -> dict:
        """
        Process all scheduled emails that are due.

        Returns the pass's counters (processed, sent, failed, suppressed, skipped, paced, recheck_in) and
        ``next_run_in``: seconds until the loop should run again (None: its normal interval).
        """
        close_db = db is None
        session: Session = SessionLocal() if db is None else db
        stats = PassStats()
        try:
            self._recover_stuck_sends(session)
            # SES / the platform keeps failing: send nothing until the cool-down ends (item 14)
            if send_breaker.platform_breaker.is_open():
                logger.warning(f"[Scheduler] Sending stopped by the circuit breaker for another "
                               f"{send_breaker.platform_breaker.retry_in():.0f}s")
                return stats.as_dict()
            due = self._select_due(session, utcnow())
            if not due:
                logger.debug("[Scheduler] No scheduled emails to process")
                self._check_completed_campaigns(session)
                return stats.as_dict()
            await self._run_lanes(session, due, stats)
            session.expire_all()
            self._check_completed_campaigns(session)  # mark campaigns completed when their objective is met
            logger.info(f"[Scheduler] Batch complete: sent={stats.sent}, failed={stats.failed}, "
                        f"suppressed={stats.suppressed}")
            return stats.as_dict()
        except Exception as e:
            logger.error(f"[Scheduler] Error processing batch: {e}")
            session.rollback()
            raise
        finally:
            if close_db:
                session.close()

    async def _run_lanes(self, db: Session, due: List[rules.DueRow], stats: PassStats) -> None:
        """One queue per sending lane (inbox), each capped to about one cycle of its pace, sent in parallel."""
        delays = _context(db).inbox_delays({row.inbox_id for row in due if row.inbox_id})
        queues = lane_queues(due, delays, self.INBOX_CYCLE_SECONDS)
        logger.info(f"[Scheduler] Processing {sum(len(v) for v in queues.values())} due emails "
                    f"across {len(queues)} inbox queue(s)")
        gate = asyncio.Semaphore(self.max_parallel_lanes)

        async def _lane(ids: List[str]) -> dict:
            async with gate:
                return await self._run_inbox_queue(ids)

        for queue_stats in await asyncio.gather(*(_lane(ids) for ids in queues.values())):
            stats.add(queue_stats)
        self._note_pacing(stats)

    def _select_due(self, db: Session, now: datetime) -> List[rules.DueRow]:
        """Due rows as (message_id, inbox_id, lane), fair across lanes (B04); see SendingQueueRepository."""
        return _queue(db).select_due(now, self.LANE_CAP, self.batch_size)

    def earlier_step_unsent(self, email_msg: EmailMessage, db: Session) -> bool:
        """This contact's previous step of the campaign has not been sent yet (B01)."""
        return _queue(db).earlier_step_unsent(email_msg)

    async def _run_inbox_queue(self, message_ids: List[str]) -> dict:
        """
        Send one inbox's due messages in order, in its own session, without waiting (F021): once
        the inbox is not ready (its pacing gap since the last send has not passed) or the circuit
        breaker is open, the rest of the lane stays due for a later pass, still in order.
        """
        stats: Dict[str, int] = defaultdict(int)
        db = SessionLocal()
        try:
            queue = _queue(db)
            for message_id in message_ids:
                if send_breaker.platform_breaker.is_open():
                    break
                email_msg = queue.load_message(message_id)
                if email_msg is None or not self._still_due(email_msg):
                    continue
                if email_msg.inbox_id and self.pace_wait(db, email_msg.inbox_id) > 0:
                    stats[PACED] += 1
                    break
                outcome = await self._attempt(email_msg, db)
                if outcome == PACED:
                    stats[PACED] += 1
                    break
                stats["processed"] += 1
                tally_outcome(stats, outcome)
        finally:
            db.close()
        return stats

    @staticmethod
    def _still_due(email_msg: EmailMessage) -> bool:
        return email_msg.status in DUE_STATUSES and not email_msg.sent_at

    async def _attempt(self, email_msg: EmailMessage, db: Session) -> str:
        """Process one message and commit its outcome; one bad message must not stop the queue."""
        try:
            outcome = await self._process_single_email(email_msg, db)
            if email_msg.status in ("QUEUED", "SCHEDULED", "CANCELLED") and not email_msg.sent_at:
                email_msg.send_key = None  # not sent: free the step for a later attempt
            db.commit()
            return outcome
        except Exception as exc:
            logger.exception(f"[Scheduler] Message {email_msg.message_id} errored: {exc}")
            db.rollback()
            return FAILED

    # ── Pacing (BR-DF-01, F021) ─────────────────────────────────────────────

    def pace_wait(self, db: Session, inbox_id: str) -> float:
        """Seconds until this inbox may send again (0: ready now). Never sleeps (F021)."""
        pace = _context(db).inbox_pace(inbox_id)
        if not pace:
            return 0.0
        state = PacingState(delay=pace[0], last_sent_at=pace[1], ready_at_mono=self._ready_at.get(inbox_id))
        return state.wait(settings.SENDING_JITTER_MAX_SECONDS, now=utcnow(), now_mono=_time.monotonic())

    def _note_sent_for_pacing(self, inbox_id: Optional[str], delay: Optional[int]) -> None:
        """Draw this inbox's next gap now, once, so its readiness does not flicker between checks."""
        if inbox_id:
            self._ready_at[inbox_id] = _time.monotonic() + next_gap(
                delay, settings.SENDING_JITTER_MIN_SECONDS, settings.SENDING_JITTER_MAX_SECONDS)

    def _note_pacing(self, stats: PassStats) -> None:
        """recheck_in: seconds until the soonest paced inbox is ready (drives next_run_in)."""
        if stats.paced:
            stats.recheck_in = rules.soonest_ready(self._ready_at.values(), _time.monotonic())

    # ── Stuck-SENDING sweep (BR-DF-06, B08) ─────────────────────────────────

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
        queue = _queue(db)
        stuck = queue.stuck_sending(utcnow() - timedelta(minutes=self.STUCK_SENDING_MINUTES))
        if not stuck:
            return
        accepted, failed = self._settle_stuck(stuck, queue.provider_event_ids([m.message_id for m in stuck]))
        db.commit()
        logger.warning(f"[Scheduler] Interrupted sends: {accepted} recorded as sent "
                       f"(provider accepted), {failed} marked failed")

    @classmethod
    def _settle_stuck(cls, stuck: Iterable[EmailMessage], provider_events: set) -> Tuple[int, int]:
        """SENT where the provider accepted the message, else FAILED with the reason; (accepted, failed)."""
        accepted = failed = 0
        now = utcnow()
        for msg in stuck:
            if cls._provider_accepted(msg, provider_events):
                msg.status = "SENT"
                msg.sent_at = msg.claimed_at or now
                msg.failure_reason = rules.INTERRUPTED_ACCEPTED
                accepted += 1
            else:
                fail(msg, rules.INTERRUPTED_UNKNOWN, now)
                failed += 1
        return accepted, failed

    @classmethod
    def _provider_accepted(cls, msg: Any, provider_events: set) -> bool:
        """Evidence SES accepted a stuck SENDING message: a provider id, a delivery, an outcome or an event."""
        return rules.provider_accepted(msg, provider_events)

    @staticmethod
    def _date_undated_claims(db: Session) -> None:
        """Start the stuck-send clock for legacy SENDING rows that carry no claimed_at."""
        dated = _queue(db).date_undated_claims(utcnow())
        # Commit even when nothing matched: under REPEATABLE READ the UPDATE still holds next-key locks on
        # the scanned status='SENDING' index range, and this session stays open for the whole batch, so
        # every lane's claim (QUEUED -> SENDING) would wait on it until the read timeout.
        db.commit()
        if dated:
            logger.info(f"[Scheduler] {dated} SENDING message(s) had no claim time; dated now")

    # ── Claim (BR-DF-06) ────────────────────────────────────────────────────

    @staticmethod
    def send_key_for(email_msg: EmailMessage) -> Optional[str]:
        """Identity of 'this campaign step to this address' (BR-DF-06); None for ad-hoc mail."""
        return rules.send_key(email_msg.campaign_id, email_msg.sequence_id, email_msg.to_email)

    def _claim(self, email_msg: EmailMessage, db: Session) -> bool:
        """
        Atomically take ownership of a message for sending. Fails if another
        worker has it, or if this campaign step was already sent (or is being
        sent) to the same address, in which case the message is cancelled.
        """
        return self._claim_message(email_msg, db).claimed

    def _claim_message(self, email_msg: EmailMessage, db: Session) -> ClaimResult:
        queue = _queue(db)
        key = self.send_key_for(email_msg)
        original = queue.duplicate_of(email_msg) if key else None
        if original:
            self._cancel_duplicate(email_msg, db, original)
            return ClaimResult(claimed=False, duplicate=True, duplicate_of=original)
        try:
            rows = queue.claim(email_msg.message_id, key, utcnow())
            db.commit()
        except SendKeyConflict:
            # The unique send_key: a concurrent worker claimed the same step for this address
            db.rollback()
            self._cancel_duplicate(email_msg, db, None)
            return ClaimResult(claimed=False, duplicate=True)
        if rows:
            db.refresh(email_msg)
        return ClaimResult(claimed=bool(rows))

    def _cancel_duplicate(self, email_msg: EmailMessage, db: Session, original_id: Optional[str]) -> None:
        _queue(db).cancel_duplicate(email_msg.message_id, rules.duplicate_reason(original_id))
        db.commit()
        logger.warning(f"[Scheduler] Cancelled duplicate send {email_msg.message_id} to {email_msg.to_email}")

    # ── Campaign completion ─────────────────────────────────────────────────

    def _check_completed_campaigns(self, db: Session) -> None:
        """
        Automatically triggers when every lead in the campaign has reached a terminal state.
        Signals to the user that the objective is met and no further action is required.
        Terminal states: COMPLETED, REPLIED, BOUNCED, UNSUBSCRIBED.
        """
        try:
            ctx = _context(db)
            for campaign in ctx.active_campaigns():
                total, still_open = ctx.enrollment_counts(campaign.campaign_id)
                if total and not still_open:
                    logger.info(f"[Scheduler] Campaign {campaign.campaign_id} objective met. All {total} "
                                f"prospects hit terminal states. Marking COMPLETED.")
                    campaign.status = CampaignStatus.COMPLETED.value
            db.commit()
        except Exception as e:
            logger.error(f"[Scheduler] Error checking completed campaigns: {e}")
            db.rollback()

    # ── One message ─────────────────────────────────────────────────────────

    async def _process_single_email(self, email_msg: EmailMessage, db: Session) -> str:
        """
        Process a single email message: the send steps (_prepare), then the send (_deliver).

        Returns the outcome: 'sent', 'failed', 'suppressed', 'skipped', 'paced' or 'deferred'.
        """
        job = _SendJob(msg=email_msg, db=db)
        try:
            outcome = self._prepare(job)
            return outcome if outcome else await self._deliver(job)
        except AmbiguousEmailFailure as e:
            return self._on_ambiguous(email_msg, e)
        except DeferredEmailFailure as e:
            # Platform or sender-identity problem: not this message's fault (item 14)
            return self._defer(email_msg, e, db)
        except TransientEmailFailure as e:
            return self._on_transient(email_msg, e)
        except PermanentEmailFailure as e:
            return self._on_permanent(job, e)
        except Exception as e:
            return self._on_unexpected(email_msg, e)

    def _prepare(self, job: _SendJob) -> Optional[str]:
        """Run the send steps in order; the first that decides the message's outcome ends the pass."""
        steps = (self._step_campaign_active, self._step_previous_sent, self._step_claim, self._step_contact,
                 self._step_template, self._step_campaign_window, self._step_sender, self._step_inbox_capacity,
                 self._step_daily_limit)
        for step in steps:
            outcome = step(job)
            if outcome:
                return outcome
        # Ensure Unified Inbox threading context exists once inbox is known.
        self._ensure_conversation(job.msg, job.prospect, job.db)
        # Restart the stuck-send clock now: the checks above happen after the claim,
        # and the SENDING sweeper must only see time actually spent in SES.
        job.msg.claimed_at = utcnow()
        job.db.commit()
        return None

    def _step_campaign_active(self, job: _SendJob) -> Optional[str]:
        """Pre-flight, BEFORE claiming: a campaign paused between the batch query and the claim waits an hour."""
        campaign = job.ctx.fresh_campaign(job.msg.campaign_id)
        if campaign and campaign.status != ACTIVE:
            logger.info(f"[Scheduler] Campaign {job.msg.campaign_id} is {campaign.status} — skipping message "
                        f"{job.msg.message_id}, re-queuing for 1 hour.")
            job.msg.status = "QUEUED"
            job.msg.scheduled_at = utcnow() + CAMPAIGN_PAUSED_RECHECK
            job.db.commit()
            return SKIPPED
        return None

    def _step_previous_sent(self, job: _SendJob) -> Optional[str]:
        """A follow-up never goes out before the step ahead of it (B01); the due query holds these back,
        this covers a race with a re-queue."""
        if self.earlier_step_unsent(job.msg, job.db):
            logger.info(f"[Scheduler] Holding {job.msg.message_id}: the previous step "
                        f"for this contact has not been sent yet")
            return SKIPPED
        return None

    def _step_claim(self, job: _SendJob) -> Optional[str]:
        """Atomic ownership claim, with the duplicate-send guard (BR-DF-06)."""
        return None if self._claim(job.msg, job.db) else SKIPPED

    def _step_contact(self, job: _SendJob) -> Optional[str]:
        """The contact exists, is not suppressed (BR-DF-08) and has not stopped this step (BR-DF-05)."""
        msg = job.msg
        job.prospect = job.ctx.prospect(msg.prospect_id)
        if not job.prospect:
            logger.warning(f"[Scheduler] Prospect not found for message {msg.message_id}")
            fail(msg, "Prospect not found", utcnow(), final=False)
            return FAILED
        reason = suppression.blocked_reason(job.db, job.prospect)
        if reason:
            logger.info(f"[Scheduler] {job.prospect.email} is suppressed ({reason}), skipping")
            msg.status, msg.failure_reason = "CANCELLED", reason
            return SUPPRESSED
        # Later steps are queued at launch, so check the contact's state before each send,
        # under this step's own stop_on_reply / stop_on_bounce flags (item 20).
        stopped = self.stopped_by_enrollment(msg, job.db)
        if stopped:
            msg.status, msg.failure_reason = "CANCELLED", stop_reason(stopped)
            return SUPPRESSED
        return None

    def _step_template(self, job: _SendJob) -> Optional[str]:
        """The template; manual unified-inbox replies have none and send the message's own snapshot."""
        msg = job.msg
        if not msg.template_id:
            job.template = SimpleNamespace(subject=msg.subject or "Reply", body=msg.body_text or "")
            return None
        job.template = job.ctx.template(msg.template_id)
        if not job.template:
            logger.warning(f"[Scheduler] Template not found for message {msg.message_id}")
            fail(msg, "Template not found", utcnow(), final=False)
            return FAILED
        return None

    def _step_campaign_window(self, job: _SendJob) -> Optional[str]:
        """The campaign is still ACTIVE (paused mid-batch: back in an hour) and it is inside the send window."""
        row = job.ctx.campaign_with_creator(job.msg.campaign_id)
        if not row:
            raise RuntimeError("campaign or its creator not found")
        campaign, user = row
        job.campaign = campaign
        if campaign.status != ACTIVE:
            logger.info(f"[Scheduler] Campaign {campaign.campaign_id} is {campaign.status}, "
                        f"re-queuing message {job.msg.message_id} for 1 hour.")
            requeue(job.msg, at=utcnow() + CAMPAIGN_PAUSED_RECHECK)
            return SKIPPED
        job.sender_name = campaign.sender_name or f"{user.first_name} {user.last_name}"
        timezone = self._contact_timezone(campaign, job.prospect)
        start, end = campaign.send_window_start or DEFAULT_WINDOW[0], campaign.send_window_end or DEFAULT_WINDOW[1]
        job.window = (timezone, start, end)
        now = utcnow()
        if not self._window_open(job.window, now):
            # S-17: parked at the next window start (business days, the contact's / campaign's time zone)
            at = self._next_window_open(job.window, now) or now + OUTSIDE_WINDOW_RECHECK
            logger.info(f"[Scheduler] Msg {job.msg.message_id} is outside send window for {timezone}. "
                        f"Parked until {at.isoformat()}.")
            requeue(job.msg, at=at)
            return SKIPPED
        return None

    @staticmethod
    def _window_open(window: Tuple[str, Any, Any], at: datetime) -> bool:
        timezone, start, end = window
        return business_calendar.is_within_send_window(current_utc=at, timezone_str=timezone,
                                                       send_window_start=start, send_window_end=end)

    @classmethod
    def _next_window_open(cls, window: Tuple[str, Any, Any], after: datetime) -> Optional[datetime]:
        """next_send_window_start: the first window start after ``after`` that is open (a business day);
        None when none is found in the look-ahead."""
        timezone, start, _ = window
        starts = rules.window_starts_after(after, timezone, rules.window_time(start, DEFAULT_WINDOW[0]))
        return rules.first_open_moment(starts, lambda at: cls._window_open(window, at))

    @staticmethod
    def _contact_timezone(campaign: Campaign, prospect: Any) -> str:
        """The contact's time zone (from their state) when the campaign respects it, else the campaign's."""
        prospect_tz = None
        if campaign.respect_timezone and prospect.poc_state:
            prospect_tz = business_calendar.get_timezone_for_state(prospect.poc_state, campaign.campaign_timezone)
        return prospect_tz or campaign.campaign_timezone or "UTC"

    def _step_sender(self, job: _SendJob) -> Optional[str]:
        """Bind the sender inbox; with no ACTIVE inbox the email stays queued (item 12)."""
        job.from_email = self._resolve_sender_for_message(job.msg, job.campaign, job.db)
        return None if job.from_email else self._hold_without_inbox(job.msg)

    def _step_inbox_capacity(self, job: _SendJob) -> Optional[str]:
        """Per-inbox daily cap (warm-up schedule + override) and pace (BR-DF-01, F021)."""
        msg = job.msg
        inbox = job.ctx.inbox(msg.inbox_id) if msg.inbox_id else None
        if not inbox:
            return None
        self._roll_inbox_day(inbox)
        limit = email_sender.get_inbox_daily_limit(inbox)
        if (inbox.emails_sent_today or 0) >= limit:
            roomier = self._roomier_inbox(job, inbox)
            if roomier is None:
                at = self._capped_until(job, inbox)
                logger.info(f"[Scheduler] Inbox {inbox.email_address} hit daily limit ({limit}), "
                            f"message {msg.message_id} parked until {at.isoformat()}")
                requeue(msg, at=at)
                return SKIPPED
            inbox = roomier
        # Not ready yet: give the claim back and leave the email due for a later pass; never
        # sleep here, so no inbox holds up another (send_pacing.py).
        if self.pace_wait(job.db, inbox.inbox_id) > 0:
            requeue(msg)
            return PACED
        return None

    def _capped_until(self, job: _SendJob, inbox: Any) -> datetime:
        """After the inbox's cooling period, at the first moment the send window is open then (S-17)."""
        earliest = utcnow() + timedelta(hours=inbox.cooling_period_hours or 24)
        if job.window is None or self._window_open(job.window, earliest):
            return earliest
        return self._next_window_open(job.window, earliest) or earliest

    def _roomier_inbox(self, job: _SendJob, full: Any) -> Any:
        """S-17: a contact this campaign has never emailed moves to the campaign mailbox with the most room
        left today (warm-up caps included; ties by rotation weight), with the rest of its sequence. None:
        stay (the contact has a sticky sender, or no other mailbox has room)."""
        msg = job.msg
        if job.campaign is None or job.ctx.previous_sends(msg.campaign_id, msg.prospect_id):
            return None
        options = []
        for candidate in active_inboxes(job.campaign):
            if candidate.inbox_id == full.inbox_id:
                continue
            self._roll_inbox_day(candidate)
            remaining = email_sender.get_inbox_daily_limit(candidate) - (candidate.emails_sent_today or 0)
            options.append(rules.InboxRoom(candidate, remaining, inbox_weight(candidate)))
        chosen = rules.roomiest_inbox(options)
        if chosen is None:
            return None
        msg.inbox_id, msg.from_email = chosen.inbox_id, chosen.email_address
        job.from_email = chosen.email_address
        moved = _queue(job.db).rebind_unsent(msg.campaign_id, msg.prospect_id, msg.message_id,
                                             chosen.inbox_id, chosen.email_address)
        logger.info(f"[Scheduler] Inbox {full.email_address} is at its daily cap: new contact {msg.prospect_id} "
                    f"moved to {chosen.email_address} (with {moved} later steps)")
        return chosen

    @staticmethod
    def _roll_inbox_day(inbox: Any) -> None:
        """New day: reset the inbox's counter, advance its warm-up day, and warn once when its pace cannot
        reach its daily cap (a delay_between_emails / limit mismatch would otherwise under-deliver silently)."""
        today = utcnow().date()
        if inbox.last_daily_reset and inbox.last_daily_reset.date() >= today:
            return
        inbox.emails_sent_today = 0
        inbox.last_daily_reset = utcnow()
        if inbox.warmup_enabled and inbox.warmup_start_date:
            inbox.warmup_day = (utcnow() - inbox.warmup_start_date).days
        configured_limit = email_sender.get_inbox_daily_limit(inbox)
        reachable = rules.pace_shortfall(inbox.delay_between_emails, configured_limit)
        if reachable is not None:
            logger.warning(
                f"[Scheduler] Inbox {inbox.email_address}: delay_between_emails={inbox.delay_between_emails}s "
                f"allows at most ~{reachable}/day, below its configured limit of {configured_limit}/day. "
                f"Lower delay_between_emails (or the campaign send window) to actually reach this cap.")

    def _step_daily_limit(self, job: _SendJob) -> Optional[str]:
        """Daily new-contact limit (BR-OV-01): first steps only; follow-ups always go."""
        msg = job.msg
        if msg.direction == "INBOUND" or not daily_limit.is_first_step(job.db, msg):
            return None
        job.limit_user = job.prospect.owner_id or job.campaign.created_by
        if not job.limit_user or self._daily.reserve(job.db, job.limit_user):
            return None
        held_user, job.limit_user = job.limit_user, None
        requeue(msg, at=daily_limit.next_day_start())
        msg.last_error_code = daily_limit.HELD_CODE
        msg.failure_reason = (f"Held for tomorrow: the daily limit of "
                              f"{settings.DAILY_NEW_CONTACT_LIMIT} new contacts was reached")
        logger.info(f"[Scheduler] Daily new-contact limit reached for user {held_user}; "
                    f"message {msg.message_id} moved to tomorrow")
        return SKIPPED

    def _release_daily(self, job: _SendJob) -> None:
        if job.limit_user:
            self._daily.release(job.limit_user)  # nothing went out

    async def _deliver(self, job: _SendJob) -> str:
        """
        Send via SES. Permanent errors (rejected / invalid address) raise PermanentEmailFailure and are
        never retried; temporary ones are retried inside send_with_retry and raise TransientEmailFailure
        when those attempts run out (B06).
        """
        msg = job.msg
        try:
            result = await email_sender.send_with_retry(
                email_message=msg, email_template=job.template, prospect=job.prospect,
                max_retries=msg.max_retries, sender_name=job.sender_name, from_email_address=job.from_email,
            )
        except (TransientEmailFailure, PermanentEmailFailure):
            self._release_daily(job)
            raise
        if not result["success"]:
            self._release_daily(job)
            return self._on_refused(msg, result, job.prospect)
        if msg.last_error_code == daily_limit.HELD_CODE:
            msg.last_error_code, msg.failure_reason = None, None
        return await self._record_sent(msg, result, job.from_email, job.prospect, job.db)

    # ── Outcomes ────────────────────────────────────────────────────────────

    @staticmethod
    def _on_refused(email_msg: EmailMessage, result: Dict[str, Any], prospect: Any) -> str:
        """The sender returned failure without raising."""
        now = utcnow()
        if SendAttempt.from_result(result).blocked:
            # Refused before reaching SES (personal address, spam lint): the same content would be
            # refused again, so fail now with the reason.
            fail(email_msg, result.get("error", "Blocked before sending"), now)
            email_msg.last_error_code = str(result.get("status"))[:50]
            logger.warning(f"[Scheduler] Email blocked for {prospect.email}: {result.get('error')}")
            return FAILED
        email_msg.retry_count = (email_msg.retry_count or 0) + 1
        if retries_exhausted(email_msg.retry_count, email_msg.max_retries):
            fail(email_msg, result.get("error", "Unknown error"), now)
        else:
            requeue(email_msg, retry_at=now + retry_delay(email_msg.retry_count, settings.SEND_RETRY_BASE_SECONDS))  # exponential backoff
            email_msg.last_error_code = result.get("error", "")[:50]
        logger.warning(f"[Scheduler] Email failed for {prospect.email}: {result.get('error')}")
        return FAILED

    @staticmethod
    def _on_ambiguous(email_msg: EmailMessage, e: Exception) -> str:
        """SES may have accepted the message before the connection failed: retrying could send it twice
        (BR-DF-06), so stop here and say why."""
        fail(email_msg, f"Send outcome unknown, not retried to avoid a duplicate: {e}"[:1000], utcnow())
        logger.error(f"[Scheduler] Ambiguous send failure for {email_msg.message_id}: {e}")
        return FAILED

    @staticmethod
    def _on_transient(email_msg: EmailMessage, e: Exception) -> str:
        """Temporary error (throttling, SES 5xx, no connection): send_with_retry already retried quickly;
        come back later with backoff, and give up with a clear reason once the retry budget is used."""
        email_msg.retry_count = (email_msg.retry_count or 0) + 1
        email_msg.last_error_code = str(e)[:50]
        if retries_exhausted(email_msg.retry_count, email_msg.max_retries):
            fail(email_msg, (f"Temporary sending error persisted after "
                             f"{email_msg.retry_count} attempts: {e}")[:1000], utcnow())
            logger.error(f"[Scheduler] Giving up on {email_msg.message_id} after transient errors: {e}")
            record_email_sent("failed")
        else:
            record_email_sent("retry")
            requeue(email_msg, retry_at=utcnow() + retry_delay(email_msg.retry_count, settings.SEND_RETRY_BASE_SECONDS))
            logger.warning(f"[Scheduler] Transient failure, will retry: {e}")
        return FAILED

    def _on_permanent(self, job: _SendJob, e: PermanentEmailFailure) -> str:
        """Non-retryable error: fail fast, record why (B06); a recipient-level rejection (invalid /
        blacklisted address) auto-suppresses the address to keep the bounce rate < 2%."""
        email_msg = job.msg
        fail(email_msg, str(e)[:1000], utcnow())
        email_msg.last_error_code = (getattr(e, "code", None) or "PERMANENT")[:50]
        logger.error(f"[Scheduler] Permanent failure: {e}")
        recipient_fault = getattr(e, "recipient_fault", False)
        record_email_sent("bounced" if recipient_fault else "failed")
        if settings.AUTO_SUPPRESS_HARD_BOUNCES and recipient_fault:
            self._auto_suppress_hard_bounce(email_msg, job.prospect, job.db, str(e))
        return FAILED

    @staticmethod
    def _on_unexpected(email_msg: EmailMessage, e: Exception) -> str:
        logger.error(f"[Scheduler] Unexpected error: {e}")
        record_email_sent("error")
        fail(email_msg, f"Unexpected: {str(e)[:200]}", utcnow())
        return FAILED

    async def _record_sent(self, email_msg: EmailMessage, result: Dict[str, Any],
                           from_email_address: Optional[str], prospect: Prospect, db: Session) -> str:
        """SES accepted the message: record it (without overwriting a mid-send bounce) and move on."""
        record_email_sent("sent")
        send_breaker.platform_breaker.record_success()
        attempt = SendAttempt.from_result(result)
        # The MIME Message-ID (not the SES ID) as provider id, so IMAP sync can match prospect replies via
        # In-Reply-To; SES's own id too, so delivery events without our tag still match (BR-DF-04)
        self.mark_sent(db, email_msg, sent_at=utcnow(), from_email=from_email_address,
                       provider_message_id=attempt.provider_message_id, ses_message_id=attempt.ses_message_id)
        queue = _queue(db)
        if email_msg.inbox_id:
            self._note_sent_for_pacing(email_msg.inbox_id, _context(db).inbox_delay(email_msg.inbox_id))
            queue.count_sent_to_inbox(email_msg.inbox_id, utcnow())
        queue.record_sent_event(email_msg.message_id, utcnow(), attempt.ses_message_id)
        # Commit the send immediately, so no later re-queue guard in this batch can
        # overwrite it through session state.
        db.commit()
        if email_msg.campaign_id:
            MetricsService(db).process_event(email_msg.campaign_id, EmailEvent.EVENT_SENT)
        await self._update_prospect_step(email_msg, db)
        logger.info(f"[Scheduler] Email sent to {prospect.email}, SES ID: {attempt.ses_message_id}")
        return SENT

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
        became_sent = _queue(db).mark_sent(email_msg, values)
        if not became_sent:
            logger.warning(f"[Scheduler] Message {email_msg.message_id} changed state while it was being "
                           f"sent (provider event); keeping that status, recording the send only")
        return became_sent

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
            fail(email_msg, (f"Sending kept failing for a {kind.lower()} reason after "
                             f"{email_msg.deferred_count} attempts: {exc}")[:1000], utcnow())
            record_email_sent("failed")
            logger.error(f"[Scheduler] Giving up on {email_msg.message_id} after {kind} failures: {exc}")
            return FAILED
        requeue(email_msg, retry_at=utcnow() + timedelta(minutes=send_breaker.DEFER_MINUTES))
        email_msg.failure_reason = f"Waiting to retry ({kind.lower()} problem): {exc}"[:1000]
        record_email_sent("retry")
        logger.warning(f"[Scheduler] {kind} failure for {email_msg.message_id}, re-queued: {exc}")
        return rules.DEFERRED

    @staticmethod
    def _hold_without_inbox(email_msg: EmailMessage) -> str:
        """No ACTIVE inbox on the campaign (item 12): keep the email queued and look again later."""
        requeue(email_msg, at=utcnow() + timedelta(minutes=NO_INBOX_RETRY_MINUTES))
        email_msg.last_error_code = "NO_ACTIVE_INBOX"
        email_msg.failure_reason = ("Waiting: the campaign has no active sending inbox. "
                                    "Connect or resume one to send this email.")
        return SKIPPED

    @staticmethod
    def stopped_by_enrollment(email_msg: EmailMessage, db: Session) -> Optional[str]:
        """
        The contact's enrollment status when it stops this step (BR-DF-05), under the step's own
        stop_on_reply / stop_on_bounce flags (sequence_stop.py); None when the step may go.
        """
        if not (email_msg.campaign_id and email_msg.sequence_id) or email_msg.direction == "INBOUND":
            return None
        ctx = _context(db)
        status = ctx.enrollment_status(email_msg.campaign_id, email_msg.prospect_id)
        if not status or status == "ACTIVE":
            return None
        return status if stops_sequence(ctx.step(email_msg.sequence_id), status) else None

    def _ensure_conversation(self, email_msg: EmailMessage, prospect: Any, db: Session) -> None:
        """
        Link the message to the contact's Unified Inbox conversation for its inbox once the inbox is known
        (creating it the first time), and bring an existing conversation to the top (last_message_at).
        """
        if email_msg.conversation_id or not email_msg.inbox_id:
            return
        ctx = _context(db)
        # Serialized against imap_sync_service's own find-or-create for the same
        # (prospect_id, inbox_id) key — see conversation_lock.py for why.
        with conversation_creation_lock:
            conv = ctx.conversation(email_msg.prospect_id, email_msg.inbox_id)
            if conv:
                conv.last_message_at = utcnow()
            else:
                conv = ctx.add_conversation(
                    tenant_id=prospect.tenant_id, prospect_id=email_msg.prospect_id, inbox_id=email_msg.inbox_id,
                    subject=email_msg.subject, status="OPEN", is_unread=False, last_message_at=utcnow(),
                )
        email_msg.conversation_id = conv.id

    # ── After a send: follow-up timing and the contact's step (B01) ─────────

    async def _update_prospect_step(self, email_msg: EmailMessage, db: Session) -> None:
        """
        Update campaign prospect state after a successful send.
        - If there is a next sequence step, advance current_step and schedule next send.
        - If there is no next step, mark prospect as COMPLETED.
        Follow-up spacing counts from this send (B01), whatever the enrollment's display status.
        """
        try:
            ctx = _context(db)
            enrollment = ctx.enrollment(email_msg.campaign_id, email_msg.prospect_id)
            sent_step = ctx.step(email_msg.sequence_id)
            if sent_step:
                following = ctx.step_number(email_msg.campaign_id, sent_step.step_number + 1)
                if following:
                    self.schedule_next_step(db, email_msg, following, email_msg.sent_at or utcnow())
            # Something else (bounce, reply, unsubscribe) may have changed the status since the send was
            # queued: don't advance / complete over it. An enrolment that opened an email (OPENED) is still in
            # the sequence and advances like an ACTIVE one (UAT 3-5: it used to stay on its step forever and
            # keep the campaign from completing).
            if enrollment and enrollment.status in IN_SEQUENCE_STATUSES:
                self._advance_enrollment(db, email_msg, enrollment, sent_step)
        except Exception as e:
            logger.exception(f"[Scheduler] Failed to update prospect step: {e}")

    def _advance_enrollment(self, db: Session, email_msg: EmailMessage, enrollment: Any, sent_step: Any) -> None:
        """Move the contact to the next step (scheduled from this send) or complete the sequence."""
        number = sent_step.step_number + 1 if sent_step else enrollment.current_step + 1
        next_step = _context(db).step_number(email_msg.campaign_id, number)
        if not next_step:
            set_prospect_status(enrollment, "COMPLETED")
            enrollment.next_scheduled_at = None
            logger.info(f"[Scheduler] Prospect {email_msg.prospect_id} completed all sequence steps "
                        f"for campaign {email_msg.campaign_id}")
            return
        enrollment.current_step = number
        enrollment.next_scheduled_at = self.schedule_next_step(
            db, email_msg, next_step, email_msg.sent_at or utcnow()
        ) or utcnow() + timedelta(days=next_step.wait_days or 0)
        logger.info(f"[Scheduler] Prospect {email_msg.prospect_id} advanced to step {number}, "
                    f"next send in {next_step.wait_days} day(s)")

    @staticmethod
    def schedule_next_step(db: Session, sent_msg: EmailMessage, next_step: Any, sent_at: datetime) -> Optional[datetime]:
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
        # Calendar or business days, as the campaign is set (wait_business_days, migration 0027)
        campaign = sent_msg.campaign
        earliest = (wait_until(sent_at, next_step.wait_days or 0, schedule_settings(campaign)) if campaign
                    else sent_at + timedelta(days=next_step.wait_days or 0))
        when = None
        for m in _queue(db).unsent_follow_ups(sent_msg.campaign_id, sent_msg.prospect_id, next_step.sequence_id):
            if not m.scheduled_at or m.scheduled_at < earliest:
                logger.info(f"[Scheduler] Follow-up {m.message_id} moved from {m.scheduled_at} to "
                            f"{earliest} ({next_step.wait_days} day(s) after the previous step was sent)")
                m.scheduled_at = earliest
            when = m.scheduled_at if when is None else min(when, m.scheduled_at)
        return when

    def _auto_suppress_hard_bounce(self, email_msg: EmailMessage, prospect: Any, db: Session,
                                   error_details: str) -> None:
        """
        Auto-suppress a recipient after a permanent/hard bounce, across every campaign in the workspace
        (BR-DF-08): GlobalUnsubscribe with reason HARD_BOUNCE_AUTO_SUPPRESSED, remaining queued mail
        cancelled, then the campaign's health is checked. Keeps the sending domain's bounce rate under the
        2% threshold required by major mailbox providers (Microsoft, Google).
        """
        try:
            suppression.suppress(db, prospect.tenant_id, prospect.email,
                                 f"HARD_BOUNCE_AUTO_SUPPRESSED: {error_details[:200]}", kind="HARD_BOUNCE")
            db.flush()
            send_safety.check_campaign_health(db, email_msg.campaign_id, trigger="hard bounce at send time")
        except Exception as e:
            logger.error(f"[Scheduler] Failed to auto-suppress hard bounce: {e}")

    # ── Loop ────────────────────────────────────────────────────────────────

    async def run_continuous(self, interval_seconds: int = 30) -> None:
        """Run the scheduler continuously; each pass says when to come back (next_run_in)."""
        if self.is_running:
            logger.warning("[Scheduler] Already running")
            return
        self.is_running = True
        logger.info(f"[Scheduler] Starting continuous mode, interval={interval_seconds}s")
        while self.is_running:
            stats: dict = {}
            try:
                stats = await self.process_scheduled_emails() or {}
            except Exception as e:
                logger.error(f"[Scheduler] Error in continuous run: {e}")
            await asyncio.sleep(min(interval_seconds, stats.get("next_run_in") or interval_seconds))

    def stop(self) -> None:
        """Stop continuous scheduler."""
        self.is_running = False
        logger.info("[Scheduler] Stopping")


def rebase_overdue_on_resume(db: Session, campaign: Any) -> int:
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
    overdue = _overdue_contacts(_queue(db).pending_for_resume(campaign.campaign_id), now)
    if not overdue:
        return 0
    delays = _context(db).inbox_delays({first.inbox_id for _, first, _ in overdue if first.inbox_id})
    campaign_gap = max(int(campaign.min_gap_minutes or 0), 0) * 60
    slot: Dict[Any, int] = defaultdict(int)
    moved = 0
    for planned, first, msgs in overdue:
        lane = first.inbox_id or campaign.campaign_id
        gap = max(int(delays.get(first.inbox_id) or 0), campaign_gap) or 60
        delta = now + timedelta(seconds=slot[lane] * gap) - planned
        slot[lane] += 1
        for m in msgs:
            m.scheduled_at = (m.scheduled_at or planned) + delta
            m.next_retry_at = None
            moved += 1
    logger.info(f"[Scheduler] Resume of campaign {campaign.campaign_id}: {len(overdue)} overdue "
                f"contact(s) re-spread from now across {len(slot)} inbox lane(s), {moved} message(s) moved")
    return moved


def _overdue_contacts(pending: List[EmailMessage], now: datetime) -> List[Tuple[datetime, EmailMessage, List[EmailMessage]]]:
    """(planned time, first message, all messages) per contact whose next step is overdue, oldest first."""
    by_prospect: Dict[str, List[EmailMessage]] = defaultdict(list)
    for m in pending:
        by_prospect[m.prospect_id].append(m)
    overdue = []
    for msgs in by_prospect.values():
        first = min(msgs, key=lambda m: m.scheduled_at or now)
        if (first.scheduled_at or now) <= now:
            overdue.append((first.scheduled_at or now, first, msgs))
    overdue.sort(key=lambda t: (t[0], t[1].message_id))
    return overdue


# =============================
# Send-failure protection (item 14)
# =============================

INBOX_PAUSED_KIND = "INBOX_PAUSED"


def pause_inbox_for_sender_rejections(db: Session, inbox_id: str, error: str) -> bool:
    """
    Set the mailbox PAUSED with the reason (its activity log), and tell the workspace admins.
    The alert centre shows it as an INBOX_PAUSED alert. Caller commits. Never raises.
    """
    try:
        ctx = _context(db)
        inbox = ctx.inbox(inbox_id)
        if not inbox or (inbox.status or "").upper() != "ACTIVE":
            return False
        reason = (f"Paused automatically: SES refused {send_breaker.SENDER_REJECTION_THRESHOLD} sends from "
                  f"{inbox.email_address} within {int(send_breaker.SENDER_REJECTION_WINDOW // 60)} minutes "
                  f"because the sending identity is not usable ({error[:300]}). Verify the address / "
                  f"MAIL FROM domain, then resume the mailbox.")
        inbox.status = "PAUSED"
        ctx.add_inbox_event(
            tenant_id=inbox.tenant_id, inbox_id=inbox.inbox_id, event_type="ISSUE", status="FAILED",
            subject="Mailbox paused: sender identity rejected", detail=reason,
            event_metadata={"code": "SENDER_IDENTITY_REJECTED"}, event_time=utcnow(),
        )
        notify(db, inbox.tenant_id, ctx.tenant_admin_ids(inbox.tenant_id), INBOX_PAUSED_KIND,
               f"Mailbox paused: {inbox.email_address}", reason, "/app/email-accounts",
               dedupe_key=f"inbox-paused:{inbox.inbox_id}", dedupe_days=1)
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
