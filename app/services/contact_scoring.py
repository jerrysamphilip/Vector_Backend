# app/services/contact_scoring.py
"""
Contact scoring on fit and engagement (BR-CM-43, CM-083): loading, storing and scheduling.

The rules themselves are in app/services/scoring_engine.py (pure). This module:
  - keeps each workspace's scoring model (seeded with the defaults on first use);
  - gathers a batch of contacts' properties and activity and scores them;
  - stores score / fit / engagement / grade / scored_at on prospects, without touching
    updated_at, and writes a property history row (field "score_grade", source SYSTEM)
    when an existing grade changes;
  - recomputes a whole workspace in batches (POST /contacts/scoring-model/recompute) and
    refreshes stale scores from the worker (app/jobs.py "contact_scoring" loop).
Contacts are also rescored right away when they are created or edited, and when a call,
meeting or completed task is logged against them; email opens, clicks and replies are picked
up by the worker (scores older than STALE_AFTER_HOURS are refreshed, which also applies decay).
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from app.core.clock import utcnow
from app.core.errors import BadRequestError, NotFoundError
from app.models.contact_scoring import ContactScoringModel
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.contact_scoring import ScoringRepository, Session, scoring_repo
from app.services import scoring_engine as engine
from app.services.contact_query_service import get_contact

logger = logging.getLogger(__name__)

BATCH_SIZE = 500            # contacts scored per transaction
JOB_CAP = 5000              # contacts refreshed per worker pass
TENANT_CAP = 200_000        # contacts per full workspace recompute
STALE_AFTER_HOURS = 24      # the worker rescores contacts last scored before this
OOO_SCAN_CHARS = 500        # subject / body characters checked for out-of-office markers
REPLY_SCAN_CAP = 50_000     # inbound messages read per scoring batch


def _now() -> datetime:
    return utcnow().replace(microsecond=0)


def _repo(db: Session) -> ScoringRepository:
    return scoring_repo(db)


# ── Model ─────────────────────────────────────────────────────────

def get_model_row(db: Session, tenant_id: str) -> ContactScoringModel:
    """The workspace's scoring model row, created with the default rules on first use."""
    repo = _repo(db)
    row = repo.model_row(tenant_id)
    if row is not None:
        return row
    seeded = ContactScoringModel(tenant_id=tenant_id, model=engine.default_model())
    if repo.seed_model_row(seeded):
        return seeded
    found = repo.model_row(tenant_id)  # another request seeded it first
    if found is None:
        raise NotFoundError("Scoring model not found")
    return found


def get_model(db: Session, tenant_id: str) -> Dict[str, Any]:
    return engine.load_model(get_model_row(db, tenant_id).model)


def save_model(db: Session, tenant_id: str, raw: Dict[str, Any], user_id: Optional[str]) -> ContactScoringModel:
    """Validate and store a new model (raises engine.ScoringModelError); asks for a recompute."""
    model = engine.validate_model(raw)
    row = get_model_row(db, tenant_id)
    row.model = model
    row.updated_by = user_id
    row.recompute_requested_at = _now()
    db.commit()
    return row


def request_recompute(db: Session, tenant_id: str) -> ContactScoringModel:
    row = get_model_row(db, tenant_id)
    row.recompute_requested_at = _now()
    db.commit()
    return row


def model_payload(db: Session, row: ContactScoringModel) -> Dict[str, Any]:
    total, scored, grades = _repo(db).contact_stats(row.tenant_id)
    return {
        "model": engine.load_model(row.model),
        "defaults": engine.default_model(),
        "fields": [{"key": k, "label": v[0], "kind": v[1]} for k, v in engine.FIT_FIELDS.items()],
        "operators": [{"key": k, "label": v, "text": k in engine.TEXT_OPERATORS, "number": k in engine.NUMBER_OPERATORS,
                       "needs_value": k not in engine.NO_VALUE_OPERATORS} for k, v in engine.OPERATORS.items()],
        "signals": [{"key": k, "label": v} for k, v in engine.ENGAGEMENT_SIGNALS.items()],
        "limits": {"max_rules": engine.MAX_RULES, "points_min": engine.POINTS_RANGE[0],
                   "points_max": engine.POINTS_RANGE[1]},
        "recompute_requested_at": row.recompute_requested_at,
        "last_recomputed_at": row.last_recomputed_at,
        "last_recompute_count": row.last_recompute_count,
        "updated_at": row.updated_at,
        "stats": {"contacts": total, "scored": scored, "grades": {g: grades.get(g, 0) for g in engine.GRADES}},
    }


