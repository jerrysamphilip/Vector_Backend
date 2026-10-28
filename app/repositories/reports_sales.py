"""
Sales reports repository (ADR 0009, chunk A6): the SQL behind GET /sales-reports/* (role dashboard,
funnel, leads, pipeline, forecast, team performance, targets, campaign ROI, SQL-to-win).

Every query is limited to the tenant and, through ``owners`` (None = the whole workspace, a set =
those owners, an empty set = nobody), to the records the service decided the viewer may see. The
report rules (rates, weighting, ranking, masking) stay in app/services/reports_sales_service.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional, Protocol

from sqlalchemy import and_, case, func, literal_column, or_, true
from sqlalchemy.orm import Query, Session

from app.models.campaign import Campaign
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, SalesStage
from app.models.sales_extra import SalesTarget
from app.models.user import User
from app.repositories.base import TenantId
from app.repositories.system_notices import reply_message

__all__ = ["LeadReportRows", "OpportunityCounts", "SalesReportRepository", "Session",
           "SqlAlchemySalesReportRepository", "TeamCounts", "in_owners"]

OwnerSet = Optional[set[str]]
SALES_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")


def in_owners(column: Any, owners: OwnerSet) -> Any:
    """``column`` among the owners; True for the whole workspace, never true for an empty team."""
    return true() if owners is None else column.in_(owners or {"-"})


def _int_map(rows: Iterable[Any]) -> dict[str, int]:
    return {k: int(v or 0) for k, v in rows}


@dataclass(frozen=True)
class OpportunityCounts:
    """The core measures' raw figures for one set of owners and period."""

    new_leads: int
    sqls: int
    open_sqls: int
    opportunities_created: int
    won_count: int
    won_amount: Any
    lost_count: int
    open_by_stage: list[tuple[str, int, Any]]   # (stage_id, open deals, open amount)


@dataclass(frozen=True)
class LeadReportRows:
    by_stage: dict[str, int]
    by_source: list[tuple[str, int, Any]]
    by_owner: list[tuple[str, int, Any, Any, Any]]
    average_days_to_sql: Any
    disqualified: list[tuple[str, int, Any]]


@dataclass(frozen=True)
class TeamCounts:
    """Per rep (user id -> figure) for the scorecard."""

    activities: dict[str, int]
    calls: dict[str, int]
    meetings: dict[str, int]
    tasks_done: dict[str, int]
    emails: dict[str, int]
    leads: dict[str, int]
    sqls: dict[str, int]
    leads_reaching_sql: dict[str, int]
    sqls_converted: dict[str, int]
    opportunities: dict[str, int]
    won: dict[str, tuple[int, Any]]
    lost: dict[str, int]
    open_pipeline: dict[str, Any]


