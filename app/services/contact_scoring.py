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

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.contact_activity import ContactActivity
from app.models.contact_scoring import ContactScoringModel
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.services import scoring_engine as engine

logger = logging.getLogger(__name__)

BATCH_SIZE = 500            # contacts scored per transaction
JOB_CAP = 5000              # contacts refreshed per worker pass
TENANT_CAP = 200_000        # contacts per full workspace recompute
STALE_AFTER_HOURS = 24      # the worker rescores contacts last scored before this

_PROSPECT_COLUMNS = (
    Prospect.prospect_id, Prospect.tenant_id, Prospect.email, Prospect.email_type, Prospect.designation,
    Prospect.industry, Prospect.emp_band, Prospect.poc_country, Prospect.lifecycle_stage, Prospect.lead_status,
    Prospect.lead_source, Prospect.tags, Prospect.phone, Prospect.mobile_phone, Prospect.account_id,
    Prospect.score, Prospect.fit_score, Prospect.engagement_score, Prospect.score_grade,
)


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


# ── Model ─────────────────────────────────────────────────────────

def get_model_row(db: Session, tenant_id: str) -> ContactScoringModel:
    """The workspace's scoring model row, created with the default rules on first use."""
    row = db.get(ContactScoringModel, tenant_id)
    if row is not None:
        return row
    row = ContactScoringModel(tenant_id=tenant_id, model=engine.default_model())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:  # another request seeded it first
        db.rollback()
        row = db.get(ContactScoringModel, tenant_id)
    return row


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
    total = db.query(func.count(Prospect.prospect_id)).filter(
        Prospect.tenant_id == row.tenant_id, Prospect.deleted_at.is_(None)).scalar() or 0
    scored = db.query(func.count(Prospect.prospect_id)).filter(
        Prospect.tenant_id == row.tenant_id, Prospect.deleted_at.is_(None), Prospect.score.isnot(None)).scalar() or 0
    grades = dict(db.query(Prospect.score_grade, func.count(Prospect.prospect_id)).filter(
        Prospect.tenant_id == row.tenant_id, Prospect.deleted_at.is_(None), Prospect.score_grade.isnot(None)
    ).group_by(Prospect.score_grade).all())
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
    account_ids = {r.account_id for r in rows if r.account_id}
    accounts = {a.account_id: a for a in db.query(
        Account.account_id, Account.emp_band, Account.annual_revenue, Account.industry, Account.country
    ).filter(Account.account_id.in_(account_ids))} if account_ids else {}
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
    event_signal = {EmailEvent.EVENT_OPEN: "email_opened", EmailEvent.EVENT_CLICK: "email_clicked",
                    EmailEvent.EVENT_POSITIVE_REPLY: "positive_reply"}
    # One open / click / positive reply per email (the first one), however often it repeats
    for pid, etype, at in db.query(EmailMessage.prospect_id, EmailEvent.event_type, func.min(EmailEvent.event_time)).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id).filter(
            EmailMessage.prospect_id.in_(ids), EmailEvent.event_type.in_(list(event_signal)),
            EmailEvent.event_time >= since).group_by(EmailMessage.prospect_id, EmailEvent.message_id, EmailEvent.event_type):
        if at is not None:
            activity[pid][event_signal[etype]].append((at, 1))
    # Replies: emails received from the contact
    for pid, day, n in db.query(EmailMessage.prospect_id, func.date(EmailMessage.sent_at), func.count()).filter(
            EmailMessage.prospect_id.in_(ids), EmailMessage.direction == "INBOUND",
            EmailMessage.sent_at >= since).group_by(EmailMessage.prospect_id, func.date(EmailMessage.sent_at)):
        activity[pid]["email_replied"].append((day, n))
    # Meetings and calls logged against the contact
    kinds = {"MEETING": "meeting", "CALL": "call"}
    for pid, kind, day, n in db.query(ContactActivity.prospect_id, ContactActivity.activity_type,
                                      func.date(ContactActivity.occurred_at), func.count()).filter(
            ContactActivity.prospect_id.in_(ids), ContactActivity.activity_type.in_(list(kinds)),
            ContactActivity.occurred_at >= since).group_by(
            ContactActivity.prospect_id, ContactActivity.activity_type, func.date(ContactActivity.occurred_at)):
        activity[pid][kinds[kind]].append((day, n))
    # Tasks on the contact marked done
    for pid, day, n in db.query(CrmTask.prospect_id, func.date(CrmTask.completed_at), func.count()).filter(
            CrmTask.prospect_id.in_(ids), CrmTask.status == "DONE", CrmTask.completed_at >= since).group_by(
            CrmTask.prospect_id, func.date(CrmTask.completed_at)):
        activity[pid]["task_completed"].append((day, n))
    return activity


def _load_rows(db: Session, tenant_id: str, ids: Iterable[str]) -> List[Any]:
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return []
    return db.query(*_PROSPECT_COLUMNS).filter(
        Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids), Prospect.deleted_at.is_(None)).all()


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
    contacts = _contact_inputs(db, rows)
    activity = _activity_inputs(db, [r.prospect_id for r in rows], now - timedelta(days=model["lookback_days"]))
    table = Prospect.__table__
    for r in rows:
        result = engine.compute_score(model, contacts[r.prospect_id], activity[r.prospect_id], now)
        # Core UPDATE that sets updated_at to itself: a score is not an edit of the contact
        db.execute(table.update().where(table.c.prospect_id == r.prospect_id).values(
            score=result["score"], fit_score=result["fit"], engagement_score=result["engagement"],
            score_grade=result["grade"], scored_at=now, updated_at=table.c.updated_at))
        if r.score_grade and r.score_grade != result["grade"]:
            db.add(PropertyChange(tenant_id=tenant_id, object_type="CONTACT", object_id=r.prospect_id,
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

def _try_lock(name: str):
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
                ids = [r[0] for r in db.query(Prospect.prospect_id).filter(
                    Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None), Prospect.prospect_id > last_id
                ).order_by(Prospect.prospect_id).limit(BATCH_SIZE)]
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
        pending_q = db.query(ContactScoringModel.tenant_id).filter(ContactScoringModel.recompute_requested_at.isnot(None))
        if tenant_id:
            pending_q = pending_q.filter(ContactScoringModel.tenant_id == tenant_id)
        pending = [r[0] for r in pending_q.limit(20)]
    for pending_tenant in pending:
        if recompute_tenant(pending_tenant) is not None:
            stats["recomputed_tenants"] += 1

    with SessionLocal() as db:
        cutoff = _now() - timedelta(hours=STALE_AFTER_HOURS)
        stale_q = db.query(Prospect.tenant_id, Prospect.prospect_id).filter(
            Prospect.deleted_at.is_(None),
            or_(Prospect.scored_at.is_(None), and_(Prospect.scored_at.isnot(None), Prospect.scored_at < cutoff)))
        if tenant_id:
            stale_q = stale_q.filter(Prospect.tenant_id == tenant_id)
        by_tenant: Dict[str, List[str]] = defaultdict(list)
        for stale_tenant, pid in stale_q.limit(cap).all():
            by_tenant[stale_tenant].append(pid)
        for stale_tenant, ids in by_tenant.items():
            model = get_model(db, stale_tenant)
            now = _now()
            for i in range(0, len(ids), BATCH_SIZE):
                stats["refreshed"] += score_contacts(db, stale_tenant, ids[i:i + BATCH_SIZE], model=model, now=now)
                db.commit()
    return stats
