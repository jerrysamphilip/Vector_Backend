# app/services/contact_activity_service.py
"""
Contact activities (BR-CM-13): notes, calls, logged emails and meetings, with @mentions
in notes (BR-CM-16). Logging a call, email or meeting moves a new lead's status on.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.models.contact_activity import ACTIVITY_TYPES, ContactActivity
from app.models.user import User
from app.schemas.contact_schema import ActivityUpdate, ActivityWrite
from app.services import contact_scoring, crm, mentions
from app.services.contact_query_service import activity_dict, get_contact, user_name
from app.services.contact_service import can_manage_contacts, clean_str

TOUCH_TYPES = ("CALL", "EMAIL", "MEETING")  # activities that count as contacting the person
ONLY_NOTES_MENTION = "You can only mention people in notes"


def utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    """Timestamps are stored as naive UTC (like func.now() on the server)."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def next_lead_status(activity_type: str, outcome: Optional[str], current: Optional[str]) -> Optional[str]:
    """The lead status a logged touch moves a new lead to; None when it stays as it is."""
    if activity_type not in TOUCH_TYPES or current not in (None, "NEW", "OPEN"):
        return None
    return "CONNECTED" if activity_type == "MEETING" or outcome == "Connected" else "ATTEMPTED_TO_CONTACT"


def _get_activity(db: Session, user: User, activity_id: str) -> ContactActivity:
    """An activity on a contact the user can see; only its author or a manager may change it."""
    activity = db.query(ContactActivity).filter(
        ContactActivity.activity_id == activity_id,
        ContactActivity.tenant_id == user.tenant_id).first()
    if not activity:
        raise NotFoundError("Activity not found")
    get_contact(db, user, activity.prospect_id)
    if activity.created_by != user.user_id and not can_manage_contacts(user):
        raise PermissionDenied("You can only change activities you logged")
    return activity


def log_activity(db: Session, user: User, prospect_id: str, payload: ActivityWrite) -> dict:
    prospect = get_contact(db, user, prospect_id)
    activity_type = payload.activity_type.upper()
    if activity_type not in ACTIVITY_TYPES:
        raise BadRequestError(f"activity_type must be one of {list(ACTIVITY_TYPES)}")
    if not clean_str(payload.body) and not clean_str(payload.subject):
        raise BadRequestError("Add a subject or some details")
    if payload.mentioned_user_ids and activity_type != "NOTE":
        raise BadRequestError(ONLY_NOTES_MENTION)
    mentioned_users: List[User] = mentions.validate(db, user, payload.mentioned_user_ids, prospect.owner_id, True)
    activity = ContactActivity(
        tenant_id=user.tenant_id,
        prospect_id=prospect.prospect_id,
        activity_type=activity_type,
        subject=clean_str(payload.subject),
        body=clean_str(payload.body),
        outcome=clean_str(payload.outcome),
        duration_minutes=payload.duration_minutes,
        occurred_at=utc_naive(payload.occurred_at) or utcnow(),
        created_by=user.user_id,
    )
    db.add(activity)
    status = next_lead_status(activity_type, payload.outcome, prospect.lead_status)
    if status:
        before = crm.snapshot(prospect, ("lead_status",))
        prospect.lead_status = status
        crm.record_changes(db, user.tenant_id, "CONTACT", prospect.prospect_id, before,
                           crm.snapshot(prospect, ("lead_status",)), user.user_id, "SYSTEM")
    if mentioned_users:
        db.flush()
        mentions.save(db, user, activity, mentioned_users, prospect.full_name or prospect.email,
                      f"/app/contacts/{prospect.prospect_id}")
    prospect.updated_at = func.now()
    db.commit()
    if activity_type in ("CALL", "MEETING"):
        contact_scoring.rescore(db, user.tenant_id, [prospect.prospect_id])  # BR-CM-43 engagement
    return activity_dict(activity, {user.user_id: user},
                         [{"user_id": u.user_id, "name": user_name(u)} for u in mentioned_users])


def update_activity(db: Session, user: User, activity_id: str, payload: ActivityUpdate) -> dict:
    activity = _get_activity(db, user, activity_id)
    data = payload.model_dump(exclude_unset=True)
    if "mentioned_user_ids" in data:
        mentioned_ids = data.pop("mentioned_user_ids") or []
        if mentioned_ids and activity.activity_type != "NOTE":
            raise BadRequestError(ONLY_NOTES_MENTION)
        prospect = get_contact(db, user, activity.prospect_id)
        mentions.save(db, user, activity,
                      mentions.validate(db, user, mentioned_ids, prospect.owner_id, True),
                      prospect.full_name or prospect.email, f"/app/contacts/{prospect.prospect_id}", replace=True)
    for field, value in data.items():
        if field == "occurred_at":
            if value is None:
                continue
            value = utc_naive(value)
        setattr(activity, field, clean_str(value) if isinstance(value, str) else value)
    db.commit()
    return activity_dict(activity, {user.user_id: user},
                         mentions.for_activities(db, [activity.activity_id]).get(activity.activity_id))


def delete_activity(db: Session, user: User, activity_id: str) -> None:
    activity = _get_activity(db, user, activity_id)
    db.delete(activity)
    db.commit()
