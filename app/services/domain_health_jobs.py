"""
Hourly domain-health snapshots with 90-day retention (team review C-10, BR-DF-07 trending).

``run_snapshot_job`` writes one DomainHealthSnapshot per tenant-owned sending domain (score and 24 h
bounce / complaint rates as last computed by send_safety's hourly health cycle) unless the domain
already has one from the last ``SNAPSHOT_INTERVAL`` (a worker restart never doubles an hour), then
deletes snapshots older than ``SNAPSHOT_RETENTION_DAYS``. One short transaction; no external calls.

Registered in app/jobs.py as ``Job("domain_health_snapshots", 3600, domain_health_jobs.snapshot_iteration)``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from app.core.clock import utcnow
from app.core.database import SessionLocal
from app.domain.deliverability import SNAPSHOT_INTERVAL, SNAPSHOT_RETENTION_DAYS
from app.repositories.deliverability import DeliverabilityUnitOfWork
from app.services.deliverability_service import DeliverabilityService

logger = logging.getLogger(__name__)


def take_snapshots(uow: Any, now: Optional[datetime] = None) -> Dict[str, int]:
    """Snapshot every tenant-owned domain not snapshotted in the last interval, then apply the retention.
    Returns {"snapshots": written, "purged": deleted}. Commits."""
    now = now or utcnow()
    with uow:
        repo = uow.domains
        recent = repo.snapshotted_since(now - SNAPSHOT_INTERVAL)
        due = [d for d in repo.owned_domains() if d.domain_id not in recent]
        written = DeliverabilityService.add_snapshots(repo, due)
        purged = repo.delete_snapshots_before(now - timedelta(days=SNAPSHOT_RETENTION_DAYS))
        uow.commit()
    return {"snapshots": written, "purged": purged}


def run_snapshot_job(session_factory: Callable[[], Any] = SessionLocal) -> Dict[str, int]:
    """The blocking job body on its own session."""
    return take_snapshots(DeliverabilityUnitOfWork(session_factory))


async def snapshot_iteration() -> None:
    """One iteration of the hourly ``domain_health_snapshots`` loop (app/jobs.py)."""
    result = await asyncio.get_running_loop().run_in_executor(None, run_snapshot_job)
    if result.get("snapshots") or result.get("purged"):
        logger.info("Domain health snapshots: %s", result)
