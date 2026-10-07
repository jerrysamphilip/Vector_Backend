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
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.inbox_warmup_metric import InboxWarmupMetric
from app.models.provider_reputation import ExternalReputationMetric
from app.models.sending_inbox import SendingInbox
from app.services import warmup_imap, warmup_smtp

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

    def __init__(self):
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
            inbox_query = db.query(SendingInbox)
            if tenant_id:
                inbox_query = inbox_query.filter(SendingInbox.tenant_id == tenant_id)

            inboxes = inbox_query.order_by(SendingInbox.tenant_id, SendingInbox.email_address).all()
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
        except Exception as exc:
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
        except Exception as exc:
            self._register_send_failure(db, peer, peer_metric, str(exc))
            return False

        if not result.get("success"):
            self._register_send_failure(db, peer, peer_metric, result.get("error", "Warmup reply failed"))
            return False

        peer_metric.actual_sends += 1
        peer.warmup_today_sent = (peer.warmup_today_sent or 0) + 1
        peer.warmup_last_activity_at = datetime.utcnow()

        sender_metric.reply_count += 1
        sender_metric.received_count += 1
        sender.warmup_today_replied = (sender.warmup_today_replied or 0) + 1
        sender.warmup_last_activity_at = datetime.utcnow()

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
        inbox.warmup_last_activity_at = datetime.utcnow()
        peer.warmup_last_activity_at = datetime.utcnow()

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
            inbox = db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).first()
            peer = db.query(SendingInbox).filter(SendingInbox.inbox_id == peer_inbox_id).first()

            if not peer or not peer.imap_host or not peer.imap_username:
                return
            from app.services.ms365_oauth import imap_secret
            secret = imap_secret(db, peer)
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
                inbox.warmup_last_activity_at = datetime.utcnow()
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
        now = datetime.utcnow()
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
        metric = db.query(InboxWarmupMetric).filter(
            InboxWarmupMetric.inbox_id == inbox.inbox_id,
            InboxWarmupMetric.metric_date == metric_date,
        ).first()
        if metric:
            return metric

        metric = InboxWarmupMetric(
            metric_id=str(uuid.uuid4()),
            tenant_id=inbox.tenant_id,
            inbox_id=inbox.inbox_id,
            metric_date=metric_date,
            reputation_score=inbox.warmup_reputation or 65.0,
            pool_tier=inbox.warmup_pool or "FOUNDATION",
        )
        db.add(metric)
        db.flush()
        return metric

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

        if inbox.last_sync_at and inbox.last_sync_at < datetime.utcnow() - timedelta(hours=24):
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
        # Blend today's metric with the last 7 days of history so that a single
        # low-activity day (weekend, bank holiday) does not crash the reputation.
        history = (
            db.query(InboxWarmupMetric)
            .filter(
                InboxWarmupMetric.inbox_id == inbox.inbox_id,
                InboxWarmupMetric.metric_date != metric.metric_date,
            )
            .order_by(desc(InboxWarmupMetric.metric_date))
            .limit(6)
            .all()
        )
        all_metrics = [metric] + list(history)

        total_sent = sum(max(m.actual_sends, 0) for m in all_metrics)
        total_opened = sum(m.open_count or 0 for m in all_metrics)
        total_replied = sum(m.reply_count or 0 for m in all_metrics)
        total_saved = sum(m.saved_from_spam_count or 0 for m in all_metrics)
        total_bounced = sum(m.bounce_count or 0 for m in all_metrics)
        total_complaints = sum(m.complaint_count or 0 for m in all_metrics)

        denom = max(total_sent, 1)
        open_rate = (total_opened / denom) * 100
        reply_rate = (total_replied / denom) * 100

        score = 58.0
        score += min(inbox.warmup_day or 0, 20) * 1.6
        score += min(open_rate, 95) * 0.18
        score += min(reply_rate, 40) * 0.25
        score += min(total_saved, 5) * 1.2
        score -= total_bounced * 18
        score -= total_complaints * 22

        if inbox.warmup_issue_code in {"POOL_TOO_SMALL", "SEND_FAILED"}:
            score -= 10
        elif inbox.warmup_issue_code in {"SYNC_STALE", "IMAP_NOT_CONFIGURED", "SMTP_NOT_CONFIGURED"}:
            score -= 4

        domain = inbox.email_address.split("@")[-1].lower() if "@" in inbox.email_address else ""
        if domain:
            external_metric = db.query(ExternalReputationMetric).filter(
                ExternalReputationMetric.domain_name == domain
            ).order_by(desc(ExternalReputationMetric.metric_date)).first()
            if external_metric:
                if external_metric.complaint_rate:
                    score -= float(external_metric.complaint_rate) * 200
                if external_metric.bounce_rate:
                    score -= float(external_metric.bounce_rate) * 120
                if external_metric.reputation_score:
                    score = (score * 0.7) + (float(external_metric.reputation_score) * 0.3)

        return max(25.0, min(round(score, 1), 100.0))

    def _pool_for_score(self, score: float, inbox: SendingInbox) -> str:
        warmup_day = inbox.warmup_day or 0
        if inbox.warmup_issue_code in {"POOL_TOO_SMALL", "SEND_FAILED", "MAILBOX_PAUSED", "SMTP_NOT_CONFIGURED"}:
            return "FOUNDATION"
        if score >= 99 and warmup_day >= 21:
            return "ULTRA_PREMIUM"
        if score >= 97 and warmup_day >= 14:
            return "PREMIUM"
        if score >= 92 and warmup_day >= 7:
            return "GROWTH"
        return "FOUNDATION"

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
        db.add(
            InboxWarmupEvent(
                event_id=str(uuid.uuid4()),
                tenant_id=inbox.tenant_id,
                inbox_id=inbox.inbox_id,
                peer_inbox_id=peer.inbox_id if peer else None,
                event_type=event_type,
                status=status,
                subject=subject,
                detail=detail,
                event_metadata=metadata or {},
                event_time=datetime.utcnow(),
            )
        )

    def get_overview(self, db: Session, tenant_id: str) -> dict:
        inboxes = db.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id).all()
        if not inboxes:
            return {
                "total_accounts": 0,
                "warmup_enabled": 0,
                "accounts_with_issues": 0,
                "average_reputation": 0,
                "current_pool": "FOUNDATION",
                "pool_growth_score": 0,
                "planned_today": 0,
                "warmup_sent_today": 0,
                "warmup_replied_today": 0,
                "saved_today": 0,
            }

        enabled = [i for i in inboxes if i.warmup_enabled]
        reputations = [i.warmup_reputation or 0 for i in enabled]
        issue_count = sum(1 for i in enabled if i.warmup_issue_code)
        pool = self._dominant_pool(enabled)
        avg_reputation = round(sum(reputations) / len(reputations), 1) if reputations else 0
        planned_today = sum(i.warmup_daily_target or 0 for i in enabled)
        sent_today = sum(i.warmup_today_sent or 0 for i in enabled)
        replied_today = sum(i.warmup_today_replied or 0 for i in enabled)
        saved_today = sum(i.warmup_today_saved or 0 for i in enabled)

        pool_score = min(
            100,
            round(avg_reputation + (sum(i.warmup_day or 0 for i in enabled) / max(len(enabled), 1)), 0),
        )

        return {
            "total_accounts": len(inboxes),
            "warmup_enabled": len(enabled),
            "accounts_with_issues": issue_count,
            "average_reputation": avg_reputation,
            "current_pool": pool,
            "pool_growth_score": pool_score,
            "planned_today": planned_today,
            "warmup_sent_today": sent_today,
            "warmup_replied_today": replied_today,
            "saved_today": saved_today,
        }

    def get_inbox_activity(self, db: Session, inbox_id: str, tenant_id: str, limit: int = 25) -> dict:
        inbox = db.query(SendingInbox).filter(
            SendingInbox.inbox_id == inbox_id,
            SendingInbox.tenant_id == tenant_id,
        ).first()
        if not inbox:
            raise ValueError("Inbox not found")

        metric = self._get_or_create_metric(db, inbox)
        events = db.query(InboxWarmupEvent).filter(
            InboxWarmupEvent.inbox_id == inbox_id,
            InboxWarmupEvent.tenant_id == tenant_id,
        ).order_by(desc(InboxWarmupEvent.event_time)).limit(limit).all()

        history = db.query(InboxWarmupMetric).filter(
            InboxWarmupMetric.inbox_id == inbox_id,
            InboxWarmupMetric.tenant_id == tenant_id,
        ).order_by(desc(InboxWarmupMetric.metric_date)).limit(14).all()

        return {
            "inbox": inbox,
            "today_metric": metric,
            "history": list(reversed(history)),
            "events": events,
        }

    async def run_continuous(self, interval_seconds: int = 900):
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

    def stop(self):
        self.is_running = False

    def _display_name(self, email_address: str) -> str:
        local = email_address.split("@")[0]
        return " ".join(chunk.capitalize() for chunk in local.replace("_", ".").split(".") if chunk)

    def _dominant_pool(self, inboxes: List[SendingInbox]) -> str:
        ranking = ["FOUNDATION", "GROWTH", "PREMIUM", "ULTRA_PREMIUM"]
        highest = "FOUNDATION"
        for inbox in inboxes:
            if ranking.index(inbox.warmup_pool or "FOUNDATION") > ranking.index(highest):
                highest = inbox.warmup_pool
        return highest

    def _today_key(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")


warmup_service = WarmupService()
