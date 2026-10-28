"""
SQL for the alert centre (dashboard alerts and alert e-mails, ADR 0009): a tenant's alert preferences,
open reputation alerts, mailboxes and paused campaigns, the admins who get alert e-mails, and the
per-recipient e-mail cooldown records. Every call names the tenant. Never commits: the alert centre
service owns the unit of work.
"""

from __future__ import annotations

from typing import List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.models.alert_center import AlertEmailDispatch, AlertPreference
from app.models.campaign import Campaign
from app.models.domain_reputation import ReputationAlert
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork

__all__ = ["AlertCenterRepository", "AlertCenterUnitOfWork", "AlertEmailDispatch", "AlertPreference", "Session"]

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")


class AlertCenterRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def preferences(self, tenant_id: str) -> List[AlertPreference]:
        return list(self._session.query(AlertPreference).filter(AlertPreference.tenant_id == tenant_id).all())

    def preference(self, tenant_id: str, scope_type: str, scope_id: Optional[str],
                   alert_type: str) -> Optional[AlertPreference]:
        return self._session.query(AlertPreference).filter(
            AlertPreference.tenant_id == tenant_id, AlertPreference.scope_type == scope_type,
            AlertPreference.scope_id == scope_id, AlertPreference.alert_type == alert_type).first()

    def open_reputation_alerts(self, tenant_id: str) -> List[ReputationAlert]:
        return list(self._session.query(ReputationAlert).filter(
            ReputationAlert.tenant_id == tenant_id, ReputationAlert.is_resolved.is_(False),
        ).order_by(ReputationAlert.created_at.desc()).all())

    def inboxes(self, tenant_id: str, *, ordered: bool = False) -> List[SendingInbox]:
        q = self._session.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id)
        return list((q.order_by(SendingInbox.email_address) if ordered else q).all())

    def inbox_ids(self, tenant_id: str) -> Set[str]:
        return {i for (i,) in self._session.query(SendingInbox.inbox_id).filter(SendingInbox.tenant_id == tenant_id)}

    def paused_campaigns(self, tenant_id: str) -> List[Campaign]:
        return list(self._session.query(Campaign).filter(
            Campaign.tenant_id == tenant_id, Campaign.status == "PAUSED").all())

    def admin_recipients(self, tenant_id: str) -> List[Tuple[str, Optional[str]]]:
        """(user id, e-mail) of the workspace's active admins."""
        return [(uid, email) for uid, email in self._session.query(User.user_id, User.email).filter(
            User.tenant_id == tenant_id, User.status == "ACTIVE", User.role.in_(list(ADMIN_ROLES))).all()]

    def dispatch(self, tenant_id: str, alert_key: str, recipient_email: str) -> Optional[AlertEmailDispatch]:
        return self._session.query(AlertEmailDispatch).filter(
            AlertEmailDispatch.tenant_id == tenant_id, AlertEmailDispatch.alert_key == alert_key,
            AlertEmailDispatch.recipient_email == recipient_email).first()

    def add(self, entity: object) -> None:
        self._session.add(entity)

    def alerting_tenant_ids(self) -> Set[str]:
        """Tenants with a mailbox or a campaign (the ones that can have alerts)."""
        ids = {t for (t,) in self._session.query(SendingInbox.tenant_id)
               .filter(SendingInbox.tenant_id.isnot(None)).distinct().all()}
        ids.update(t for (t,) in self._session.query(Campaign.tenant_id)
                   .filter(Campaign.tenant_id.isnot(None)).distinct().all())
        return ids


class AlertCenterUnitOfWork(SqlAlchemyUnitOfWork):
    def __enter__(self) -> "AlertCenterUnitOfWork":
        super().__enter__()
        return self

    @property
    def alerts(self) -> AlertCenterRepository:
        return AlertCenterRepository(self.session)

    @classmethod
    def for_session(cls, session: Session) -> "AlertCenterUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
