# app/jobs.py
"""
Background loops (email scheduler, IMAP sync, deliverability, warmup, CRM purge, send safety,
sales jobs), shared by the dedicated worker (`python -m app.worker`) and, when
RUN_BACKGROUND_JOBS is true, the API process.

Every iteration runs under a MySQL named lock (GET_LOCK('vector:job:<name>:<db>', 0)) taken on
its own connection, so however many processes or replicas run the loops, each iteration runs
in one place only; the others skip it and try again next interval. The scheduler's per-message
send_key claim stays as the second line of defence against double sends.

HEARTBEATS holds, per loop, the last time (time.time()) the loop was alive; the worker's
/health/ready and the metrics read it.
"""
import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from sqlalchemy import create_engine, pool, text

import app.models  # noqa: F401  (resolve SQLAlchemy relationships before any query)
from app.core.config import settings
from app.core.observability import record_job_heartbeat
from app.core.database import SessionLocal
from app.services import domain_health_jobs, duplicate_scan, import_service, reply_classifier, sending_alerts
from app.services.alert_center_service import alert_center_service as sending_alert_center

logger = logging.getLogger("app.jobs")

# name -> last time (epoch seconds) the loop was alive: set when an iteration starts and ends,
# including iterations skipped because another process holds the lock
HEARTBEATS: Dict[str, float] = {}
# name -> last time an iteration actually ran here (lock acquired) and finished
LAST_RUN: Dict[str, float] = {}
# name -> iterations skipped here because another process held the lock
LOCK_SKIPS: Dict[str, int] = {}
# name -> iterations that raised
FAILURES: Dict[str, int] = {}


@dataclass(frozen=True)
class Job:
    name: str
    interval: float  # seconds between iterations
    run: Callable[[], Awaitable[Optional[float]]]  # may return a shorter sleep for the next pass

    @property
    def stale_after(self) -> float:
        """Heartbeat age after which the loop counts as stuck (worker readiness)."""
        return max(3 * self.interval, 180)


# ---------------------------------------------------------------------------
# Named locks (leader election per iteration)
# ---------------------------------------------------------------------------
_lock_engine = None


def _get_lock_engine():
    # Own connections (not the app pool): a lock lives exactly as long as its connection
    global _lock_engine
    if _lock_engine is None:
        _lock_engine = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool,
                                     connect_args={"connect_timeout": 10})
    return _lock_engine


def lock_name(job_name: str) -> str:
    # Scoped to the database so environments sharing a MySQL server do not block each other
    database = _get_lock_engine().url.database or ""
    return f"vector:job:{job_name}:{database}"[:64]


def try_acquire(job_name: str):
    """Return an open connection holding the job's lock, or None if another process holds it."""
    conn = _get_lock_engine().connect()
    try:
        got = conn.execute(text("SELECT GET_LOCK(:n, 0)"), {"n": lock_name(job_name)}).scalar()
    except Exception:
        conn.close()
        raise
    if got == 1:
        return conn
    conn.close()
    return None


def release(conn, job_name: str) -> None:
    try:
        conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": lock_name(job_name)})
    except Exception as e:
        # Closing the connection below releases the lock anyway
        logger.warning("Job %s: RELEASE_LOCK failed (%s)", job_name, type(e).__name__)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Loop bodies (one iteration each)
# ---------------------------------------------------------------------------
async def _run_in_thread(fn, *args):
    # Blocking DB / IMAP / DNS / SES work runs in the default thread pool so the event loop stays free
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def scheduler_iteration() -> Optional[float]:
    from app.services.email_scheduler_service import email_scheduler
    from app.services import platform_flags
    if await _run_in_thread(platform_flags.scheduler_paused):
        return None  # paused through POST /scheduler/stop (shared by every process)
    stats = await email_scheduler.process_scheduled_emails() or {}
    # Come straight back while there is work, so pace is set by the inboxes (BR-DF-01)
    # Straight back while there is work, else when the soonest paced mailbox is ready (A8)
    return stats.get("next_run_in")


async def imap_iteration() -> None:
    from app.services.imap_sync_service import imap_sync_service
    with SessionLocal() as db:
        await _run_in_thread(imap_sync_service.sync_all_active_inboxes, db)


async def deliverability_iteration() -> None:
    from app.services.alert_center_service import alert_center_service
    from app.services.deliverability_service import deliverability_service

    def _sync():
        with SessionLocal() as db:
            deliverability_service.sync_ses_metrics(db)
            # Sending domains are tenant-owned (one row per tenant and domain). DNS and the SES
            # identity are facts about the domain, so each name is scanned once and the result
            # written to every tenant's row for it; rows without a tenant are skipped.
            names = sorted({row[0].lower() for row in db.execute(text(
                "SELECT domain_name FROM sending_domains WHERE tenant_id IS NOT NULL")).fetchall()})
            for name in names:
                deliverability_service.perform_dns_scan(name, db)

    await _run_in_thread(_sync)
    with SessionLocal() as db:
        await alert_center_service.evaluate_and_dispatch_all(db)


