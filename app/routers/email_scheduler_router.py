# app/routers/email_scheduler_router.py
"""
Email Scheduler API endpoints.
For triggering and monitoring email processing.
"""

import logging
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_role
from app.models.user import User
from app.services.email_scheduler_service import email_scheduler, trigger_email_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["Email Scheduler"])


@router.post("/process")
async def process_emails_now(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
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
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
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
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """Get current scheduler status."""
    return {
        "running": email_scheduler.is_running,
        "batch_size": email_scheduler.batch_size
    }


@router.post("/start")
async def start_scheduler(
    background_tasks: BackgroundTasks,
    interval: int = 30,
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """
    Start continuous email scheduler in background.
    
    Args:
        interval: Seconds between processing batches (default: 30)
    """
    if email_scheduler.is_running:
        return {
            "status": "already_running",
            "message": "Scheduler is already running"
        }
    
    background_tasks.add_task(email_scheduler.run_continuous, interval)
    
    return {
        "status": "started",
        "interval_seconds": interval
    }


@router.post("/stop")
async def stop_scheduler(
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN")),
):
    """Stop the continuous email scheduler."""
    if not email_scheduler.is_running:
        return {
            "status": "not_running",
            "message": "Scheduler is not running"
        }
    
    email_scheduler.stop()
    
    return {
        "status": "stopped",
        "message": "Scheduler stop requested"
    }