class SalesReportRepository(Protocol):
    def tenant_user(self, tenant_id: TenantId, user_id: str) -> Optional[Any]: ...
    def stage_probabilities(self, tenant_id: TenantId) -> dict[str, int]: ...
    def opportunity_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> OpportunityCounts: ...
    def email_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> tuple[int, int]: ...
    def direct_reports(self, tenant_id: TenantId, user_id: str) -> list[Any]: ...
    def users_at_level(self, tenant_id: TenantId, level: Optional[int]) -> list[Any]: ...
    def sql_leads(self, tenant_id: TenantId, owner_id: str, limit: int) -> list[Any]: ...
    def leads_due(self, tenant_id: TenantId, owner_id: str, before: datetime, limit: int) -> list[Any]: ...
    def deals_closing(self, tenant_id: TenantId, owner_id: str, by: date, limit: int) -> list[Any]: ...
    def lead_report(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> LeadReportRows: ...
    def pipeline_rows(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str]) -> list[tuple]: ...
    def forecast_rows(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str], start: date,
                      end: date) -> list[tuple]: ...
    def target_total(self, tenant_id: TenantId, fy: int, quarters: Sequence[int], owners: OwnerSet) -> Any: ...
    def undated_open(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str]) -> tuple[int, Any]: ...
    def scorecard_users(self, tenant_id: TenantId, owners: OwnerSet) -> list[Any]: ...
    def team_counts(self, tenant_id: TenantId, ids: Sequence[str], start: datetime, end: datetime) -> TeamCounts: ...
    def target_users(self, tenant_id: TenantId, owners: OwnerSet) -> list[Any]: ...
    def targets_by_user(self, tenant_id: TenantId, fy: int, quarters: Sequence[int], ids: Sequence[str]) -> dict[str, Any]: ...
    def target_deal_rows(self, tenant_id: TenantId, ids: Sequence[str], start: date, end: date) -> list[tuple]: ...
    def roi_campaigns(self, tenant_id: TenantId, limit: int) -> list[Any]: ...
    def roi_messages(self, ids: Sequence[str], owners: OwnerSet, start: Optional[datetime],
                     end: Optional[datetime]) -> list[tuple]: ...
    def roi_leads(self, ids: Sequence[str], owners: OwnerSet, start: Optional[datetime],
                  end: Optional[datetime]) -> list[tuple]: ...
    def roi_deals(self, ids: Sequence[str], owners: OwnerSet, start: Optional[datetime],
                  end: Optional[datetime]) -> list[tuple]: ...
    def qualified_leads(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> list[Any]: ...
    def deals_of_leads(self, tenant_id: TenantId, lead_ids: Sequence[str], opportunity_ids: Sequence[str]) -> list[Any]: ...


def _tuples(rows: Iterable[Any]) -> list[tuple]:
    return [tuple(r) for r in rows]


class SqlAlchemySalesReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ── people and stages ──

    def tenant_user(self, tenant_id: TenantId, user_id: str) -> Optional[Any]:
        return self._session.query(User).filter(User.user_id == user_id, User.tenant_id == tenant_id).first()

    def stage_probabilities(self, tenant_id: TenantId) -> dict[str, int]:
        return {s.stage_id: s.probability
                for s in self._session.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)}

    def _active_users(self, tenant_id: TenantId) -> Query[Any]:
        return self._session.query(User).filter(User.tenant_id == tenant_id, User.status != "INACTIVE")

    def direct_reports(self, tenant_id: TenantId, user_id: str) -> list[Any]:
        return self._active_users(tenant_id).filter(User.manager_id == user_id).all()

    def users_at_level(self, tenant_id: TenantId, level: Optional[int]) -> list[Any]:
        """Users at a sales level; level None = everyone with a level."""
        column = User.sales_level
        return self._active_users(tenant_id).filter(column.isnot(None) if level is None else column == level).all()

    # ── core measures ──

    def opportunity_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> OpportunityCounts:
        s = self._session
        leads = s.query(Lead).filter(Lead.tenant_id == tenant_id, in_owners(Lead.owner_id, owners))
        opps = s.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, in_owners(Opportunity.owner_id, owners))
        closed = opps.filter(Opportunity.closed_at >= start, Opportunity.closed_at < end)
        won = closed.filter(Opportunity.status == "WON").with_entities(
            func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).one()
        open_rows = opps.filter(Opportunity.status == "OPEN").with_entities(
            Opportunity.stage_id, func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
        ).group_by(Opportunity.stage_id).all()
        return OpportunityCounts(
            new_leads=leads.filter(Lead.created_at >= start, Lead.created_at < end).count(),
            sqls=leads.filter(Lead.qualified_at >= start, Lead.qualified_at < end).count(),
            open_sqls=leads.filter(Lead.stage == "SQL").count(),
            opportunities_created=opps.filter(Opportunity.created_at >= start, Opportunity.created_at < end).count(),
            won_count=int(won[0] or 0), won_amount=won[1],
            lost_count=closed.filter(Opportunity.status == "LOST").count(),
            open_by_stage=_tuples(open_rows))

    def email_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> tuple[int, int]:
        """(contacts emailed, contacts who replied) in the period."""
        base = self._session.query(EmailMessage).join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id).filter(
            Prospect.tenant_id == tenant_id, in_owners(Prospect.owner_id, owners),
            EmailMessage.sent_at >= start, EmailMessage.sent_at < end)
        distinct = func.count(func.distinct(EmailMessage.prospect_id))
        sent = base.filter(EmailMessage.direction == "OUTBOUND").with_entities(distinct).scalar() or 0
        replied = base.filter(reply_message()).with_entities(distinct).scalar() or 0
        return sent, replied

    # ── the viewer's work queue ──

    def sql_leads(self, tenant_id: TenantId, owner_id: str, limit: int) -> list[Any]:
        return self._session.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.owner_id == owner_id,
                                                Lead.stage == "SQL").order_by(Lead.qualified_at).limit(limit).all()

    def leads_due(self, tenant_id: TenantId, owner_id: str, before: datetime, limit: int) -> list[Any]:
        return self._session.query(Lead).filter(
            Lead.tenant_id == tenant_id, Lead.owner_id == owner_id,
            Lead.stage.in_(("NEW", "CONTACTED", "ENGAGED", "SQL")), Lead.next_step_at <= before
        ).order_by(Lead.next_step_at).limit(limit).all()

    def deals_closing(self, tenant_id: TenantId, owner_id: str, by: date, limit: int) -> list[Any]:
        return self._session.query(Opportunity).filter(
            Opportunity.tenant_id == tenant_id, Opportunity.owner_id == owner_id, Opportunity.status == "OPEN",
            Opportunity.close_date <= by).order_by(Opportunity.close_date).limit(limit).all()

    # ── leads report ──

    def lead_report(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> LeadReportRows:
        base = self._session.query(Lead).filter(Lead.tenant_id == tenant_id, in_owners(Lead.owner_id, owners),
                                                Lead.created_at >= start, Lead.created_at < end)
        qualified = func.sum(case((Lead.qualified_at.isnot(None), 1), else_=0))
        source = func.coalesce(Lead.source, "Unknown")
        reason = func.coalesce(Lead.disqualified_reason, "No reason")
        to_sql = base.filter(Lead.qualified_at.isnot(None)).with_entities(
            func.avg(func.timestampdiff(literal_column("DAY"), Lead.created_at, Lead.qualified_at))).scalar()
        return LeadReportRows(
            by_stage=_int_map(base.with_entities(Lead.stage, func.count(Lead.lead_id)).group_by(Lead.stage)),
            by_source=_tuples(base.with_entities(source, func.count(Lead.lead_id), qualified).group_by(source).all()),
            by_owner=_tuples(base.with_entities(
                Lead.owner_id, func.count(Lead.lead_id), qualified,
                func.sum(case((Lead.stage == "CONVERTED", 1), else_=0)),
                func.sum(case((Lead.stage == "DISQUALIFIED", 1), else_=0)),
            ).group_by(Lead.owner_id).all()),
            average_days_to_sql=to_sql,
            disqualified=_tuples(base.filter(Lead.stage == "DISQUALIFIED").with_entities(
                reason, func.count(Lead.lead_id), func.sum(case((Lead.recycle_at.isnot(None), 1), else_=0))
            ).group_by(reason).all()))

    # ── pipeline and forecast ──

    def _deals(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str]) -> Query[Any]:
        q = self._session.query(Opportunity).filter(Opportunity.tenant_id == tenant_id,
                                                    in_owners(Opportunity.owner_id, owners))
        return q.filter(Opportunity.client_type == client_type) if client_type else q

    def pipeline_rows(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str]) -> list[tuple]:
        """(owner, status, stage, close month, deals, amount) groups."""
        month = func.date_format(Opportunity.close_date, "%Y-%m")
        return _tuples(self._deals(tenant_id, owners, client_type).with_entities(
            Opportunity.owner_id, Opportunity.status, Opportunity.stage_id, month,
            func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
        ).group_by(Opportunity.owner_id, Opportunity.status, Opportunity.stage_id, month).all())

    def forecast_rows(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str], start: date,
                      end: date) -> list[tuple]:
        """(status, stage, forecast category, deals, amount) of deals closing in [start, end)."""
        return _tuples(self._deals(tenant_id, owners, client_type).filter(
            Opportunity.close_date.isnot(None), Opportunity.close_date >= start, Opportunity.close_date < end
        ).with_entities(
            Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category,
            func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
        ).group_by(Opportunity.status, Opportunity.stage_id, Opportunity.forecast_category).all())

    def target_total(self, tenant_id: TenantId, fy: int, quarters: Sequence[int], owners: OwnerSet) -> Any:
        return self._session.query(func.coalesce(func.sum(SalesTarget.amount), 0)).filter(
            SalesTarget.tenant_id == tenant_id, SalesTarget.fy == fy, SalesTarget.quarter.in_(list(quarters)),
            in_owners(SalesTarget.user_id, owners)).scalar()

    def undated_open(self, tenant_id: TenantId, owners: OwnerSet, client_type: Optional[str]) -> tuple[int, Any]:
        row = self._deals(tenant_id, owners, client_type).filter(
            Opportunity.close_date.is_(None), Opportunity.status == "OPEN"
        ).with_entities(func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)).one()
        return row[0], row[1]

    # ── team performance ──

    def scorecard_users(self, tenant_id: TenantId, owners: OwnerSet) -> list[Any]:
        users = self._active_users(tenant_id).filter(User.role.in_(SALES_ROLES))
        if owners is not None:
            users = users.filter(User.user_id.in_(owners or {"-"}))
        return users.order_by(User.sales_level.is_(None), User.sales_level, User.first_name).all()

    def _grouped(self, query: Query[Any], column: Any) -> dict[str, int]:
        return _int_map(query.with_entities(column, func.count()).group_by(column).all())

    def _activity_counts(self, ids: Sequence[str], start: datetime, end: datetime,
                         activity_type: Optional[str] = None) -> dict[str, int]:
        q = self._session.query(ContactActivity).filter(ContactActivity.created_by.in_(ids))
        if activity_type:
            q = q.filter(ContactActivity.activity_type == activity_type)
        q = q.filter(ContactActivity.occurred_at >= start, ContactActivity.occurred_at < end)
        return self._grouped(q, ContactActivity.created_by)

    def team_counts(self, tenant_id: TenantId, ids: Sequence[str], start: datetime, end: datetime) -> TeamCounts:
        s = self._session
        tasks_done = self._grouped(s.query(CrmTask).filter(CrmTask.owner_id.in_(ids), CrmTask.status == "DONE",
                                                           CrmTask.completed_at >= start, CrmTask.completed_at < end),
                                   CrmTask.owner_id)
        emails = _int_map(s.query(Prospect.owner_id, func.count(EmailMessage.message_id))
                          .join(EmailMessage, EmailMessage.prospect_id == Prospect.prospect_id)
                          .filter(Prospect.owner_id.in_(ids), EmailMessage.direction == "OUTBOUND",
                                  EmailMessage.sent_at >= start, EmailMessage.sent_at < end)
                          .group_by(Prospect.owner_id).all())
        lead_q = s.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.owner_id.in_(ids))
        created = lead_q.filter(Lead.created_at >= start, Lead.created_at < end)
        qualified = lead_q.filter(Lead.qualified_at >= start, Lead.qualified_at < end)
        opp_q = s.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, Opportunity.owner_id.in_(ids))
        closed = opp_q.filter(Opportunity.closed_at >= start, Opportunity.closed_at < end)
        won = {o: (int(n), a) for o, n, a in closed.filter(Opportunity.status == "WON").with_entities(
            Opportunity.owner_id, func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
        ).group_by(Opportunity.owner_id).all()}
        open_pipe = {o: a for o, a in opp_q.filter(Opportunity.status == "OPEN").with_entities(
            Opportunity.owner_id, func.coalesce(func.sum(Opportunity.amount), 0)).group_by(Opportunity.owner_id).all()}
        return TeamCounts(
            activities=self._activity_counts(ids, start, end), calls=self._activity_counts(ids, start, end, "CALL"),
            meetings=self._activity_counts(ids, start, end, "MEETING"), tasks_done=tasks_done, emails=emails,
            leads=self._grouped(created, Lead.owner_id), sqls=self._grouped(qualified, Lead.owner_id),
            # Conversion rates follow one cohort so they never exceed 100%
            leads_reaching_sql=self._grouped(created.filter(Lead.qualified_at.isnot(None)), Lead.owner_id),
            sqls_converted=self._grouped(qualified.filter(Lead.converted_at.isnot(None)), Lead.owner_id),
            opportunities=self._grouped(opp_q.filter(Opportunity.created_at >= start, Opportunity.created_at < end),
                                        Opportunity.owner_id),
            won=won, lost=self._grouped(closed.filter(Opportunity.status == "LOST"), Opportunity.owner_id),
            open_pipeline=open_pipe)

    # ── targets ──

    def target_users(self, tenant_id: TenantId, owners: OwnerSet) -> list[Any]:
        users = self._session.query(User).filter(User.tenant_id == tenant_id, User.status == "ACTIVE",
                                                 User.role.in_(SALES_ROLES))
        if owners is not None:
            users = users.filter(User.user_id.in_(owners or {"-"}))
        return users.all()

    def targets_by_user(self, tenant_id: TenantId, fy: int, quarters: Sequence[int], ids: Sequence[str]) -> dict[str, Any]:
        return {uid: a for uid, a in self._session.query(SalesTarget.user_id, func.sum(SalesTarget.amount)).filter(
            SalesTarget.tenant_id == tenant_id, SalesTarget.fy == fy, SalesTarget.quarter.in_(list(quarters)),
            SalesTarget.user_id.in_(list(ids) or ["-"])).group_by(SalesTarget.user_id)}

    def target_deal_rows(self, tenant_id: TenantId, ids: Sequence[str], start: date, end: date) -> list[tuple]:
        """(owner, status, forecast category, deals, amount) of deals closing in [start, end)."""
        return _tuples(self._session.query(
            Opportunity.owner_id, Opportunity.status, Opportunity.forecast_category,
            func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
        ).filter(Opportunity.tenant_id == tenant_id, Opportunity.owner_id.in_(list(ids) or ["-"]),
                 Opportunity.close_date >= start, Opportunity.close_date < end
                 ).group_by(Opportunity.owner_id, Opportunity.status, Opportunity.forecast_category).all())

    # ── campaign ROI ──

    def roi_campaigns(self, tenant_id: TenantId, limit: int) -> list[Any]:
        return self._session.query(Campaign).filter(Campaign.tenant_id == tenant_id, Campaign.status != "DRAFT") \
            .order_by(Campaign.created_at.desc()).limit(limit).all()

    @staticmethod
    def _period(column: Any, start: Optional[datetime], end: Optional[datetime]) -> Any:
        return and_(column >= start, column < end) if start else true()

    def roi_messages(self, ids: Sequence[str], owners: OwnerSet, start: Optional[datetime],
                     end: Optional[datetime]) -> list[tuple]:
        """(campaign, direction, distinct contacts) of the tenant's campaigns ``ids``."""
        return _tuples(self._session.query(
            EmailMessage.campaign_id, EmailMessage.direction, func.count(func.distinct(EmailMessage.prospect_id))
        ).join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id).filter(
            EmailMessage.campaign_id.in_(ids), EmailMessage.sent_at.isnot(None),
            self._period(EmailMessage.sent_at, start, end), in_owners(Prospect.owner_id, owners)
        ).group_by(EmailMessage.campaign_id, EmailMessage.direction).all())

    def roi_leads(self, ids: Sequence[str], owners: OwnerSet, start: Optional[datetime],
                  end: Optional[datetime]) -> list[tuple]:
        return _tuples(self._session.query(
            Lead.campaign_id, func.count(Lead.lead_id), func.sum(case((Lead.qualified_at.isnot(None), 1), else_=0))
        ).filter(Lead.campaign_id.in_(ids), in_owners(Lead.owner_id, owners),
                 self._period(Lead.created_at, start, end)).group_by(Lead.campaign_id).all())

    def roi_deals(self, ids: Sequence[str], owners: OwnerSet, start: Optional[datetime],
                  end: Optional[datetime]) -> list[tuple]:
        return _tuples(self._session.query(
            Opportunity.campaign_id, Opportunity.status, Opportunity.stage_id,
            func.count(Opportunity.opportunity_id), func.coalesce(func.sum(Opportunity.amount), 0)
        ).filter(Opportunity.campaign_id.in_(ids), in_owners(Opportunity.owner_id, owners),
                 self._period(Opportunity.created_at, start, end)
                 ).group_by(Opportunity.campaign_id, Opportunity.status, Opportunity.stage_id).all())

    # ── SQL-to-win ──

    def qualified_leads(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime) -> list[Any]:
        return self._session.query(Lead.lead_id, Lead.owner_id, Lead.opportunity_id, Lead.qualified_at).filter(
            Lead.tenant_id == tenant_id, in_owners(Lead.owner_id, owners),
            Lead.qualified_at >= start, Lead.qualified_at < end).all()

    def deals_of_leads(self, tenant_id: TenantId, lead_ids: Sequence[str], opportunity_ids: Sequence[str]) -> list[Any]:
        """Deals created from the leads, or linked from them."""
        conditions = [Opportunity.lead_id.in_(list(lead_ids))]
        if opportunity_ids:
            conditions.append(Opportunity.opportunity_id.in_(list(opportunity_ids)))
        return self._session.query(Opportunity.opportunity_id, Opportunity.lead_id, Opportunity.status,
                                   Opportunity.amount, Opportunity.closed_at).filter(
            Opportunity.tenant_id == tenant_id, or_(*conditions)).all()