# ── Inputs ────────────────────────────────────────────────────────

def _email_type(email: Optional[str], stored: Optional[str]) -> Optional[str]:
    if stored:
        return stored.upper()
    if not email:
        return None
    from app.utils.email_utils import parse_email
    # Same classification as when a contact is created (contacts_router._email_meta)
    info = parse_email(email)
    return info.get("email_type") if info else "PERSONAL"


def _contact_inputs(db: Session, rows: List[Any]) -> Dict[str, Dict[str, Any]]:
    accounts = _repo(db).account_rows({r.account_id for r in rows if r.account_id})
    contacts = {}
    for r in rows:
        acc = accounts.get(r.account_id)
        emp_band = r.emp_band or (acc.emp_band if acc else None)
        contacts[r.prospect_id] = {
            "designation": r.designation,
            "industry": r.industry or (acc.industry if acc else None),
            "emp_band": emp_band,
            "employees": engine.employees_from_band(emp_band),
            "annual_revenue": float(acc.annual_revenue) if acc and acc.annual_revenue is not None else None,
            "country": r.poc_country or (acc.country if acc else None),
            "lifecycle_stage": r.lifecycle_stage,
            "lead_status": r.lead_status,
            "lead_source": r.lead_source,
            "email_type": _email_type(r.email, r.email_type),
            "tags": r.tags if isinstance(r.tags, list) else [],
            "has_phone": bool(r.phone or r.mobile_phone),
        }
    return contacts


def _activity_inputs(db: Session, ids: List[str], since: datetime) -> Dict[str, Dict[str, list]]:
    """Per contact and signal, [(datetime, count), ...] since `since` (grouped to keep rows few)."""
    activity: Dict[str, Dict[str, list]] = {pid: defaultdict(list) for pid in ids}
    if not ids:
        return activity
    repo = _repo(db)
    # One open / click / positive reply per email (the first one), however often it repeats
    for pid, signal, at in repo.first_email_events(ids, since):
        activity[pid][signal].append((at, 1))
    # Replies: emails received from the contact, except out-of-office / vacation auto-replies
    # (same subject/body markers the IMAP sync uses; the Auto-Submitted header is not stored)
    replies: Dict[tuple, int] = defaultdict(int)
    for pid, sent_at, subject, body in repo.inbound_replies(ids, since, OOO_SCAN_CHARS, REPLY_SCAN_CAP):
        if sent_at is None or is_auto_reply(subject, body):
            continue
        replies[(pid, sent_at.date())] += 1
    for (pid, day), n in replies.items():
        activity[pid]["email_replied"].append((day, n))
    # Meetings and calls logged against the contact
    for pid, signal, day, n in repo.logged_activities(ids, since):
        activity[pid][signal].append((day, n))
    # Tasks on the contact marked done
    for pid, day, n in repo.completed_tasks(ids, since):
        activity[pid]["task_completed"].append((day, n))
    return activity


def is_auto_reply(subject: Optional[str], body: Optional[str]) -> bool:
    """An out-of-office / vacation auto-response (not real engagement)."""
    from app.utils.reply_classifier import is_out_of_office
    return is_out_of_office(subject or "", body or "")


def grade_history_needed(stored_grade: Optional[str], new_grade: Optional[str], latest_logged: Optional[str]) -> bool:
    """Write a history row only when an existing grade changes and the latest row does not
    already record this same new grade (two overlapping runs must not log it twice)."""
    return bool(stored_grade) and stored_grade != new_grade and latest_logged != new_grade


def _load_rows(db: Session, tenant_id: str, ids: Iterable[str]) -> List[Any]:
    return _repo(db).contact_rows(tenant_id, list(dict.fromkeys(i for i in ids if i)))


# ── Scoring ───────────────────────────────────────────────────────

