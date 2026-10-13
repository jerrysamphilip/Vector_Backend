"""
@mentions in notes (BR-CM-16, CM-033).

The client sends the ids of the colleagues picked in the note composer (the note text keeps
"@First Last"). Any active user of the workspace can be mentioned. Each newly mentioned user
except the author gets an in-app notification linking to the record; the notification loop
emails it too when that user has email notifications on (app/services/notifications.py).
"""
from typing import Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.contact_activity import ContactActivity
from app.models.crm import NoteMention
from app.models.user import User
from app.services.notifications import notify

MAX_MENTIONS = 20


def _name(u: Optional[User]) -> str:
    if not u:
        return ""
    return f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email


def mentionable_users(db: Session, user: User, q: Optional[str] = None, limit: int = 20,
                      owner_id: Optional[str] = None, check_owner: bool = False) -> List[dict]:
    """Active colleagues matching q. With check_owner, only those who can see a record owned by
    owner_id (the same rule validate() applies when the note is saved)."""
    query = db.query(User).filter(User.tenant_id == user.tenant_id, User.status == "ACTIVE",
                                  User.role.in_(("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")))
    if q and q.strip():
        term = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(func.lower(func.concat(func.coalesce(User.first_name, ""), " ",
                                                        func.coalesce(User.last_name, ""))).like(term),
                                 func.lower(User.email).like(term)))
    query = query.order_by(User.first_name, User.last_name)
    if check_owner:
        from app.services.contact_service import can_see_owner
        users = [u for u in query.limit(500).all() if can_see_owner(db, u, owner_id)][:limit]
    else:
        users = query.limit(limit).all()
    return [{"user_id": u.user_id, "name": _name(u), "email": u.email} for u in users]


def validate(db: Session, user: User, user_ids: Optional[Iterable[str]], owner_id: Optional[str] = None,
             check_owner: bool = False) -> List[User]:
    """The mentioned users, each an active user of the author's workspace (400 otherwise).
    With check_owner, each must also be able to see a record owned by owner_id: a notification
    linking to a record the person cannot open would only lead to a 404."""
    ids = list(dict.fromkeys(i for i in (user_ids or []) if i))
    if not ids:
        return []
    if len(ids) > MAX_MENTIONS:
        raise HTTPException(status_code=400, detail=f"Mention at most {MAX_MENTIONS} people in one note")
    users = db.query(User).filter(User.user_id.in_(ids), User.tenant_id == user.tenant_id,
                                  User.status == "ACTIVE",
                                  User.role.in_(("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"))).all()
    if len(users) != len(ids):
        raise HTTPException(status_code=400, detail="You can only mention active users in your workspace")
    if check_owner:
        from app.services.contact_service import can_see_owner
        for u in users:
            if not can_see_owner(db, u, owner_id):
                raise HTTPException(status_code=400, detail=f"{_name(u)} cannot see this record, so cannot be mentioned")
    by_id = {u.user_id: u for u in users}
    return [by_id[i] for i in ids]


def save(db: Session, author: User, activity: ContactActivity, users: List[User], record_name: str,
         link: str, replace: bool = False) -> int:
    """Store the mentions and notify the newly mentioned users (never the author). With replace,
    mentions no longer in `users` are removed. Caller commits. Returns notifications created."""
    existing = {m.user_id: m for m in db.query(NoteMention).filter(NoteMention.activity_id == activity.activity_id)}
    wanted = {u.user_id for u in users}
    if replace:
        for uid, m in existing.items():
            if uid not in wanted:
                db.delete(m)
    new = [u for u in users if u.user_id not in existing]
    for u in new:
        db.add(NoteMention(tenant_id=author.tenant_id, activity_id=activity.activity_id, user_id=u.user_id,
                           created_by=author.user_id))
    recipients = [u.user_id for u in new if u.user_id != author.user_id]
    if not recipients:
        return 0
    snippet = (activity.body or activity.subject or "").strip()
    if len(snippet) > 280:
        snippet = snippet[:277] + "..."
    return notify(db, author.tenant_id, recipients, "MENTION",
                  f"{_name(author)} mentioned you in a note on {record_name}", snippet or None, link)


def for_activities(db: Session, activity_ids: Iterable[str]) -> Dict[str, List[dict]]:
    """{activity_id: [{user_id, name}]} for timelines."""
    ids = [i for i in activity_ids if i]
    if not ids:
        return {}
    out: Dict[str, List[dict]] = {}
    for m, u in db.query(NoteMention, User).join(User, User.user_id == NoteMention.user_id).filter(
            NoteMention.activity_id.in_(ids)).order_by(NoteMention.created_at):
        out.setdefault(m.activity_id, []).append({"user_id": u.user_id, "name": _name(u)})
    return out