async def warmup_iteration() -> None:
    from app.services.warmup_service import warmup_service
    with SessionLocal() as db:
        await warmup_service.run_cycle(db)


async def crm_purge_iteration() -> None:
    # Contacts and companies deleted more than 90 days ago are purged (BR-CM-35)
    from app.services.crm import purge_expired

    def _purge():
        with SessionLocal() as db:
            return purge_expired(db)

    purged = await _run_in_thread(_purge)
    if purged:
        logger.info("CRM purge: permanently deleted %s record(s) past the 90-day restore window", purged)


_last_health = 0.0


async def send_safety_iteration() -> None:
    # Every sent email gets a final status within 15 minutes (BR-DF-04); domain / campaign
    # health is recomputed hourly with auto-pause on risk (BR-DF-07)
    global _last_health
    from app.services.send_safety import reconcile_delivery_status, run_health_cycle

    def _reconcile():
        with SessionLocal() as db:
            return reconcile_delivery_status(db)

    def _health():
        with SessionLocal() as db:
            return run_health_cycle(db)

    await _run_in_thread(_reconcile)
    if time.monotonic() - _last_health >= 3600 or not _last_health:
        result = await _run_in_thread(_health)
        _last_health = time.monotonic()
        if result.get("paused"):
            logger.info("Health check paused %s campaign(s)", result["paused"])


_last_hourly = 0.0
_last_sync = 0.0


async def sales_jobs_iteration() -> None:
    # Notification emails every minute; lead recycling, stale-deal alerts and forecast snapshots hourly
    # (BR-SF-02, 05, 07); calendar / email sync every 10 minutes (BR-SF-16)
    global _last_hourly, _last_sync
    from app.services import account_sync, sales_jobs
    from app.services.notifications import email_pending

    with SessionLocal() as db:
        await email_pending(db)
    if not _last_hourly or time.monotonic() - _last_hourly >= 3600:
        def _hourly():
            with SessionLocal() as db:
                # Forecast snapshot at the start of each month / quarter (forecast accuracy, BRD 8)
                from app.services.forecast_snapshots import run_due
                return sales_jobs.recycle_leads(db), sales_jobs.stale_deals(db), run_due(db)
        await _run_in_thread(_hourly)
        _last_hourly = time.monotonic()
    if not _last_sync or time.monotonic() - _last_sync >= 600:
        def _sync():
            with SessionLocal() as db:
                return account_sync.sync_all(db)
        await _run_in_thread(_sync)
        _last_sync = time.monotonic()


async def contact_scoring_iteration() -> None:
    # Contact scores (BR-CM-43): full recomputes an admin asked for, then stale / unscored
    # contacts (at most JOB_CAP per pass, in batches), which also applies recency decay.
    # Both take the per-workspace named lock "score:<tenant_id>"; a workspace whose lock is
    # held is skipped this pass (counted in skipped_locked) and refreshed on the next one.
    from app.services.contact_scoring import run_scoring_job
    stats = await _run_in_thread(run_scoring_job)
    if stats.get("refreshed") or stats.get("recomputed_tenants") or stats.get("skipped_locked"):
        logger.info("Contact scoring: %s", stats)


_last_webhook_purge = 0.0


async def webhooks_iteration() -> Optional[float]:
    # Outbound webhooks (BR-SF-20): send due deliveries from the outbox with retries / backoff;
    # delivery log older than 30 days is purged hourly
    global _last_webhook_purge
    from app.services import webhooks

    sent = await _run_in_thread(webhooks.deliver_due, SessionLocal)
    if not _last_webhook_purge or time.monotonic() - _last_webhook_purge >= 3600:
        def _purge():
            with SessionLocal() as db:
                return webhooks.purge_old(db)
        await _run_in_thread(_purge)
        _last_webhook_purge = time.monotonic()
    # A full batch means more may be due: come straight back
    return 2 if sent >= webhooks.BATCH_SIZE else None


async def reply_classification_iteration() -> None:
    # Reply labels (S-09): replies of the last 30 days still unlabelled (LLM error, crash between
    # steps, stored before labelling existed); LLM spend stays under the daily budget (llm_budget)
    stats = await _run_in_thread(reply_classifier.classify_pending)
    if stats.get("pending"):
        logger.info("Reply classification: %s", stats)


