"""
Duplicate scan (BR-CM-32; gap list C-08 / C-09 / F-21): match a workspace's live contacts with the rules in
app/domain/duplicates.py and keep the result in ``duplicate_pairs``.

* a new pair is stored OPEN with its score, confidence and reasons;
* an OPEN pair found again is refreshed; an OPEN pair no longer found (a contact changed or was deleted)
  is removed;
* a DISMISSED pair ("not the same person") is never suggested again, and a MERGED one stays as history.

Used by the on-demand scan (``DuplicateService.scan``), by GET /contacts/duplicates for a workspace that
was never scanned, and by the background job (``run_scan_job``). The caller commits.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Optional

from app.core.clock import utcnow
from app.domain.duplicates import PairMatch, find_pairs
from app.models.contact_identity import DuplicatePair
from app.repositories.duplicates import DuplicateRepository, SqlAlchemyDuplicateRepository
from app.utils.free_mail import is_free_mail_domain

__all__ = ["pair_group", "run_scan_job", "scan_tenant"]

logger = logging.getLogger(__name__)


def _apply(store: DuplicateRepository, tenant_id: str, found: list[PairMatch], now: datetime) -> dict[str, int]:
    existing = {(p.contact_a_id, p.contact_b_id): p for p in store.pairs(tenant_id)}
    stats = {"new": 0, "updated": 0, "removed": 0, "kept_dismissed": 0}
    for match in found:
        row = existing.get((match.contact_a, match.contact_b))
        if row is None:
            store.add_pair(DuplicatePair(tenant_id=tenant_id, contact_a_id=match.contact_a,
                                         contact_b_id=match.contact_b, score=match.score,
                                         confidence=match.confidence, reasons=match.reasons, status="OPEN",
                                         last_seen_at=now))
            stats["new"] += 1
        elif row.status == "OPEN":
            row.score, row.confidence, row.reasons, row.last_seen_at = (
                match.score, match.confidence, match.reasons, now)
            stats["updated"] += 1
        elif row.status == "DISMISSED":
            stats["kept_dismissed"] += 1
    seen = {(m.contact_a, m.contact_b) for m in found}
    stale = [row.pair_id for key, row in existing.items() if row.status == "OPEN" and key not in seen]
    stats["removed"] = store.delete_pairs(stale) if stale else 0
    return stats


def scan_tenant(store: DuplicateRepository, tenant_id: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Scan one workspace and store the pairs; returns counts for the UI / logs."""
    started = time.perf_counter()
    candidates = store.candidates(tenant_id)
    found = find_pairs(candidates, is_free_mail_domain)
    stats: dict[str, Any] = dict(_apply(store, tenant_id, found, now or utcnow()))
    high = sum(1 for m in found if m.confidence == "HIGH")
    stats.update(contacts_scanned=len(candidates), pairs_found=len(found), high=high, medium=len(found) - high,
                 seconds=round(time.perf_counter() - started, 2))
    return stats


def pair_group(pair: DuplicatePair, summaries: Mapping[str, dict]) -> dict:
    """A stored pair as a "group" of GET /contacts/duplicates (older contact first)."""
    contacts = [summaries[i] for i in (pair.contact_a_id, pair.contact_b_id) if i in summaries]
    contacts.sort(key=lambda s: s.get("created_at") or datetime.min)
    return {"pair_id": pair.pair_id, "score": pair.score, "confidence": pair.confidence,
            "status": pair.status, "reasons": list(pair.reasons or []), "contacts": contacts}


def run_scan_job(session_factory: Callable[[], Any],
                 repository: Callable[[Any], DuplicateRepository] = SqlAlchemyDuplicateRepository,
                 tenant_ids: Optional[list[str]] = None) -> dict[str, int]:
    """Worker pass (app/jobs.py): rescan every workspace that has live contacts, each in its own
    transaction; one workspace failing does not stop the others."""
    totals = {"tenants": 0, "new": 0, "removed": 0, "failed": 0}
    with session_factory() as db:
        tenants = tenant_ids if tenant_ids is not None else repository(db).tenants_with_contacts()
    for tenant_id in tenants:
        with session_factory() as db:
            try:
                stats = scan_tenant(repository(db), tenant_id)
                db.commit()
            except Exception:  # one workspace's failure must not stop the others
                db.rollback()
                logger.exception("Duplicate scan failed for tenant %s", tenant_id)
                totals["failed"] += 1
                continue
        totals["tenants"] += 1
        totals["new"] += stats["new"]
        totals["removed"] += stats["removed"]
    return totals
