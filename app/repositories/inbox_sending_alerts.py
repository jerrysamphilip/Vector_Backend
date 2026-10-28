"""
SQL behind the sending-pipeline alerts (S-11) and the email accounts page's paused / capacity fields
(F-20). Read-only aggregate queries, each tenant-scoped (email_messages carries no tenant_id: it is
reached through its contact, prospects.tenant_id). The pause / resume writes for a mailbox are here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import ColumnElement, and_, case, func, or_
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.email_message import EmailMessage
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.join_tables import campaign_inboxes
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.repositories.reports_campaign_metrics import SENT_STATUSES

PENDING_STATUSES = ("QUEUED", "SCHEDULED")
PAUSE_EVENT_PREFIX = "Mailbox paused"
RESUME_EVENT_SUBJECT = "Mailbox resumed"


@dataclass(frozen=True)
class CampaignWithoutMailbox:
    campaign_id: str
    campaign_name: str
    updated_at: Optional[datetime]
    pending: int


@dataclass(frozen=True)
class PauseInfo:
    reason: Optional[str]
    paused_at: Optional[datetime]


def _tenant_outbound(tenant_id: str) -> ColumnElement[bool]:
    return and_(Prospect.prospect_id == EmailMessage.prospect_id, Prospect.tenant_id == tenant_id,
                EmailMessage.direction == "OUTBOUND")


class SendingAlertRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def tenant_ids(self) -> List[str]:
        """Workspaces with at least one mailbox or campaign (the only ones that can have sending alerts)."""
        inbox_tenants = {t for (t,) in self._session.query(SendingInbox.tenant_id).distinct() if t}
        campaign_tenants = {t for (t,) in self._session.query(Campaign.tenant_id).filter(
            Campaign.status == "ACTIVE").distinct() if t}
        return sorted(inbox_tenants | campaign_tenants)

    def tenant_inboxes(self, tenant_id: str) -> List[SendingInbox]:
        return self._session.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id).all()

    def get_inbox(self, tenant_id: str, inbox_id: str, *, lock: bool = False) -> Optional[SendingInbox]:
        q = self._session.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id,
                                                     SendingInbox.tenant_id == tenant_id)
        if lock:
            q = q.with_for_update()
        return q.first()

    def sent_since_by_inbox(self, inbox_ids: Iterable[str], since: datetime) -> Dict[str, int]:
        """Outbound mail per inbox that actually went out since ``since`` (status SENT, or BOUNCED / COMPLAINED
        after the send: the campaign counters' SENT_STATUSES); FAILED / QUEUED rows with a stray sent_at are
        not counted. Every campaign using the inbox counts."""
        ids = list(inbox_ids)
        if not ids:
            return {}
        rows = self._session.query(EmailMessage.inbox_id, func.count(EmailMessage.message_id)).filter(
            EmailMessage.inbox_id.in_(ids), EmailMessage.direction == "OUTBOUND",
            EmailMessage.status.in_(SENT_STATUSES), EmailMessage.sent_at >= since,
        ).group_by(EmailMessage.inbox_id).all()
        return {inbox_id: int(n) for inbox_id, n in rows}

    def pending_by_inbox(self, inbox_ids: Iterable[str]) -> Dict[str, int]:
        ids = list(inbox_ids)
        if not ids:
            return {}
        rows = self._session.query(EmailMessage.inbox_id, func.count(EmailMessage.message_id)).filter(
            EmailMessage.inbox_id.in_(ids), EmailMessage.direction == "OUTBOUND",
            EmailMessage.status.in_(PENDING_STATUSES), EmailMessage.sent_at.is_(None),
        ).group_by(EmailMessage.inbox_id).all()
        return {inbox_id: int(n) for inbox_id, n in rows}

    def campaigns_without_active_mailbox(self, tenant_id: str) -> List[CampaignWithoutMailbox]:
        """ACTIVE campaigns with mail waiting and none of their mailboxes ACTIVE (or none at all)."""
        active = func.count(case((SendingInbox.status == "ACTIVE", SendingInbox.inbox_id)))
        campaigns = (
            self._session.query(Campaign.campaign_id, Campaign.campaign_name, Campaign.updated_at)
            .outerjoin(campaign_inboxes, campaign_inboxes.c.campaign_id == Campaign.campaign_id)
            .outerjoin(SendingInbox, and_(SendingInbox.inbox_id == campaign_inboxes.c.inbox_id,
                                          SendingInbox.tenant_id == tenant_id))
            .filter(Campaign.tenant_id == tenant_id, Campaign.status == "ACTIVE")
            .group_by(Campaign.campaign_id, Campaign.campaign_name, Campaign.updated_at)
            .having(active == 0)
            .all()
        )
        if not campaigns:
            return []
        pending = dict(
            self._session.query(EmailMessage.campaign_id, func.count(EmailMessage.message_id)).filter(
                EmailMessage.campaign_id.in_([c[0] for c in campaigns]), EmailMessage.direction == "OUTBOUND",
                EmailMessage.status.in_(PENDING_STATUSES), EmailMessage.sent_at.is_(None),
            ).group_by(EmailMessage.campaign_id).all()
        )
        return [CampaignWithoutMailbox(cid, name, updated, int(pending.get(cid, 0)))
                for cid, name, updated in campaigns if pending.get(cid)]

    def send_outcomes(self, tenant_id: str, since: datetime) -> Tuple[int, int, Optional[str]]:
        """(failed, sent, latest failure reason) of the tenant's outbound mail since ``since``."""
        attempted_at = func.coalesce(EmailMessage.final_status_at, EmailMessage.next_retry_at,
                                     EmailMessage.scheduled_at)
        failed_in_window = and_(EmailMessage.status == "FAILED", attempted_at >= since)
        sent_in_window = EmailMessage.sent_at >= since
        failed, sent = (
            self._session.query(
                func.coalesce(func.sum(case((failed_in_window, 1), else_=0)), 0),
                func.coalesce(func.sum(case((sent_in_window, 1), else_=0)), 0),
            )
            .select_from(EmailMessage).join(Prospect, _tenant_outbound(tenant_id))
            .filter(or_(failed_in_window, sent_in_window))
            .one()
        )
        latest = None
        if failed:
            row = (
                self._session.query(EmailMessage.failure_reason)
                .join(Prospect, _tenant_outbound(tenant_id))
                .filter(failed_in_window).order_by(attempted_at.desc()).first()
            )
            latest = row[0] if row else None
        return int(failed), int(sent), latest

    def delivery_gap(self, tenant_id: str, start: datetime, end: datetime) -> Tuple[int, int]:
        """(sent, still without any delivery / bounce / complaint report) for mail sent in [start, end]."""
        unconfirmed = and_(EmailMessage.delivered_at.is_(None),
                           or_(EmailMessage.final_status.is_(None), EmailMessage.final_status == "UNCONFIRMED"),
                           EmailMessage.status.in_(("SENT", "DELIVERED")))
        sample, missing = (
            self._session.query(func.count(EmailMessage.message_id),
                                func.coalesce(func.sum(case((unconfirmed, 1), else_=0)), 0))
            .select_from(EmailMessage).join(Prospect, _tenant_outbound(tenant_id))
            .filter(EmailMessage.sent_at >= start, EmailMessage.sent_at <= end)
            .one()
        )
        return int(sample), int(missing)

    def stuck_sending(self, tenant_id: str, cutoff: datetime) -> int:
        return int(
            self._session.query(func.count(EmailMessage.message_id))
            .join(Prospect, _tenant_outbound(tenant_id))
            .filter(EmailMessage.status == "SENDING", EmailMessage.sent_at.is_(None),
                    EmailMessage.claimed_at < cutoff)
            .scalar() or 0
        )

    def platform_deferrals(self, tenant_id: str, codes: Iterable[str], since: datetime) -> int:
        """Mail waiting because SES (the platform) refused it recently (deferred, not failed)."""
        return int(
            self._session.query(func.count(EmailMessage.message_id))
            .join(Prospect, _tenant_outbound(tenant_id))
            .filter(EmailMessage.status.in_(PENDING_STATUSES), EmailMessage.deferred_count > 0,
                    EmailMessage.last_error_code.in_(list(codes)), EmailMessage.next_retry_at >= since)
            .scalar() or 0
        )

    def last_pause_events(self, tenant_id: str, inbox_ids: Iterable[str]) -> Dict[str, PauseInfo]:
        """Newest pause / resume entry of each mailbox's activity log (one query); a resume clears it."""
        ids = list(inbox_ids)
        if not ids:
            return {}
        relevant = or_(InboxWarmupEvent.subject.like(f"{PAUSE_EVENT_PREFIX}%"),
                       InboxWarmupEvent.subject == RESUME_EVENT_SUBJECT)
        rn = func.row_number().over(partition_by=InboxWarmupEvent.inbox_id,
                                    order_by=InboxWarmupEvent.event_time.desc()).label("rn")
        ranked = (
            self._session.query(InboxWarmupEvent.inbox_id, InboxWarmupEvent.subject, InboxWarmupEvent.detail,
                                InboxWarmupEvent.event_time, rn)
            .filter(InboxWarmupEvent.tenant_id == tenant_id, InboxWarmupEvent.inbox_id.in_(ids), relevant)
            .subquery()
        )
        rows = self._session.query(ranked).filter(ranked.c.rn == 1).all()
        return {r.inbox_id: PauseInfo(r.detail or r.subject, r.event_time) for r in rows
                if r.subject != RESUME_EVENT_SUBJECT}

    def add_inbox_event(self, inbox: SendingInbox, subject: str, detail: str, code: str, at: datetime,
                        event_type: str = "STATUS", status: str = "SUCCESS") -> None:
        self._session.add(InboxWarmupEvent(
            tenant_id=inbox.tenant_id, inbox_id=inbox.inbox_id, event_type=event_type, status=status,
            subject=subject, detail=detail, event_metadata={"code": code}, event_time=at,
        ))