async def sending_alerts_iteration() -> None:
    # Sending-pipeline alerts (S-11): copy the SES breaker state for every process, compute each
    # workspace's sending alerts and e-mail admins who asked for them (cooldown per alert)
    result = await _run_in_thread(sending_alerts.run_job)
    tenants = result.get("tenants_with_alerts") or []
    if not tenants:
        return
    with SessionLocal() as db:
        for tenant_id in tenants:
            await sending_alert_center.dispatch_alert_emails_for_tenant(
                db, tenant_id, only_types=sending_alerts.SENDING_ALERT_TYPES)


async def duplicate_scan_iteration() -> None:
    # Stored duplicate suggestions (C-08): rescan every workspace; dismissed pairs are never re-suggested
    await _run_in_thread(duplicate_scan.run_scan_job, SessionLocal)


async def imports_iteration() -> Optional[float]:
    # Background CRM imports (C-03, F-16): queued jobs, and RUNNING ones whose heartbeat went stale
    # (the process running them died), continue from their checkpoint in chunks; each pass works for
    # at most import_service.WORKER_BUDGET_SECONDS and comes straight back while work is left
    worker = f"{socket.gethostname()}:{os.getpid()}"
    stats = await _run_in_thread(import_service.run_background, SessionLocal, worker)
    if stats.get("claimed"):
        logger.info("Imports: %s", stats)
    return 2 if stats.get("pending") else None


def all_jobs() -> List[Job]:
    return [
        Job("scheduler", 30, scheduler_iteration),
        # Each inbox syncs about every 4 minutes (replies visible within 10, BR-DF-05)
        Job("imap_sync", 120, imap_iteration),
        Job("deliverability", 600, deliverability_iteration),
        Job("warmup", getattr(settings, "WARMUP_INTERVAL_SECONDS", 900) or 900, warmup_iteration),
        Job("crm_purge", 6 * 3600, crm_purge_iteration),
        Job("send_safety", 60, send_safety_iteration),
        Job("sales_jobs", 60, sales_jobs_iteration),
        Job("contact_scoring", 300, contact_scoring_iteration),
        Job("webhooks", 15, webhooks_iteration),
        Job("reply_classification", 300, reply_classification_iteration),
        Job("sending_alerts", 60, sending_alerts_iteration),
        Job("imports", 5, imports_iteration),
        Job("duplicate_scan", 6 * 3600, duplicate_scan_iteration),
        Job("domain_health_snapshots", 3600, domain_health_jobs.snapshot_iteration),
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def run_job_loop(job: Job) -> None:
    logger.info("Starting %s loop (%ss interval)", job.name, int(job.interval))
    standing_by = False
    while True:
        HEARTBEATS[job.name] = time.time()
        sleep_for = job.interval
        try:
            conn = await _run_in_thread(try_acquire, job.name)
            if conn is None:
                LOCK_SKIPS[job.name] = LOCK_SKIPS.get(job.name, 0) + 1
                if not standing_by:
                    logger.info("Job %s: lock held by another process; skipping this iteration", job.name)
                else:
                    logger.debug("Job %s: lock held elsewhere; skipped", job.name)
                standing_by = True
            else:
                if standing_by:
                    logger.info("Job %s: lock acquired; running here", job.name)
                standing_by = False
                try:
                    shorter = await job.run()
                    LAST_RUN[job.name] = time.time()
                    record_job_heartbeat(job.name)
                    if shorter:
                        sleep_for = shorter
                finally:
                    await _run_in_thread(release, conn, job.name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            FAILURES[job.name] = FAILURES.get(job.name, 0) + 1
            logger.error("Job %s iteration failed: %s", job.name, e, exc_info=True)
        HEARTBEATS[job.name] = time.time()
        await asyncio.sleep(sleep_for)


def start_jobs() -> List[asyncio.Task]:
    """Start every loop as an asyncio task on the running loop; returns the tasks."""
    from app.services.email_scheduler_service import email_scheduler
    # The scheduler API reports this flag (/scheduler/status) and refuses a second loop while it is set
    email_scheduler.is_running = True
    jobs = all_jobs()
    now = time.time()
    for job in jobs:
        HEARTBEATS.setdefault(job.name, now)
    return [asyncio.create_task(run_job_loop(job), name=f"job:{job.name}") for job in jobs]


async def stop_jobs(tasks: List[asyncio.Task]) -> None:
    from app.services.email_scheduler_service import email_scheduler
    email_scheduler.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def stale_jobs(now: Optional[float] = None) -> Dict[str, float]:
    """Loops whose heartbeat is older than 3x their interval: {name: age_seconds}."""
    now = now or time.time()
    stale = {}
    for job in all_jobs():
        beat = HEARTBEATS.get(job.name)
        if beat is not None and now - beat > job.stale_after:
            stale[job.name] = round(now - beat, 1)
    return stale
