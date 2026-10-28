# app/routers/email_scheduler_router.py
"""
Email Scheduler API endpoints.
For triggering and monitoring email processing.

The scheduler is a single process-wide worker that sends for every tenant, so
anything that starts, stops or runs it is restricted to PLATFORM_ADMIN.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, BackgroundTasks

from app.core.database import get_db
from app.core.auth import require_role
from app.services import platform_flags
from app.services.email_scheduler_service import EmailSchedulerService, email_scheduler, trigger_email_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["Email Scheduler"])


# Role guards, one per access level (module level, so tests can override them)
platform_admin = require_role("PLATFORM_ADMIN")
scheduler_viewer = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "PLATFORM_ADMIN")


def scheduler_service() -> EmailSchedulerService:
    """The process-wide scheduler (one per process; the worker runs its loop)."""
    return email_scheduler


@router.post("/process")
async def process_emails_now(
    background_tasks: BackgroundTasks,
    db: Any = Depends(get_db),
    current_user: Any = Depends(platform_admin),
):
    """
    Trigger immediate processing of scheduled emails.
    Runs in background to avoid request timeout.
    """
    background_tasks.add_task(trigger_email_processing)
    
    return {
        "status": "started",
        "message": "Email processing triggered in background"
    }


@router.post("/process/sync")
async def process_emails_sync(
    db: Any = Depends(get_db),
    current_user: Any = Depends(platform_admin),
    scheduler: EmailSchedulerService = Depends(scheduler_service),
):
    """
    Process scheduled emails synchronously (waits for completion).
    Use for testing or small batches.
    """
    try:
        stats = await scheduler.process_scheduled_emails(db)
        return {
            "status": "completed",
            "stats": stats
        }
    except Exception as e:
        logger.error(f"Processing error: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


@router.get("/status")
async def get_scheduler_status(
    current_user: Any = Depends(scheduler_viewer),
    scheduler: EmailSchedulerService = Depends(scheduler_service),
):
    """Get current scheduler status (the pause switch is shared by the API and the worker).
    Platform admins also see who paused it (paused_by: the flag value, an admin email)."""
    paused_by = platform_flags.get(platform_flags.SCHEDULER_PAUSED)
    out = {
        "running": paused_by is None,
        "batch_size": scheduler.batch_size
    }
    if current_user.role == "PLATFORM_ADMIN":
        out["paused_by"] = paused_by
    return out


@router.post("/start")
async def start_scheduler(
    current_user: Any = Depends(platform_admin),
):
    """Resume sending. The worker's scheduler loop picks this up on its next cycle."""
    if not platform_flags.scheduler_paused():
        return {"status": "already_running", "message": "Scheduler is already running"}
    platform_flags.set_flag(platform_flags.SCHEDULER_PAUSED, None)
    return {"status": "started"}


@router.post("/stop")
async def stop_scheduler(
    current_user: Any = Depends(platform_admin),
):
    """Pause sending for every tenant until /scheduler/start."""
    if platform_flags.scheduler_paused():
        return {"status": "not_running", "message": "Scheduler is already paused"}
    platform_flags.set_flag(platform_flags.SCHEDULER_PAUSED, current_user.email or current_user.user_id)
    return {"status": "stopped", "message": "Scheduler paused"}
