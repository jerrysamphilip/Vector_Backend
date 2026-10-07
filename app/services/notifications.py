"""
In-app and email notifications (BRD v2.0 BR-SF-07).

notify() records the in-app notification straight away; a background loop
(email_pending) emails the ones whose user has email notifications on, so a
slow or failing mail send never blocks the action that raised it.
"""
import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.sales_extra import Notification
from app.models.user import User

logger = logging.getLogger(__name__)


def notify(db: Session, tenant_id: str, user_ids: Iterable[Optional[str]], kind: str, title: str,
           body: Optional[str] = None, link: Optional[str] = None, dedupe_key: Optional[str] = None,
           dedupe_days: int = 7) -> int:
    """Notify each user once. With dedupe_key, skip users already told the same thing recently. Caller commits."""
    sent = 0
    since = datetime.utcnow() - timedelta(days=dedupe_days)
    for uid in dict.fromkeys(u for u in user_ids if u):
        if dedupe_key and db.query(Notification.notification_id).filter(
                Notification.user_id == uid, Notification.dedupe_key == dedupe_key,
                Notification.created_at >= since).first():
            continue
        db.add(Notification(tenant_id=tenant_id, user_id=uid, kind=kind, title=title[:255], body=body,
                            link=link, dedupe_key=dedupe_key))
        sent += 1
    return sent


def manager_of(db: Session, user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    return db.query(User.manager_id).filter(User.user_id == user_id).scalar()


def as_dict(n: Notification) -> dict:
    return {"notification_id": n.notification_id, "kind": n.kind, "title": n.title, "body": n.body,
            "link": n.link, "read": n.read_at is not None, "created_at": n.created_at}


async def email_pending(db: Session, limit: int = 100) -> int:
    """Email unsent notifications to users who want email. Marks them done either way."""
    from app.services.email_sender_service import email_sender
    rows = db.query(Notification, User).join(User, User.user_id == Notification.user_id).filter(
        Notification.emailed_at.is_(None), Notification.created_at >= datetime.utcnow() - timedelta(days=1)
    ).order_by(Notification.created_at).limit(limit).all()
    sent = 0
    base = (settings.BASE_URL or "").rstrip("/")
    for n, user in rows:
        n.emailed_at = datetime.utcnow()
        if not user.notify_email or user.status != "ACTIVE" or not settings.SENDER_EMAIL:
            continue
        body = (n.body or "") + (f"\n\nOpen: {base}{n.link}" if n.link else "") + \
            "\n\nYou get these because email notifications are on in Vector."
        try:
            result = await email_sender.send_plain_email(user.email, f"[Vector] {n.title}", body,
                                                         from_email_address=settings.SENDER_EMAIL,
                                                         sender_name="Vector")
            sent += 1 if result.get("success") else 0
        except Exception as exc:  # mail problems never break the loop
            logger.warning(f"[Notify] Email to {user.email} failed: {exc}")
    db.commit()
    return sent