def evaluate(db: Session, tenant_id: str, ids: Iterable[str], model: Optional[Dict[str, Any]] = None,
             now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Live scores with breakdowns for these contacts (nothing is stored)."""
    model = model or get_model(db, tenant_id)
    now = now or _now()
    rows = _load_rows(db, tenant_id, ids)
    contacts = _contact_inputs(db, rows)
    activity = _activity_inputs(db, [r.prospect_id for r in rows], now - timedelta(days=model["lookback_days"]))
    return {r.prospect_id: engine.compute_score(model, contacts[r.prospect_id], activity[r.prospect_id], now)
            for r in rows}


def score_contacts(db: Session, tenant_id: str, ids: Iterable[str], model: Optional[Dict[str, Any]] = None,
                   now: Optional[datetime] = None, changed_by: Optional[str] = None) -> int:
    """Score and store these contacts (one batch, at most BATCH_SIZE ids). Does not commit."""
    model = model or get_model(db, tenant_id)
    now = now or _now()
    rows = _load_rows(db, tenant_id, list(ids)[:BATCH_SIZE])
    if not rows:
        return 0
    repo = _repo(db)
    contacts = _contact_inputs(db, rows)
    activity = _activity_inputs(db, [r.prospect_id for r in rows], now - timedelta(days=model["lookback_days"]))
    results = {r.prospect_id: engine.compute_score(model, contacts[r.prospect_id], activity[r.prospect_id], now)
               for r in rows}
    changed = [r.prospect_id for r in rows if r.score_grade and r.score_grade != results[r.prospect_id]["grade"]]
    latest = repo.latest_grades(tenant_id, changed)
    for r in rows:
        result = results[r.prospect_id]
        repo.store_score(r.prospect_id, result, now)
        if grade_history_needed(r.score_grade, result["grade"], latest.get(r.prospect_id)):
            repo.add(PropertyChange(tenant_id=tenant_id, object_type="CONTACT", object_id=r.prospect_id,
                                    field="score_grade", old_value=r.score_grade, new_value=result["grade"],
                                    source="SYSTEM", changed_by=changed_by))
    return len(rows)


def rescore(db: Session, tenant_id: str, ids: Iterable[str], changed_by: Optional[str] = None) -> int:
    """Rescore a few contacts now and commit; never raises (the triggering change is already saved)."""
    try:
        n = score_contacts(db, tenant_id, ids, changed_by=changed_by)
        db.commit()
        return n
    except Exception as e:  # pragma: no cover - defensive; the worker catches up
        db.rollback()
        logger.warning("Contact rescoring failed (%s); the scoring job will retry", type(e).__name__)
        return 0


# ── Batches ───────────────────────────────────────────────────────

def _try_lock(name: str) -> Any:
    try:
        from app.jobs import try_acquire
        return try_acquire(name)
    except Exception as e:  # no lock available (e.g. database unreachable): run unlocked
        logger.warning("Scoring lock %s unavailable (%s)", name, type(e).__name__)
        return "unlocked"


def _release(conn, name: str) -> None:
    if conn and conn != "unlocked":
        from app.jobs import release
        release(conn, name)


def recompute_tenant(tenant_id: str) -> Optional[int]:
    """Rescore every live contact of a workspace in batches. None when another run holds the lock."""
    from app.core.database import SessionLocal
    lock = f"score:{tenant_id}"
    conn = _try_lock(lock)
    if conn is None:
        return None
    try:
        with SessionLocal() as db:
            started = _now()
            model = get_model(db, tenant_id)
            done, last_id = 0, ""
            while done < TENANT_CAP:
                ids = _repo(db).live_ids_after(tenant_id, last_id, BATCH_SIZE)
                if not ids:
                    break
                done += score_contacts(db, tenant_id, ids, model=model, now=started)
                db.commit()
                last_id = ids[-1]
            row = get_model_row(db, tenant_id)
            if row.recompute_requested_at is not None and row.recompute_requested_at <= started:
                row.recompute_requested_at = None
            row.last_recomputed_at = _now()
            row.last_recompute_count = done
            db.commit()
            logger.info("Contact scoring: rescored %s contact(s) in workspace %s", done, tenant_id)
            return done
    finally:
        _release(conn, lock)


def run_scoring_job(cap: int = JOB_CAP, tenant_id: Optional[str] = None) -> Dict[str, int]:
    """Worker pass: full recomputes that were asked for, then up to `cap` stale or unscored contacts.
    `tenant_id` limits the pass to one workspace (tests, manual runs)."""
    from app.core.database import SessionLocal
    stats = {"recomputed_tenants": 0, "refreshed": 0}
    with SessionLocal() as db:
        pending = _repo(db).recompute_requested(tenant_id, 20)
    for pending_tenant in pending:
        if recompute_tenant(pending_tenant) is not None:
            stats["recomputed_tenants"] += 1

    with SessionLocal() as db:
        cutoff = _now() - timedelta(hours=STALE_AFTER_HOURS)
        by_tenant: Dict[str, List[str]] = defaultdict(list)
        for stale_tenant, pid in _repo(db).stale_contacts(cutoff, tenant_id, cap):
            by_tenant[stale_tenant].append(pid)
        for stale_tenant, ids in by_tenant.items():
            # Same per-workspace lock as recompute_tenant: a sweep never overlaps a full recompute
            # (or another worker's sweep) of the workspace; it is picked up on the next pass instead.
            lock = f"score:{stale_tenant}"
            conn = _try_lock(lock)
            if conn is None:
                stats["skipped_locked"] = stats.get("skipped_locked", 0) + 1
                continue
            try:
                model = get_model(db, stale_tenant)
                now = _now()
                for i in range(0, len(ids), BATCH_SIZE):
                    stats["refreshed"] += score_contacts(db, stale_tenant, ids[i:i + BATCH_SIZE], model=model, now=now)
                    db.commit()
            finally:
                _release(conn, lock)
    return stats


# ── Routes ────────────────────────────────────────────────────────

class ContactScoringService:
    """The scoring routes (GET/PUT /contacts/scoring-model, scores of one contact). The background
    recompute after a model change is scheduled by the router (FastAPI BackgroundTasks)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    @classmethod
    def for_session(cls, db: Session) -> "ContactScoringService":
        return cls(db)

    def model(self, user: User) -> Dict[str, Any]:
        return model_payload(self.db, get_model_row(self.db, user.tenant_id))

    def save_model(self, user: User, raw: Dict[str, Any]) -> Dict[str, Any]:
        try:
            row = save_model(self.db, user.tenant_id, raw, user.user_id)
        except engine.ScoringModelError as exc:
            raise BadRequestError(str(exc)) from exc
        return model_payload(self.db, row)

    def request_recompute(self, user: User) -> Dict[str, Any]:
        row = request_recompute(self.db, user.tenant_id)
        return {"status": "queued", "contacts": _repo(self.db).count_live(user.tenant_id),
                "requested_at": row.recompute_requested_at}

    def _score_response(self, user: User, prospect: Prospect) -> Dict[str, Any]:
        model = get_model(self.db, user.tenant_id)
        live = evaluate(self.db, user.tenant_id, [prospect.prospect_id], model=model).get(prospect.prospect_id)
        if live is None:
            raise NotFoundError("Contact not found")
        return {
            "prospect_id": prospect.prospect_id,
            "stored": {"score": prospect.score, "fit": prospect.fit_score, "engagement": prospect.engagement_score,
                       "grade": prospect.score_grade, "scored_at": prospect.scored_at},
            **live,
            "grade_thresholds": model["grade_thresholds"],
            "half_life_days": model["half_life_days"],
            "lookback_days": model["lookback_days"],
        }

    def contact_score(self, user: User, prospect_id: str) -> Dict[str, Any]:
        """The stored score and a live breakdown (anyone who can see the contact)."""
        return self._score_response(user, get_contact(self.db, user, prospect_id))

    def recompute_contact(self, user: User, prospect_id: str) -> Dict[str, Any]:
        prospect = get_contact(self.db, user, prospect_id)
        score_contacts(self.db, user.tenant_id, [prospect.prospect_id])
        self.db.commit()
        self.db.refresh(prospect)
        return self._score_response(user, prospect)
