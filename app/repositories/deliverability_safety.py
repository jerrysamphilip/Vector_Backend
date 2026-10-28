"""
SQL for delivery safety (BR-DF-04 / 07, ADR 0009): final-status reconciliation of sent mail, the hard
bounce / complaint counts behind auto-pause, freezing a paused campaign's queue, the people told about
an auto-pause, and the (tenant, sending domain) pairs the hourly health cycle covers. Never commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Set, Tuple

from sqlalchemy import ColumnElement, case, func
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.email_message import EmailMessage
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.repositories.deliverability import from_domain_criterion, tenant_message_criterion

__all__ = ["SafetyRepository", "Session"]

HARD_BOUNCE = EmailMessage.last_error_code == "BOUNCE_Permanent"
COMPLAINT = EmailMessage.status == "COMPLAINED"
OUTCOME_STATUSES = ("BOUNCED", "COMPLAINED", "REJECTED", "FAILED")
QUEUED_STATUSES = ("QUEUED", "SCHEDULED")


class SafetyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Final status (BR-DF-04) ───────────────────────────────────────────
    def reconcile_final_status(self, unconfirmed_before: datetime) -> int:
        """Give a final status to sent mail without one: the outcome already on the row, DELIVERED when
        a delivery was recorded, UNCONFIRMED when SES said nothing before ``unconfirmed_before``."""
        base = self._session.query(EmailMessage).filter(
            EmailMessage.final_status.is_(None), EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.isnot(None),
        )
        n = base.filter(EmailMessage.status.in_(OUTCOME_STATUSES)).update(
            {EmailMessage.final_status: EmailMessage.status, EmailMessage.final_status_at: func.utc_timestamp()},
            synchronize_session=False)
        n += base.filter(EmailMessage.delivered_at.isnot(None)).update(
            {EmailMessage.final_status: "DELIVERED", EmailMessage.final_status_at: EmailMessage.delivered_at},
            synchronize_session=False)
        n += base.filter(EmailMessage.sent_at < unconfirmed_before).update(
            {EmailMessage.final_status: "UNCONFIRMED", EmailMessage.final_status_at: func.utc_timestamp()},
            synchronize_session=False)
        return int(n)

    # ── Rates ─────────────────────────────────────────────────────────────
    def _outcomes(self, *criteria: ColumnElement[bool]) -> Tuple[int, int, int]:
        row = self._session.query(
            func.count(EmailMessage.message_id),
            func.coalesce(func.sum(case((HARD_BOUNCE, 1), else_=0)), 0),
            func.coalesce(func.sum(case((COMPLAINT, 1), else_=0)), 0),
        ).filter(EmailMessage.direction == "OUTBOUND", EmailMessage.sent_at.isnot(None), *criteria).one()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    def campaign_outcomes(self, campaign_id: str, sent_after: Optional[datetime] = None) -> Tuple[int, int, int]:
        """(sends, hard bounces, complaints) of a campaign, optionally only mail sent after a time."""
        criteria = [EmailMessage.campaign_id == campaign_id]
        if sent_after is not None:
            criteria.append(EmailMessage.sent_at > sent_after)
        return self._outcomes(*criteria)

    def domain_outcomes(self, tenant_id: str, domain: str, since: datetime) -> Tuple[int, int, int]:
        """(sends, hard bounces, complaints) of one tenant's mail from this domain since ``since``."""
        return self._outcomes(EmailMessage.sent_at >= since, from_domain_criterion(domain),
                              tenant_message_criterion(tenant_id))

    # ── Campaigns ─────────────────────────────────────────────────────────
    def get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        return self._session.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()

    def campaign_tenant(self, campaign_id: str) -> Optional[str]:
        return self._session.query(Campaign.tenant_id).filter(Campaign.campaign_id == campaign_id).scalar()

    def active_campaign_ids(self) -> List[str]:
        return [cid for (cid,) in self._session.query(Campaign.campaign_id).filter(Campaign.status == "ACTIVE").all()]

    def freeze_queue(self, campaign_id: str) -> int:
        """Queued / scheduled mail of a paused campaign waits as PAUSED_BY_CAMPAIGN."""
        return int(self._session.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id, EmailMessage.status.in_(list(QUEUED_STATUSES)),
        ).update({"status": "PAUSED_BY_CAMPAIGN"}, synchronize_session=False))

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def active_admin_ids(self, tenant_id: str) -> List[str]:
        return [uid for (uid,) in self._session.query(User.user_id).filter(
            User.tenant_id == tenant_id, User.role.in_(("SUPER_ADMIN", "ADMIN")), User.status == "ACTIVE").all()]

    # ── Hourly cycle ──────────────────────────────────────────────────────
    def recent_senders(self, since: datetime) -> Set[Tuple[Optional[str], Optional[str]]]:
        """(sender address, sending tenant) of outbound mail sent since ``since``."""
        rows = (self._session.query(EmailMessage.from_email, func.coalesce(SendingInbox.tenant_id, Campaign.tenant_id))
                .outerjoin(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
                .outerjoin(Campaign, Campaign.campaign_id == EmailMessage.campaign_id)
                .filter(EmailMessage.sent_at >= since, EmailMessage.from_email.isnot(None),
                        EmailMessage.direction == "OUTBOUND")
                .distinct())
        return {(sender, tenant) for sender, tenant in rows}
