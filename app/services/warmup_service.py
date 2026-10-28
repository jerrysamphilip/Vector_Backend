"""
Automatic mailbox warmup service.

This is a mailbox-level warmup engine that:
- ramps daily warmup targets
- auto-adjusts warmup volume around campaign sending
- sends real warmup emails across connected inboxes in the same tenant
- logs mailbox warmup activity
- computes warmup reputation / pool tier / mailbox issues
"""

import asyncio
import logging
import random
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from app.core.clock import utcnow
from app.core.config import settings
from app.core.database import SessionLocal
from app.domain import sending_warmup as scoring
from app.models.inbox_warmup_metric import InboxWarmupMetric
from app.models.sending_inbox import SendingInbox
from app.repositories.sending_warmup import Session, SqlAlchemyWarmupRepository
from app.services import ms365_oauth, warmup_imap, warmup_smtp

logger = logging.getLogger(__name__)


class WarmupService:
    SUBJECT_BANK = [
        "Quick follow-up on yesterday",
        "Looping back before tomorrow",
        "Sharing this before I forget",
        "Thanks again for the note",
        "Quick sync on the timeline",
        "Thought this might help",
        "One more thing on this",
        "Circling back here",
    ]

    BODY_BANK = [
        "Wanted to keep the thread moving. The next step still looks straightforward from my side.",
        "Sharing a quick note so this does not get buried before the day gets busy.",
        "Small follow-up here. I have the draft ready and can send the final version once you confirm.",
        "Just keeping this active in case it slipped under other priorities today.",
        "Dropping this here so we have it in one place before the next handoff.",
        "Quick check-in from my end. Nothing urgent, but I wanted to keep the thread warm.",
    ]

    REPLY_BANK = [
        "Saw this come through. That works for me.",
        "Thanks, keeping this in view from my side too.",
        "Received. I will keep the thread moving.",
        "This reached me cleanly. Appreciate the follow-up.",
        "Looks good. Leaving this active for the next pass.",
    ]

    def __init__(self) -> None:
        self.is_running = False
        self.max_sends_per_cycle = int(getattr(settings, "WARMUP_MAX_SENDS_PER_CYCLE", 3) or 3)
        self.reply_rate_default = int(getattr(settings, "WARMUP_REPLY_RATE_DEFAULT", 35) or 35)
        self.randomize_variance = int(getattr(settings, "WARMUP_RANDOMIZE_VARIANCE", 15) or 15)

    async def run_cycle(self, db: Optional[Session] = None, tenant_id: Optional[str] = None) -> dict:
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        stats = {
            "processed_inboxes": 0,
            "warmup_emails_sent": 0,
            "warmup_replies_sent": 0,
            "issues_detected": 0,
        }

        try:
            # Suspended / deleted workspaces do not warm up (no sending at all)
            inboxes = SqlAlchemyWarmupRepository(db).inboxes_to_warm(tenant_id)
            if not inboxes:
                return stats

            inboxes_by_tenant: Dict[str, List[SendingInbox]] = defaultdict(list)
            for inbox in inboxes:
                inboxes_by_tenant[inbox.tenant_id].append(inbox)

            for tenant_inboxes in inboxes_by_tenant.values():
                tenant_stats = await self._process_tenant_pool(db, tenant_inboxes)
                for key, value in tenant_stats.items():
                    stats[key] += value

            db.commit()
            return stats
        except Exception:
            db.rollback()
            logger.exception("[Warmup] Warmup cycle failed")
            raise
        finally:
            if close_db:
                db.close()

    async def _process_tenant_pool(self, db: Session, inboxes: List[SendingInbox]) -> dict:
        stats = {
            "processed_inboxes": 0,
            "warmup_emails_sent": 0,
            "warmup_replies_sent": 0,
            "issues_detected": 0,
        }

        active_pool = [i for i in inboxes if i.warmup_enabled and i.status == "ACTIVE"]

        for inbox in inboxes:
            self._sync_daily_rollover(inbox)
            metric = self._get_or_create_metric(db, inbox)
            issue_code, issue_message, blocking = self._assess_health(inbox, active_pool)

            if issue_code:
                stats["issues_detected"] += 1

            self._apply_issue_state(inbox, metric, issue_code, issue_message)
            daily_target = self._compute_daily_target(inbox)
            metric.planned_sends = daily_target
            inbox.warmup_daily_target = daily_target  # persist so UI/overview reflect current target
            stats["processed_inboxes"] += 1

            if blocking or not inbox.warmup_enabled:
                self._refresh_scores(db, inbox, metric)
                continue

            peers = [peer for peer in active_pool if peer.inbox_id != inbox.inbox_id and peer.status == "ACTIVE"]
            if not peers:
                self._apply_issue_state(
                    inbox,
                    metric,
                    "POOL_TOO_SMALL",
                    "At least two active warmup mailboxes are required to run automated warmup.",
                )
                self._refresh_scores(db, inbox, metric)
                stats["issues_detected"] += 1
                continue

            remaining = max(metric.planned_sends - (inbox.warmup_today_sent or 0), 0)
            cycle_budget = min(remaining, self.max_sends_per_cycle)

            if cycle_budget <= 0:
                self._refresh_scores(db, inbox, metric)
                continue

            random.shuffle(peers)
            for index in range(cycle_budget):
                peer = peers[index % len(peers)]
                sent, replied = await self._send_warmup_pair(db, inbox, peer)
                stats["warmup_emails_sent"] += int(sent)
                stats["warmup_replies_sent"] += int(replied)

            self._refresh_scores(db, inbox, metric)

        return stats

    async def _send_warmup_pair(self, db: Session, inbox: SendingInbox, peer: SendingInbox) -> Tuple[bool, bool]:
        sender_metric = self._get_or_create_metric(db, inbox)
        peer_metric = self._get_or_create_metric(db, peer)

        subject = random.choice(self.SUBJECT_BANK)
        if inbox.warmup_identifier:
            subject = f"{subject} [{inbox.warmup_identifier}]"

        body = (
            f"Hi {self._display_name(peer.email_address)},\n\n"
            f"{random.choice(self.BODY_BANK)}\n\n"
            f"Best,\n{self._display_name(inbox.email_address)}"
        )

        try:
            result = await warmup_smtp.send_via_inbox(
                inbox=inbox,
                to_email=peer.email_address,
                subject=subject,
                body=body,
                extra_headers={
                    "X-Outreach-Warmup": "true",
                    "X-Warmup-Inbox": inbox.inbox_id,
                },
            )
        except Exception as exc:  # provider send failed: count it against the inbox, keep the loop going
            logger.warning("Warmup send from %s failed: %s", inbox.email_address, exc)
            self._register_send_failure(db, inbox, sender_metric, str(exc))
            return False, False

        if not result.get("success"):
            self._register_send_failure(db, inbox, sender_metric, result.get("error", "Warmup send failed"))
            return False, False

        self._record_send_success(db, inbox, peer, sender_metric, peer_metric, subject)

        # Schedule real IMAP engagement on the peer inbox (2-8 min delay)
        if peer.imap_host and peer.imap_username and peer.imap_password:
            delay = random.randint(120, 480)
            asyncio.create_task(
                self._do_imap_engagement(
                    inbox_id=inbox.inbox_id,
                    peer_inbox_id=peer.inbox_id,
                    subject=subject,
                    sender_email=inbox.email_address,
                    delay_seconds=delay,
                )
            )

        replied = await self._maybe_send_reply(
            db=db,
            sender=inbox,
            peer=peer,
            subject=subject,
            internet_message_id=result.get("internet_message_id"),
        )
        return True, replied

    async def _maybe_send_reply(
        self,
        db: Session,
        sender: SendingInbox,
        peer: SendingInbox,
        subject: str,
        internet_message_id: Optional[str],
    ) -> bool:
        sender_metric = self._get_or_create_metric(db, sender)
        peer_metric = self._get_or_create_metric(db, peer)

        reply_rate = peer.warmup_reply_rate_target or self.reply_rate_default
        if random.randint(1, 100) > reply_rate:
            return False

        peer_remaining = max(peer_metric.planned_sends - (peer.warmup_today_sent or 0), 0)
        if peer_remaining <= 0:
            # No send budget left for the peer. Record a simulated reply event only.
            sender_metric.reply_count += 1
            sender.warmup_today_replied = (sender.warmup_today_replied or 0) + 1
            self._record_event(
                db,
                inbox=sender,
                peer=peer,
                event_type="REPLIED",
                subject=f"Re: {subject}",
                detail=f"{peer.email_address} replied inside the warmup pool.",
                metadata={"mode": "simulated"},
            )
            return True

        reply_body = (
            f"Hi {self._display_name(sender.email_address)},\n\n"
            f"{random.choice(self.REPLY_BANK)}\n\n"
            f"Best,\n{self._display_name(peer.email_address)}"
        )

        try:
            result = await warmup_smtp.send_via_inbox(
                inbox=peer,
                to_email=sender.email_address,
                subject=f"Re: {subject}",
                body=reply_body,
                extra_headers={
                    "X-Outreach-Warmup": "true",
                    "In-Reply-To": internet_message_id or "",
                    "References": internet_message_id or "",
                },
            )
        except Exception as exc:  # provider send failed: count it against the peer, keep the loop going
            logger.warning("Warmup reply from %s failed: %s", peer.email_address, exc)
            self._register_send_failure(db, peer, peer_metric, str(exc))
            return False

        if not result.get("success"):
            self._register_send_failure(db, peer, peer_metric, result.get("error", "Warmup reply failed"))
            return False

        peer_metric.actual_sends += 1
        peer.warmup_today_sent = (peer.warmup_today_sent or 0) + 1
        peer.warmup_last_activity_at = utcnow()

        sender_metric.reply_count += 1
        sender_metric.received_count += 1
        sender.warmup_today_replied = (sender.warmup_today_replied or 0) + 1
        sender.warmup_last_activity_at = utcnow()

        self._record_event(
            db,
            inbox=peer,
            peer=sender,
            event_type="SENT",
            subject=f"Re: {subject}",
            detail=f"Warmup reply sent to {sender.email_address}",
            metadata={"mode": "reply"},
        )
        self._record_event(
            db,
            inbox=sender,
            peer=peer,
            event_type="REPLIED",
            subject=f"Re: {subject}",
            detail=f"{peer.email_address} replied to a warmup email.",
            metadata={"mode": "reply"},
        )
        return True

    def _record_send_success(
        self,
        db: Session,
        inbox: SendingInbox,
        peer: SendingInbox,
        sender_metric: InboxWarmupMetric,
        peer_metric: InboxWarmupMetric,
        subject: str,
    ) -> None:
        sender_metric.actual_sends += 1
        peer_metric.received_count += 1

        inbox.warmup_today_sent = (inbox.warmup_today_sent or 0) + 1
        inbox.warmup_last_activity_at = utcnow()
        peer.warmup_last_activity_at = utcnow()

        self._record_event(
            db,
            inbox=inbox,
            peer=peer,
            event_type="SENT",
            subject=subject,
            detail=f"Warmup email sent to {peer.email_address}",
            metadata={"recipient": peer.email_address},
        )
        self._record_event(
            db,
            inbox=peer,
            peer=inbox,
            event_type="RECEIVED",
            subject=subject,
            detail=f"Warmup email received from {inbox.email_address}",
            metadata={"sender": inbox.email_address},
        )
        # Open and spam-rescue events are recorded by _do_imap_engagement
        # after a real IMAP check (2-8 min delay). No fake dice rolls.

    async def _do_imap_engagement(
        self,
        inbox_id: str,
        peer_inbox_id: str,
        subject: str,
        sender_email: str,
        delay_seconds: int,
    ) -> None:
        """
        Background task: after delay_seconds, connect to peer's IMAP, find the
        warmup email, rescue from spam if needed, and mark as read.
        Opens its own DB session so it is independent of the main cycle session.
        """
        await asyncio.sleep(delay_seconds)

        db = SessionLocal()
        try:
            repo = SqlAlchemyWarmupRepository(db)
            inbox = repo.inbox(inbox_id)
            peer = repo.inbox(peer_inbox_id)

            if not peer or not peer.imap_host or not peer.imap_username:
                return
            secret = ms365_oauth.imap_secret(db, peer)
            if not secret:
                return

            result = await asyncio.to_thread(
                warmup_imap._engage_blocking,
                peer.imap_host,
                peer.imap_port or 993,
                peer.imap_username,
                secret,
                subject,
                sender_email,
            )

            if result.get("error"):
                logger.warning("[Warmup] IMAP engagement error for %s: %s", peer.email_address, result["error"])

            if not result.get("found"):
                logger.debug("[Warmup] IMAP: email not found yet in %s's mailbox", peer.email_address)
                return

            # Update metrics on the SENDER inbox (its email got engaged)
            if inbox:
                metric = self._get_or_create_metric(db, inbox)
                metric.open_count += 1
                inbox.warmup_today_opened = (inbox.warmup_today_opened or 0) + 1
                inbox.warmup_last_activity_at = utcnow()
                self._record_event(
                    db,
                    inbox=inbox,
                    peer=peer,
                    event_type="OPENED",
                    subject=subject,
                    detail=f"{peer.email_address} opened the warmup email (real IMAP).",
                    metadata={"mode": "imap", "delay_seconds": delay_seconds},
                )

                if result.get("rescued_from_spam"):
                    metric.saved_from_spam_count += 1
                    inbox.warmup_today_saved = (inbox.warmup_today_saved or 0) + 1
                    self._record_event(
                        db,
                        inbox=inbox,
                        peer=peer,
                        event_type="SAVED_FROM_SPAM",
                        subject=subject,
                        detail=f"{peer.email_address} rescued warmup email from spam (real IMAP).",
                        metadata={"mode": "imap"},
                    )

            db.commit()
        except Exception:
            logger.exception("[Warmup] _do_imap_engagement failed")
            db.rollback()
        finally:
            db.close()

    def _register_send_failure(self, db: Session, inbox: SendingInbox, metric: InboxWarmupMetric, message: str) -> None:
        # Do NOT count SMTP/auth failures as bounces — they're infrastructure errors,
        # not recipient bounces.  Only actual bounce DSN messages should affect bounce_count.
        inbox.warmup_issue_code = "SEND_FAILED"
        inbox.warmup_issue_message = message[:255]
        inbox.warmup_status = "ISSUE"
        self._record_event(
            db,
            inbox=inbox,
            peer=None,
            event_type="ISSUE",
            subject=None,
            detail=message[:255],
            metadata={},
            status="FAILED",
        )

    def _sync_daily_rollover(self, inbox: SendingInbox) -> None:
        now = utcnow()
        today = now.date()
        if not inbox.last_daily_reset or inbox.last_daily_reset.date() < today:
            inbox.emails_sent_today = 0
            inbox.warmup_today_sent = 0
            inbox.warmup_today_opened = 0
            inbox.warmup_today_replied = 0
            inbox.warmup_today_saved = 0
            inbox.last_daily_reset = now

        if inbox.warmup_enabled and inbox.warmup_start_date:
            inbox.warmup_day = max((now - inbox.warmup_start_date).days, 0)

    def _get_or_create_metric(self, db: Session, inbox: SendingInbox) -> InboxWarmupMetric:
        metric_date = self._today_key()
        repo = SqlAlchemyWarmupRepository(db)
        metric = repo.metric(inbox.inbox_id, metric_date)
        if metric:
            return metric
        return repo.add_metric(
            metric_id=str(uuid.uuid4()),
            tenant_id=inbox.tenant_id,
            inbox_id=inbox.inbox_id,
            metric_date=metric_date,
            reputation_score=inbox.warmup_reputation or 65.0,
            pool_tier=inbox.warmup_pool or "FOUNDATION",
        )

    def _assess_health(
        self,
        inbox: SendingInbox,
        active_pool: List[SendingInbox],
    ) -> Tuple[Optional[str], Optional[str], bool]:
        if not inbox.warmup_enabled:
            return "WARMUP_DISABLED", "Automated warmup is turned off for this mailbox.", True

        if inbox.status == "PAUSED":
            return "MAILBOX_PAUSED", "Mailbox is paused and cannot send warmup traffic.", True

        if inbox.is_in_cooling:
            return "INBOX_COOLING", "Mailbox is in a cooling window after hitting its sending threshold.", True

        if len(active_pool) < 2:
            return "POOL_TOO_SMALL", "Warmup requires at least two active mailboxes in the pool.", True

        if not inbox.smtp_host or not inbox.smtp_password:
            return (
                "SMTP_NOT_CONFIGURED",
                "Per-inbox SMTP not configured. Warmup sends will use the SES relay (real IMAP engagement disabled).",
                False,
            )

        if not inbox.imap_host or not inbox.imap_username or not inbox.imap_password:
            return "IMAP_NOT_CONFIGURED", "IMAP not configured. Emails are sent but open/spam-rescue tracking is disabled.", False

        if inbox.last_sync_at and inbox.last_sync_at < utcnow() - timedelta(hours=24):
            return "SYNC_STALE", "IMAP sync is stale. Warmup is still running, but mailbox health is degraded.", False

        return None, None, False

    def _apply_issue_state(
        self,
        inbox: SendingInbox,
        metric: InboxWarmupMetric,
        issue_code: Optional[str],
        issue_message: Optional[str],
    ) -> None:
        inbox.warmup_issue_code = issue_code
        inbox.warmup_issue_message = issue_message
        metric.issue_code = issue_code

        if not inbox.warmup_enabled:
            inbox.warmup_status = "DISABLED"
        elif issue_code in {"MAILBOX_PAUSED"}:
            inbox.warmup_status = "PAUSED"
        elif issue_code in {"INBOX_COOLING", "POOL_TOO_SMALL", "SEND_FAILED"}:
            inbox.warmup_status = "ISSUE"
        elif issue_code in {"SMTP_NOT_CONFIGURED", "IMAP_NOT_CONFIGURED", "SYNC_STALE"}:
            # Non-blocking degraded state — warmup still runs via SES fallback
            inbox.warmup_status = "ACTIVE"
        else:
            inbox.warmup_status = "ACTIVE"

    def _compute_daily_target(self, inbox: SendingInbox) -> int:
        base_target = inbox.warmup_effective_target

        if inbox.warmup_auto_adjust:
            base_target = max(0, base_target - (inbox.emails_sent_today or 0))

        if inbox.warmup_randomize and base_target > 0:
            variance = max(1, int(base_target * (self.randomize_variance / 100)))
            base_target += random.randint(-variance, variance)

        return max(0, min(base_target, inbox.daily_limit or base_target))

    def _refresh_scores(self, db: Session, inbox: SendingInbox, metric: InboxWarmupMetric) -> None:
        score = self._score_reputation(db, inbox, metric)
        pool = self._pool_for_score(score, inbox)
        metric.reputation_score = score
        metric.pool_tier = pool
        metric.issue_code = inbox.warmup_issue_code

        inbox.warmup_reputation = score
        inbox.warmup_pool = pool

    def _score_reputation(self, db: Session, inbox: SendingInbox, metric: InboxWarmupMetric) -> float:
        """Today's metric blended with the last week of history (so one quiet day does not crash the score),
        the open issue, and the domain's external reputation (app/domain/sending_warmup.py)."""
        repo = SqlAlchemyWarmupRepository(db)
        history = repo.recent_metrics(inbox.inbox_id, metric.metric_date, scoring.HISTORY_DAYS)
        domain = scoring.email_domain(inbox.email_address)
        external = repo.external_reputation(inbox.tenant_id, domain) if domain else None
        return scoring.reputation_score([metric] + list(history), inbox.warmup_day, inbox.warmup_issue_code, external)

    def _pool_for_score(self, score: float, inbox: SendingInbox) -> str:
        return scoring.pool_for_score(score, inbox.warmup_day, inbox.warmup_issue_code)

    def _record_event(
        self,
        db: Session,
        inbox: SendingInbox,
        peer: Optional[SendingInbox],
        event_type: str,
        subject: Optional[str],
        detail: Optional[str],
        metadata: Optional[dict],
        status: str = "SUCCESS",
    ) -> None:
        SqlAlchemyWarmupRepository(db).add_event(
            event_id=str(uuid.uuid4()),
            tenant_id=inbox.tenant_id,
            inbox_id=inbox.inbox_id,
            peer_inbox_id=peer.inbox_id if peer else None,
            event_type=event_type,
            status=status,
            subject=subject,
            detail=detail,
            event_metadata=metadata or {},
            event_time=utcnow(),
        )

    def get_overview(self, db: Session, tenant_id: str) -> dict:
        return scoring.warmup_overview(SqlAlchemyWarmupRepository(db).tenant_inboxes(tenant_id))

    def get_inbox_activity(self, db: Session, inbox_id: str, tenant_id: str, limit: int = 25) -> dict:
        repo = SqlAlchemyWarmupRepository(db)
        inbox = repo.tenant_inbox(tenant_id, inbox_id)
        if not inbox:
            raise ValueError("Inbox not found")
        metric = self._get_or_create_metric(db, inbox)
        events = repo.tenant_events(tenant_id, inbox_id, limit)
        history = repo.tenant_metric_history(tenant_id, inbox_id, 14)
        return {
            "inbox": inbox,
            "today_metric": metric,
            "history": list(reversed(history)),
            "events": events,
        }

    async def run_continuous(self, interval_seconds: int = 900) -> None:
        if self.is_running:
            logger.warning("[Warmup] Already running")
            return

        self.is_running = True
        while self.is_running:
            try:
                await self.run_cycle()
            except Exception:
                logger.exception("[Warmup] Continuous warmup loop failed")
            await asyncio.sleep(interval_seconds)

    def stop(self) -> None:
        self.is_running = False

    def _display_name(self, email_address: str) -> str:
        return scoring.display_name(email_address)

    def _dominant_pool(self, inboxes: List[SendingInbox]) -> str:
        return scoring.dominant_pool(inbox.warmup_pool for inbox in inboxes)

    def _today_key(self) -> str:
        return utcnow().strftime("%Y-%m-%d")


warmup_service = WarmupService()
