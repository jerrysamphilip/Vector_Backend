# app/routers/email_scheduler_router.py
"""
Email Scheduler API endpoints.
For triggering and monitoring email processing.

The scheduler is a single process-wide worker that sends for every tenant, so
anything that starts, stops or runs it is restricted to PLATFORM_ADMIN.
"""

import logging
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_role
from app.models.user import User
from app.services import platform_flags
from app.services.email_scheduler_service import email_scheduler, trigger_email_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["Email Scheduler"])


@router.post("/process")
async def process_emails_now(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
):
    """
    Process scheduled emails synchronously (waits for completion).
    Use for testing or small batches.
    """
    try:
        stats = await email_scheduler.process_scheduled_emails(db)
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
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "PLATFORM_ADMIN")),
):
    """Get current scheduler status (the pause switch is shared by the API and the worker)."""
    return {
        "running": not platform_flags.scheduler_paused(),
        "batch_size": email_scheduler.batch_size
    }


@router.post("/start")
async def start_scheduler(
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
):
    """Resume sending. The worker's scheduler loop picks this up on its next cycle."""
    if not platform_flags.scheduler_paused():
        return {"status": "already_running", "message": "Scheduler is already running"}
    platform_flags.set_flag(platform_flags.SCHEDULER_PAUSED, None)
    return {"status": "started"}


@router.post("/stop")
async def stop_scheduler(
    current_user: User = Depends(require_role("PLATFORM_ADMIN")),
):
    """Pause sending for every tenant until /scheduler/start."""
    if platform_flags.scheduler_paused():
        return {"status": "not_running", "message": "Scheduler is already paused"}
    platform_flags.set_flag(platform_flags.SCHEDULER_PAUSED, current_user.email or current_user.user_id)
    return {"status": "stopped", "message": "Scheduler paused"}
