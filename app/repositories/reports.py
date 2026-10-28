"""
Campaign analytics reports repository (ADR 0009, chunk A6): the SQL behind GET /reports/*
(dashboard summary, global analytics, campaign comparison, provider performance, mailbox health,
optimisation metrics, team breakdown and the CSV / Word exports).

Every query is tenant-scoped: campaign figures go through ``_campaign_ids`` (the tenant's campaigns,
optionally limited to ``Owners``, whose campaigns the service decided the viewer may see). Nothing
here applies the reporting rules (fallbacks, rates, series, sorting of results); the service does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import ColumnElement, and_, case, func
from sqlalchemy.orm import Query, Session

from app.domain.paging import Paging
from app.domain.reports import ComparisonSort, DateWindow, OptionalWindow, Owners
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.repositories.base import TenantId
from app.repositories.reports_campaign_metrics import sent_message_filters, unique_openers_expr, unique_prospects_expr

__all__ = ["CampaignReportRepository", "ComparisonRow", "GlobalCounts", "MailboxCounts", "OptimizationCounts",
           "ProspectExport", "ProviderCounts", "Session", "SqlAlchemyCampaignReportRepository", "UserCounts"]

EVENT_TYPES = (EmailEvent.EVENT_OPEN, EmailEvent.EVENT_REPLY, EmailEvent.EVENT_BOUNCE)


def owner_filter(column: Any, owners: Owners) -> list[ColumnElement[bool]]:
    """Filter for one user id, a set of ids (a manager's team), or nobody (None = everyone)."""
    if not owners:
        return [] if owners is None else [column.is_(None) & column.isnot(None)]  # empty team -> nothing
    if isinstance(owners, str):
        return [column == owners]
    return [column.in_(list(owners))]


@dataclass(frozen=True)
class GlobalCounts:
    sent: int
    replied: int
    bounced: int
    opened: int
    clicked: int
    opened_prospects: int      # prospects with status OPENED / REPLIED (fallback when open tracking is off)
    replied_prospects: int     # prospects with status REPLIED
    leads_contacted: int
    active_campaigns: int


@dataclass(frozen=True)
class ComparisonRow:
    campaign_id: str
    campaign_name: str
    status: str
    created_at: Optional[datetime]
    sent_count: int
    opened_count: int
    replied_count: int
    bounced_count: int


@dataclass(frozen=True)
class ProviderCounts:
    sent: dict[str, int]
    events: dict[str, dict[int, int]]
    openers: dict[str, int]


@dataclass(frozen=True)
class MailboxCounts:
    sent: list[tuple[str, int, int]]      # (mailbox, sent, leads) in query order
    events: dict[str, dict[int, int]]
    openers: dict[str, int]


@dataclass(frozen=True)
class OptimizationCounts:
    campaigns_with_replies: int
    total_prospects: int
    follow_up_sent: int
    follow_up_replies: int


@dataclass(frozen=True)
class UserCounts:
    campaigns: int
    sent: int
    opened: int
    replied: int
    bounced: int


@dataclass(frozen=True)
class ProspectExport:
    rows: list[Any]                        # email, names, company, designation, status, step, prospect_id
    engagement: dict[str, dict[str, Any]]  # prospect_id -> {"sent", "delivered"}
    events: dict[str, dict[int, int]]      # prospect_id -> {event_type: count}


class CampaignReportRepository(Protocol):
    def global_counts(self, tenant_id: TenantId, owners: Owners, window: DateWindow) -> GlobalCounts: ...
    def daily_counts(self, tenant_id: TenantId, owners: Owners, window: DateWindow) -> tuple[dict, dict, dict, dict]: ...
    def comparison_page(self, tenant_id: TenantId, owners: Owners, window: OptionalWindow, sort: ComparisonSort,
                        paging: Paging) -> tuple[list[ComparisonRow], int]: ...
    def prospect_status_counts(self, campaign_ids: Sequence[str]) -> dict[str, dict[str, int]]: ...
    def provider_counts(self, tenant_id: TenantId, owners: Owners, window: DateWindow) -> ProviderCounts: ...
    def lead_counts(self, tenant_id: TenantId, owners: Owners) -> tuple[int, int]: ...
    def mailbox_states(self, tenant_id: TenantId) -> list[tuple[Optional[str], Any]]: ...
    def campaign_status_counts(self, tenant_id: TenantId, owners: Owners) -> dict[str, int]: ...
    def mailbox_counts(self, tenant_id: TenantId, owners: Owners) -> MailboxCounts: ...
    def optimization_counts(self, tenant_id: TenantId, owners: Owners) -> OptimizationCounts: ...
    def first_reply_times(self, tenant_id: TenantId, owners: Owners) -> list[tuple[Any, Any]]: ...
    def breakdown_users(self, tenant_id: TenantId, user_ids: Owners) -> list[Any]: ...
    def user_counts(self, tenant_id: TenantId, user_id: str, window: DateWindow) -> UserCounts: ...
    def user_name(self, tenant_id: TenantId, user_id: str) -> Optional[tuple[Optional[str], Optional[str], str]]: ...
    def campaign_owner(self, tenant_id: TenantId, campaign_id: str) -> tuple[bool, Optional[str]]: ...
    def prospect_export(self, campaign_id: str) -> ProspectExport: ...


def _by_event(rows: Sequence[Any]) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    for key, etype, cnt in rows:
        out.setdefault(key, {})[etype] = cnt
    return out


class SqlAlchemyCampaignReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── building blocks ──

    def _campaign_ids(self, tenant_id: TenantId, owners: Owners) -> Query[Any]:
        """The tenant's campaign ids (optionally of some owners), for ``column.in_(...)``."""
        sub = (self._session.query(Campaign.campaign_id)
               .filter(Campaign.tenant_id == tenant_id, *owner_filter(Campaign.created_by, owners)).subquery())
        return self._session.query(sub.c.campaign_id)

    def _in_campaigns(self, tenant_id: TenantId, owners: Owners, column: Any = EmailMessage.campaign_id) -> Any:
        return column.in_(self._campaign_ids(tenant_id, owners))

    @staticmethod
    def _sent_in(window: DateWindow) -> list[Any]:
        return [*sent_message_filters(), EmailMessage.sent_at >= window.start,
                EmailMessage.sent_at < window.end_exclusive]

    @staticmethod
    def _events_in(window: DateWindow) -> list[Any]:
        return [EmailEvent.event_time >= window.start, EmailEvent.event_time < window.end_exclusive]

    def _unique_prospects_with(self, event_type: int, *filters: Any) -> int:
        """Unique prospects (per campaign) with an event of ``event_type``."""
        return (self._session.query(unique_prospects_expr()).select_from(EmailEvent)
                .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
                .filter(EmailEvent.event_type == event_type, *filters).scalar()) or 0

    def _scalar(self, query: Query[Any]) -> int:
        return query.scalar() or 0

    def _distinct_prospects_with_status(self, in_campaigns: Any, statuses: Sequence[str]) -> int:
        status_filter = (CampaignProspect.status.in_(list(statuses)) if len(statuses) > 1
                         else CampaignProspect.status == statuses[0])
        return self._scalar(self._session.query(func.count(func.distinct(CampaignProspect.prospect_id)))
                            .filter(in_campaigns, status_filter))

    # ── global analytics ──

    def global_counts(self, tenant_id: TenantId, owners: Owners, window: DateWindow) -> GlobalCounts:
        s = self._session
        in_campaigns = self._in_campaigns(tenant_id, owners)
        sent = self._scalar(s.query(func.count(EmailMessage.message_id)).filter(in_campaigns, *self._sent_in(window)))
        # replied / bounced = distinct messages with the event (as the campaign metrics count them)
        event_row: Any = (
            s.query(
                func.count(func.distinct(case((EmailEvent.event_type == EmailEvent.EVENT_REPLY, EmailEvent.message_id)))
                           ).label("replied"),
                func.count(func.distinct(case((EmailEvent.event_type == EmailEvent.EVENT_BOUNCE, EmailEvent.message_id)))
                           ).label("bounced"),
            )
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(in_campaigns, *self._events_in(window)).one()
        )
        opened = self._unique_prospects_with(EmailEvent.EVENT_OPEN, in_campaigns, *self._events_in(window))
        clicked = self._unique_prospects_with(EmailEvent.EVENT_CLICK, in_campaigns, *self._events_in(window))
        cp_in = self._in_campaigns(tenant_id, owners, CampaignProspect.campaign_id)
        leads_contacted = self._scalar(s.query(func.count(func.distinct(EmailMessage.prospect_id)))
                                       .filter(in_campaigns, *self._sent_in(window)))
        active = self._scalar(s.query(func.count(Campaign.campaign_id)).filter(
            self._in_campaigns(tenant_id, owners, Campaign.campaign_id), Campaign.status == "ACTIVE"))
        return GlobalCounts(
            sent=sent, replied=event_row.replied or 0, bounced=event_row.bounced or 0, opened=opened, clicked=clicked,
            opened_prospects=self._distinct_prospects_with_status(cp_in, ("OPENED", "REPLIED")),
            replied_prospects=self._distinct_prospects_with_status(cp_in, ("REPLIED",)),
            leads_contacted=leads_contacted, active_campaigns=active)

    def daily_counts(self, tenant_id: TenantId, owners: Owners, window: DateWindow) -> tuple[dict, dict, dict, dict]:
        """Per day: sent messages, unique openers, replied and bounced messages."""
        s = self._session
        in_campaigns = self._in_campaigns(tenant_id, owners)
        day_sent = func.date(EmailMessage.sent_at)
        sent = {r.d: r.cnt for r in s.query(day_sent.label("d"), func.count(EmailMessage.message_id).label("cnt"))
                .filter(in_campaigns, *self._sent_in(window)).group_by(day_sent).all()}
        day_event = func.date(EmailEvent.event_time)
        event_rows = (
            s.query(day_event.label("d"), EmailEvent.event_type.label("etype"),
                    func.count(func.distinct(EmailEvent.message_id)).label("cnt"))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(in_campaigns, *self._events_in(window), EmailEvent.event_type.in_(EVENT_TYPES))
            .group_by(day_event, EmailEvent.event_type).all()
        )
        opened = {r.d: r.cnt for r in (
            s.query(day_event.label("d"), unique_openers_expr().label("cnt"))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(in_campaigns, *self._events_in(window), EmailEvent.event_type == EmailEvent.EVENT_OPEN)
            .group_by(day_event).all())}
        replied = {r.d: r.cnt for r in event_rows if r.etype == EmailEvent.EVENT_REPLY}
        bounced = {r.d: r.cnt for r in event_rows if r.etype == EmailEvent.EVENT_BOUNCE}
        return sent, opened, replied, bounced

    # ── campaign comparison ──

    def comparison_page(self, tenant_id: TenantId, owners: Owners, window: OptionalWindow, sort: ComparisonSort,
                        paging: Paging) -> tuple[list[ComparisonRow], int]:
        s = self._session
        sent_filters = list(sent_message_filters())
        event_filters = []
        if window.start:
            sent_filters.append(EmailMessage.sent_at >= window.start)
            event_filters.append(EmailEvent.event_time >= window.start)
        if window.end_exclusive:
            sent_filters.append(EmailMessage.sent_at < window.end_exclusive)
            event_filters.append(EmailEvent.event_time < window.end_exclusive)
        sent_sq = (s.query(EmailMessage.campaign_id.label("campaign_id"),
                           func.count(EmailMessage.message_id).label("sent_count"))
                   .filter(*sent_filters).group_by(EmailMessage.campaign_id).subquery())
        event_sq = (
            s.query(
                EmailMessage.campaign_id.label("campaign_id"),
                func.count(func.distinct(case((EmailEvent.event_type == EmailEvent.EVENT_OPEN, EmailMessage.prospect_id)))
                           ).label("opened_count"),
                func.sum(case((EmailEvent.event_type == EmailEvent.EVENT_REPLY, 1), else_=0)).label("replied_count"),
                func.sum(case((EmailEvent.event_type == EmailEvent.EVENT_BOUNCE, 1), else_=0)).label("bounced_count"),
            )
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(*event_filters).group_by(EmailMessage.campaign_id).subquery()
        )
        cols = {"sent_count": func.coalesce(sent_sq.c.sent_count, 0),
                "opened_count": func.coalesce(event_sq.c.opened_count, 0),
                "replied_count": func.coalesce(event_sq.c.replied_count, 0),
                "bounced_count": func.coalesce(event_sq.c.bounced_count, 0)}
        query = (
            s.query(Campaign.campaign_id, Campaign.campaign_name, Campaign.status, Campaign.created_at,
                    *(col.label(name) for name, col in cols.items()))
            .outerjoin(sent_sq, sent_sq.c.campaign_id == Campaign.campaign_id)
            .outerjoin(event_sq, event_sq.c.campaign_id == Campaign.campaign_id)
            .filter(Campaign.tenant_id == tenant_id, *owner_filter(Campaign.created_by, owners))
        )
        total = query.count()
        order = self._comparison_order(sort, cols)
        rows = query.order_by(order.desc() if sort.descending else order.asc()) \
            .offset(paging.offset).limit(paging.limit).all()
        return [ComparisonRow(r.campaign_id, r.campaign_name, r.status, r.created_at, r.sent_count or 0,
                              r.opened_count or 0, r.replied_count or 0, r.bounced_count or 0) for r in rows], total

    @staticmethod
    def _comparison_order(sort: ComparisonSort, cols: dict[str, Any]) -> Any:
        """The whitelisted sort column (``ComparisonSort`` only ever names one of these)."""
        if sort.is_rate:
            part = cols[{"open_rate": "opened_count", "reply_rate": "replied_count",
                         "bounce_rate": "bounced_count"}[sort.field]]
            # open / reply rates divide by delivered (sent - bounced), bounce rate by sent (domain.reports.delivered)
            whole = cols["sent_count"] if sort.field == "bounce_rate" else cols["sent_count"] - cols["bounced_count"]
            return func.coalesce(case((whole > 0, (part * 100.0) / whole), else_=0.0), 0)
        return {**cols, "campaign_name": Campaign.campaign_name, "created_at": Campaign.created_at}[sort.field]

    def prospect_status_counts(self, campaign_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        """campaign_id -> {"OPENED": n, "REPLIED": n} distinct prospects (campaigns with any)."""
        if not campaign_ids:
            return {}
        rows = (
            self._session.query(CampaignProspect.campaign_id, CampaignProspect.status,
                                func.count(func.distinct(CampaignProspect.prospect_id)).label("cnt"))
            .filter(CampaignProspect.campaign_id.in_(list(campaign_ids)),
                    CampaignProspect.status.in_(["OPENED", "REPLIED"]))
            .group_by(CampaignProspect.campaign_id, CampaignProspect.status).all()
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r.campaign_id, {"OPENED": 0, "REPLIED": 0})[r.status] = r.cnt
        return out

    # ── providers and mailboxes ──

    def provider_counts(self, tenant_id: TenantId, owners: Owners, window: DateWindow) -> ProviderCounts:
        s = self._session
        provider = func.coalesce(SendingInbox.provider, "unknown")
        in_campaigns = self._in_campaigns(tenant_id, owners)
        sent_rows = (s.query(provider.label("provider"), func.count(EmailMessage.message_id).label("sent"))
                     .join(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
                     .filter(in_campaigns, *self._sent_in(window)).group_by(provider).all())
        event_rows = (
            s.query(provider.label("provider"), EmailEvent.event_type.label("etype"),
                    func.count(EmailEvent.event_id).label("cnt"))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .join(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
            .filter(in_campaigns, *self._events_in(window), EmailEvent.event_type.in_(EVENT_TYPES))
            .group_by(provider, EmailEvent.event_type).all()
        )
        openers = dict(
            s.query(provider, unique_openers_expr()).select_from(EmailEvent)
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .join(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
            .filter(in_campaigns, *self._events_in(window), EmailEvent.event_type == EmailEvent.EVENT_OPEN)
            .group_by(provider).all()
        )
        return ProviderCounts({r.provider: r.sent for r in sent_rows}, _by_event(event_rows), openers)

    def mailbox_states(self, tenant_id: TenantId) -> list[tuple[Optional[str], Any]]:
        rows = (self._session.query(SendingInbox.status, SendingInbox.warmup_enabled)
                .filter(SendingInbox.tenant_id == tenant_id).all())
        return [(r.status, r.warmup_enabled) for r in rows]

    def mailbox_counts(self, tenant_id: TenantId, owners: Owners) -> MailboxCounts:
        s = self._session
        in_campaigns = self._in_campaigns(tenant_id, owners)
        sent_rows = (
            s.query(SendingInbox.email_address.label("mailbox"), func.count(EmailMessage.message_id).label("sent"),
                    func.count(func.distinct(EmailMessage.prospect_id)).label("leads"))
            .join(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
            .filter(in_campaigns, *sent_message_filters()).group_by(SendingInbox.email_address).all()
        )
        event_rows = (
            s.query(SendingInbox.email_address.label("mailbox"), EmailEvent.event_type.label("etype"),
                    func.count(EmailEvent.event_id).label("cnt"))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .join(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
            .filter(in_campaigns, EmailEvent.event_type.in_(EVENT_TYPES))
            .group_by(SendingInbox.email_address, EmailEvent.event_type).all()
        )
        openers = dict(
            s.query(SendingInbox.email_address, unique_openers_expr()).select_from(EmailEvent)
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .join(SendingInbox, SendingInbox.inbox_id == EmailMessage.inbox_id)
            .filter(in_campaigns, EmailEvent.event_type == EmailEvent.EVENT_OPEN)
            .group_by(SendingInbox.email_address).all()
        )
        return MailboxCounts([(r.mailbox, r.sent, r.leads) for r in sent_rows], _by_event(event_rows), openers)

    # ── leads, campaigns, optimisation ──

    def lead_counts(self, tenant_id: TenantId, owners: Owners) -> tuple[int, int]:
        """(campaign prospects, those still on step 1)."""
        s = self._session
        cp_in = self._in_campaigns(tenant_id, owners, CampaignProspect.campaign_id)
        total = self._scalar(s.query(func.count(CampaignProspect.id)).filter(cp_in))
        first_step = self._scalar(s.query(func.count(CampaignProspect.id))
                                  .filter(cp_in, CampaignProspect.current_step == 1))
        return total, first_step

    def campaign_status_counts(self, tenant_id: TenantId, owners: Owners) -> dict[str, int]:
        rows = (self._session.query(Campaign.status, func.count(Campaign.campaign_id))
                .filter(Campaign.tenant_id == tenant_id, *owner_filter(Campaign.created_by, owners))
                .group_by(Campaign.status).all())
        return {r[0]: r[1] for r in rows}

    def optimization_counts(self, tenant_id: TenantId, owners: Owners) -> OptimizationCounts:
        s = self._session
        in_campaigns = self._in_campaigns(tenant_id, owners)
        cp_in = self._in_campaigns(tenant_id, owners, CampaignProspect.campaign_id)
        with_replies = self._scalar(
            s.query(func.count(func.distinct(EmailMessage.campaign_id)))
            .join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id)
            .filter(in_campaigns, EmailEvent.event_type == EmailEvent.EVENT_REPLY))
        total_prospects = self._scalar(s.query(func.count(CampaignProspect.id)).filter(cp_in))
        # Prospects past step 1 (follow-ups)
        follow_up = (s.query(CampaignProspect.prospect_id, CampaignProspect.campaign_id)
                     .filter(cp_in, CampaignProspect.current_step > 1).subquery())
        in_follow_up = EmailMessage.prospect_id.in_(s.query(follow_up.c.prospect_id))
        follow_up_sent = self._scalar(s.query(func.count(EmailMessage.message_id))
                                      .filter(in_campaigns, in_follow_up, *sent_message_filters()))
        follow_up_replies = self._scalar(
            s.query(func.count(EmailEvent.event_id))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(in_campaigns, in_follow_up, EmailEvent.event_type == EmailEvent.EVENT_REPLY))
        return OptimizationCounts(with_replies, total_prospects, follow_up_sent, follow_up_replies)

    def first_reply_times(self, tenant_id: TenantId, owners: Owners) -> list[tuple[Any, Any]]:
        """Per replying prospect: (first reply event, first message sent)."""
        rows = (
            self._session.query(func.min(EmailEvent.event_time).label("first_reply"),
                                func.min(EmailMessage.sent_at).label("first_sent"))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(self._in_campaigns(tenant_id, owners), EmailEvent.event_type == EmailEvent.EVENT_REPLY)
            .group_by(EmailMessage.prospect_id).all()
        )
        return [(r.first_reply, r.first_sent) for r in rows]

    # ── team breakdown ──

    def breakdown_users(self, tenant_id: TenantId, user_ids: Owners) -> list[Any]:
        return (self._session.query(User)
                .filter(User.tenant_id == tenant_id, User.status == "ACTIVE", User.role != "PLATFORM_ADMIN",
                        *owner_filter(User.user_id, user_ids))
                .order_by(User.first_name).all())

    def user_counts(self, tenant_id: TenantId, user_id: str, window: DateWindow) -> UserCounts:
        s = self._session
        in_campaigns = self._in_campaigns(tenant_id, user_id)
        campaigns = self._scalar(s.query(func.count(Campaign.campaign_id))
                                 .filter(Campaign.tenant_id == tenant_id, Campaign.created_by == user_id))
        sent = self._scalar(s.query(func.count(EmailMessage.message_id)).filter(in_campaigns, *self._sent_in(window)))
        row: Any = (
            s.query(func.sum(case((EmailEvent.event_type == EmailEvent.EVENT_REPLY, 1), else_=0)).label("replied"),
                    func.sum(case((EmailEvent.event_type == EmailEvent.EVENT_BOUNCE, 1), else_=0)).label("bounced"))
            .join(EmailMessage, EmailMessage.message_id == EmailEvent.message_id)
            .filter(in_campaigns, *self._events_in(window)).one()
        )
        opened = self._unique_prospects_with(EmailEvent.EVENT_OPEN, in_campaigns, *self._events_in(window))
        return UserCounts(campaigns, sent, opened, row.replied or 0, row.bounced or 0)

    def user_name(self, tenant_id: TenantId, user_id: str) -> Optional[tuple[Optional[str], Optional[str], str]]:
        u = self._session.query(User.first_name, User.last_name, User.email).filter(
            User.user_id == user_id, User.tenant_id == tenant_id).first()
        return (u.first_name, u.last_name, u.email) if u else None

    # ── exports ──

    def campaign_owner(self, tenant_id: TenantId, campaign_id: str) -> tuple[bool, Optional[str]]:
        """(the campaign exists in the tenant, its creator)."""
        row = self._session.query(Campaign.created_by).filter(
            Campaign.campaign_id == campaign_id, Campaign.tenant_id == tenant_id).first()
        return (row is not None, row.created_by if row else None)

    def prospect_export(self, campaign_id: str) -> ProspectExport:
        """Enrolled contacts of a campaign (resolved in the tenant by ``campaign_owner`` first) with
        their send and event counts."""
        s = self._session
        rows = (
            s.query(Prospect.email, Prospect.first_name, Prospect.last_name, Prospect.company_name,
                    Prospect.designation, CampaignProspect.status, CampaignProspect.current_step,
                    CampaignProspect.prospect_id)
            .join(CampaignProspect, CampaignProspect.prospect_id == Prospect.prospect_id)
            .filter(CampaignProspect.campaign_id == campaign_id).all()
        )
        engagement = (
            s.query(EmailMessage.prospect_id,
                    func.sum(case((and_(*sent_message_filters()), 1), else_=0)).label("sent"),
                    func.sum(case((EmailMessage.status == "SENT", 1), else_=0)).label("delivered"))
            .filter(EmailMessage.campaign_id == campaign_id).group_by(EmailMessage.prospect_id).all()
        )
        event_rows = (
            s.query(EmailMessage.prospect_id, EmailEvent.event_type, func.count(EmailEvent.event_id).label("cnt"))
            .join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id)
            .filter(EmailMessage.campaign_id == campaign_id)
            .group_by(EmailMessage.prospect_id, EmailEvent.event_type).all()
        )
        return ProspectExport(list(rows), {r.prospect_id: {"sent": r.sent, "delivered": r.delivered} for r in engagement},
                              _by_event(event_rows))

