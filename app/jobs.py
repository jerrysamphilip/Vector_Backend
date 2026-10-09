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
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from sqlalchemy import create_engine, pool, text

import app.models  # noqa: F401  (resolve SQLAlchemy relationships before any query)
from app.core.config import settings
from app.core.observability import record_job_heartbeat
from app.core.database import SessionLocal

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
    return 2 if stats.get("processed") else None


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
            for row in db.execute(text("SELECT domain_name FROM sending_domains")).fetchall():
                deliverability_service.perform_dns_scan(row[0], db)

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
    # Notification emails every minute; lead recycling and stale-deal alerts hourly
    # (BR-SF-02, 05, 07); calendar / email sync every 10 minutes (BR-SF-16)
    global _last_hourly, _last_sync
    from app.services import account_sync, sales_jobs
    from app.services.notifications import email_pending

    with SessionLocal() as db:
        await email_pending(db)
    if not _last_hourly or time.monotonic() - _last_hourly >= 3600:
        def _hourly():
            with SessionLocal() as db:
                return sales_jobs.recycle_leads(db), sales_jobs.stale_deals(db)
        await _run_in_thread(_hourly)
        _last_hourly = time.monotonic()
    if not _last_sync or time.monotonic() - _last_sync >= 600:
        def _sync():
            with SessionLocal() as db:
                return account_sync.sync_all(db)
        await _run_in_thread(_sync)
        _last_sync = time.monotonic()


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
