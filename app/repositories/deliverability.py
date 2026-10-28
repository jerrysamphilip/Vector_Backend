"""
SQL for deliverability (BR-DF-04 / 07, ADR 0009): tenant-owned sending domains, their health snapshots
and reputation alerts, ingested provider metrics / feedback, the local sending statistics and the
per-domain sent log, the mailboxes and campaigns on a domain, and the tenant a message or sender
belongs to.

A sending domain record belongs to one tenant; two tenants on the same domain name each have their own
row. Every lookup by domain name also names the tenant, and every per-domain figure counts only that
tenant's mail. EmailMessage has no tenant_id: a message belongs to the tenant of its mailbox
(inbox_id) or, failing that, of its campaign (campaign_id) -- ``tenant_message_criterion``.
Never commits: the calling service owns the unit of work.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime
from typing import Any, List, Optional, Sequence, Set, Tuple

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.domain_reputation import DomainHealthSnapshot, ReputationAlert, SendingDomain
from app.models.email_message import EmailEvent, EmailMessage
from app.models.join_tables import campaign_inboxes
from app.models.provider_reputation import ExternalFeedbackEvent, ExternalReputationMetric
from app.models.sending_inbox import SendingInbox
from app.repositories.base import SqlAlchemyUnitOfWork

__all__ = ["DeliverabilityRepository", "DeliverabilityUnitOfWork", "Session", "SendingDomain",
           "from_domain_criterion", "tenant_message_criterion"]


def tenant_message_criterion(tenant_id: str) -> ColumnElement[bool]:
    """SQL criterion: this EmailMessage was sent by (belongs to) the tenant."""
    return or_(
        EmailMessage.inbox_id.in_(select(SendingInbox.inbox_id).where(SendingInbox.tenant_id == tenant_id)),
        EmailMessage.campaign_id.in_(select(Campaign.campaign_id).where(Campaign.tenant_id == tenant_id)),
    )


def from_domain_criterion(domain_name: str) -> ColumnElement[bool]:
    return EmailMessage.from_email.like(f"%@{domain_name.strip().lower()}")


def _outbound_scope(tenant_id: Optional[str], domain: Optional[str]) -> List[ColumnElement[bool]]:
    scope: List[ColumnElement[bool]] = [EmailMessage.direction == "OUTBOUND"]
    if domain:
        scope.append(from_domain_criterion(domain))
    if tenant_id:
        scope.append(tenant_message_criterion(tenant_id))
    return scope


class DeliverabilityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Sending domains ───────────────────────────────────────────────────
    def get_domain(self, tenant_id: Optional[str], domain_name: str) -> Optional[SendingDomain]:
        if not tenant_id or not domain_name:
            return None
        return self._session.query(SendingDomain).filter(
            SendingDomain.tenant_id == tenant_id, SendingDomain.domain_name == domain_name.strip().lower()).first()

    def add_domain(self, domain: SendingDomain) -> SendingDomain:
        self._session.add(domain)
        self._session.flush()
        return domain

    def insert_domain(self, domain: SendingDomain) -> bool:
        """Add and flush a new domain row; False (nothing added) when the (tenant, name) unique key
        already holds one, e.g. a concurrent add."""
        try:
            with self._session.begin_nested():
                self._session.add(domain)
                self._session.flush()
        except IntegrityError:
            return False
        return True

    def count_domains(self, tenant_id: str) -> int:
        return int(self._session.query(SendingDomain).filter(SendingDomain.tenant_id == tenant_id).count())

    def tenant_domains(self, tenant_id: str) -> List[SendingDomain]:
        return list(self._session.query(SendingDomain).filter(SendingDomain.tenant_id == tenant_id)
                    .order_by(SendingDomain.domain_name).all())

    def owned_domains(self, tenant_id: Optional[str] = None) -> List[SendingDomain]:
        """Every tenant-owned domain row, or only one tenant's."""
        q = self._session.query(SendingDomain).filter(SendingDomain.tenant_id.isnot(None))
        if tenant_id:
            q = q.filter(SendingDomain.tenant_id == tenant_id)
        return list(q.all())

    def rows_named(self, domain_name: str) -> List[SendingDomain]:
        """Every tenant's row for this domain name (DNS and the SES identity are facts of the name)."""
        return list(self._session.query(SendingDomain).filter(
            SendingDomain.domain_name == domain_name, SendingDomain.tenant_id.isnot(None)).all())

    def owned_domain_names(self) -> List[str]:
        return sorted({name.lower() for (name,) in self._session.query(SendingDomain.domain_name)
                       .filter(SendingDomain.tenant_id.isnot(None)).all()})

    def tenants_with_domain(self, domain_name: str) -> List[str]:
        return [t for (t,) in self._session.query(SendingDomain.tenant_id).filter(
            SendingDomain.domain_name == domain_name, SendingDomain.tenant_id.isnot(None)).distinct()]

    def tenant_domain_pairs(self) -> Set[Tuple[str, str]]:
        return {(t, d.lower()) for (t, d) in self._session.query(SendingDomain.tenant_id, SendingDomain.domain_name)
                .filter(SendingDomain.tenant_id.isnot(None))}

    def delete_domain(self, domain: SendingDomain) -> None:
        """The tenant's record of the domain, its snapshots and alerts (cascade) and the tenant's
        ingested provider data for the name. Other tenants' rows are untouched."""
        for model in (ExternalReputationMetric, ExternalFeedbackEvent):
            self._session.query(model).filter(model.tenant_id == domain.tenant_id,
                                              model.domain_name == domain.domain_name).delete(synchronize_session=False)
        self._session.delete(domain)

    # ── Snapshots and alerts ──────────────────────────────────────────────
    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def snapshotted_since(self, since: datetime) -> Set[str]:
        """Domain ids with a health snapshot at or after ``since``."""
        return {d for (d,) in self._session.query(DomainHealthSnapshot.domain_id)
                .filter(DomainHealthSnapshot.snapshot_at >= since).distinct()}

    def delete_snapshots_before(self, cutoff: datetime) -> int:
        return int(self._session.query(DomainHealthSnapshot).filter(
            DomainHealthSnapshot.snapshot_at < cutoff).delete(synchronize_session=False))

    def reputation_alerts(self, tenant_id: str, include_resolved: bool = False) -> List[ReputationAlert]:
        q = self._session.query(ReputationAlert).filter(ReputationAlert.tenant_id == tenant_id)
        if not include_resolved:
            q = q.filter(ReputationAlert.is_resolved.is_(False))
        return list(q.order_by(ReputationAlert.created_at.desc()).all())

    # ── Provider data ─────────────────────────────────────────────────────
    def external_metrics(self, tenant_id: str, provider: str, domain_name: str,
                         limit: int) -> List[ExternalReputationMetric]:
        return list(self._session.query(ExternalReputationMetric).filter(
            ExternalReputationMetric.tenant_id == tenant_id, ExternalReputationMetric.provider == provider,
            ExternalReputationMetric.domain_name == domain_name,
        ).order_by(ExternalReputationMetric.ingested_at.desc()).limit(limit).all())

    def external_events(self, tenant_id: str, provider: str, domain_name: str,
                        limit: int) -> List[ExternalFeedbackEvent]:
        return list(self._session.query(ExternalFeedbackEvent).filter(
            ExternalFeedbackEvent.tenant_id == tenant_id, ExternalFeedbackEvent.provider == provider,
            ExternalFeedbackEvent.domain_name == domain_name,
        ).order_by(ExternalFeedbackEvent.ingested_at.desc()).limit(limit).all())

    # ── Mail statistics ───────────────────────────────────────────────────
    def daily_sends(self, tenant_id: Optional[str], domain: Optional[str], since: datetime) -> List[Tuple[date, int]]:
        """(day, outbound emails sent that day) since ``since``."""
        day = func.date(EmailMessage.sent_at)
        return [(d, int(n)) for d, n in self._session.query(day, func.count(EmailMessage.message_id)).filter(
            *_outbound_scope(tenant_id, domain), EmailMessage.sent_at >= since).group_by(day).all()]

    def daily_events(self, tenant_id: Optional[str], domain: Optional[str], since: datetime,
                     event_types: Sequence[int]) -> List[Tuple[date, int, int]]:
        """(day, event type, count) of these events on outbound mail since ``since``."""
        day = func.date(EmailEvent.event_time)
        rows = self._session.query(day, EmailEvent.event_type, func.count(EmailEvent.event_id)).join(
            EmailMessage, EmailEvent.message_id == EmailMessage.message_id,
        ).filter(*_outbound_scope(tenant_id, domain), EmailEvent.event_time >= since,
                 EmailEvent.event_type.in_(list(event_types))).group_by(day, EmailEvent.event_type).all()
        return [(d, int(t), int(n)) for d, t, n in rows]

    def sent_log(self, tenant_id: str, domain_name: str, limit: int) -> List[Tuple[EmailMessage, Optional[str]]]:
        """The tenant's latest outbound emails from this domain with their campaign name."""
        rows = (self._session.query(EmailMessage, Campaign.campaign_name)
                .outerjoin(Campaign, EmailMessage.campaign_id == Campaign.campaign_id)
                .filter(EmailMessage.direction == "OUTBOUND", EmailMessage.from_email.ilike(f"%@{domain_name}"),
                        # Only this tenant's messages, even when another tenant uses the same domain
                        tenant_message_criterion(tenant_id))
                .order_by(EmailMessage.sent_at.desc(), EmailMessage.scheduled_at.desc())
                .limit(limit).all())
        return [(m, name) for m, name in rows]

    # ── Mailboxes and campaigns on a domain ───────────────────────────────
    def inbox_addresses(self, tenant_id: Optional[str] = None) -> List[Tuple[Optional[str], Optional[str]]]:
        """(tenant id, address) of every mailbox, or of one tenant's."""
        q = self._session.query(SendingInbox.tenant_id, SendingInbox.email_address)
        if tenant_id:
            q = q.filter(SendingInbox.tenant_id == tenant_id)
        return [(t, e) for t, e in q.all()]

    def inboxes_on_domain(self, tenant_id: str, domain_name: str) -> List[SendingInbox]:
        return list(self._session.query(SendingInbox).filter(
            SendingInbox.tenant_id == tenant_id, SendingInbox.email_address.like(f"%@{domain_name}")).all())

    def active_campaigns(self) -> List[Campaign]:
        return list(self._session.query(Campaign).filter(Campaign.status == "ACTIVE").all())

    def active_campaigns_on_domain(self, tenant_id: str, domain_name: str, *, distinct: bool = True) -> List[Campaign]:
        """The tenant's active campaigns sending from its mailboxes on this domain."""
        q = (self._session.query(Campaign)
             .join(campaign_inboxes, campaign_inboxes.c.campaign_id == Campaign.campaign_id)
             .join(SendingInbox, SendingInbox.inbox_id == campaign_inboxes.c.inbox_id)
             .filter(Campaign.status == "ACTIVE", Campaign.tenant_id == tenant_id,
                     SendingInbox.tenant_id == tenant_id, SendingInbox.email_address.like(f"%@{domain_name}")))
        return list((q.distinct() if distinct else q).all())

    # ── Whose mail ────────────────────────────────────────────────────────
    def inbox_tenant(self, inbox_id: str) -> Optional[str]:
        return self._session.query(SendingInbox.tenant_id).filter(SendingInbox.inbox_id == inbox_id).scalar()

    def campaign_tenant(self, campaign_id: str) -> Optional[str]:
        return self._session.query(Campaign.tenant_id).filter(Campaign.campaign_id == campaign_id).scalar()

    def sender_tenants(self, sender_email: str) -> Set[str]:
        return {t for (t,) in self._session.query(SendingInbox.tenant_id)
                .filter(SendingInbox.email_address == sender_email).all()}


def borrow(session: Session) -> DeliverabilityRepository:
    """Repository access inside a caller's transaction (legacy ``fn(db, ...)`` helpers)."""
    return DeliverabilityRepository(session)


class DeliverabilityUnitOfWork(SqlAlchemyUnitOfWork):
    def __enter__(self) -> "DeliverabilityUnitOfWork":
        super().__enter__()
        return self

    @property
    def domains(self) -> DeliverabilityRepository:
        return DeliverabilityRepository(self.session)

    def savepoint(self) -> AbstractContextManager[Any]:
        return self.session.begin_nested()

    @classmethod
    def for_session(cls, session: Session) -> "DeliverabilityUnitOfWork":
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
