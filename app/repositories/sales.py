"""
Sales aggregate repositories (ADR 0009, chunk A3): leads, sales stages, opportunities, proposals and
the lookups they need (users, contacts, companies, campaigns, history, timeline).

All SQL of the sales aggregate lives here and in ``sales_quotes.py``, ``sales_admin.py`` and
``sales_forecast.py``. Every call is tenant-scoped; record visibility follows the owner through the
sales hierarchy and arrives as an ``OwnerScope`` the service computed (BR-SH-02). Repositories stage
changes in the session (``add`` / ``flush`` / bulk updates) and never commit.

The worker-side queries (``due_for_recycle``, ``tenants_with_open_deals``) run across workspaces on
purpose: they are the per-workspace loops' entry points.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any, Optional, Protocol

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Query, Session

from app.domain.paging import Page
from app.domain.sales import OPEN_LEAD_STAGES, ListQuery, LeadFilter, OpportunityFilter, OwnerScope
from app.models.account import Account
from app.models.campaign import Campaign
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, Proposal, SalesStage
from app.models.sales_extra import ProposalLine
from app.models.user import User
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId
from app.repositories.sales_admin import SqlAlchemySalesAdminRepository, SqlAlchemySalesSettingsRepository
from app.repositories.sales_common import SessionRepository as _SessionRepository
from app.repositories.sales_common import owner_condition, scoped
from app.repositories.sales_forecast import SqlAlchemyForecastRepository
from app.repositories.sales_quotes import SqlAlchemyQuoteRepository
from app.utils.sql_like import ilike

SqlSession = Session
__all__ = ["BOARD_COLUMN_LIMIT", "SqlSession", "owner_condition", "scoped"]
"""The SQLAlchemy session type, for service modules that still hand a session to unmigrated services
(services may not import sqlalchemy themselves)."""

BOARD_COLUMN_LIMIT = 100  # cards loaded per stage column


# ── Stages ───────────────────────────────────────────────────

class StageRepository(Protocol):
    def exists(self, tenant_id: TenantId) -> bool: ...
    def list(self, tenant_id: TenantId, include_inactive: bool) -> list[SalesStage]: ...
    def get(self, tenant_id: TenantId, stage_id: str) -> Optional[SalesStage]: ...
    def by_ids(self, tenant_id: TenantId, stage_ids: Iterable[str]) -> dict[str, SalesStage]: ...
    def add(self, entity: Any) -> Any: ...


class SqlAlchemyStageRepository(_SessionRepository):
    def exists(self, tenant_id: TenantId) -> bool:
        return self._session.query(SalesStage).filter(SalesStage.tenant_id == tenant_id).first() is not None

    def list(self, tenant_id: TenantId, include_inactive: bool) -> list[SalesStage]:
        query = self._session.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)
        if not include_inactive:
            query = query.filter(SalesStage.active.is_(True))
        return query.order_by(SalesStage.sort_order, SalesStage.name).all()

    def get(self, tenant_id: TenantId, stage_id: str) -> Optional[SalesStage]:
        return self._session.query(SalesStage).filter(SalesStage.stage_id == stage_id,
                                                      SalesStage.tenant_id == tenant_id).first()

    def by_ids(self, tenant_id: TenantId, stage_ids: Iterable[str]) -> dict[str, SalesStage]:
        return {s.stage_id: s for s in self._session.query(SalesStage).filter(
            SalesStage.tenant_id == tenant_id, SalesStage.stage_id.in_(set(stage_ids)))}


# ── Leads ────────────────────────────────────────────────────

class LeadRepository(Protocol):
    def get(self, tenant_id: TenantId, lead_id: str) -> Optional[Lead]: ...
    def get_visible(self, tenant_id: TenantId, scope: OwnerScope, lead_id: str) -> Optional[Lead]: ...
    def page(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter, query: ListQuery) -> Page[Lead]: ...
    def sources(self, tenant_id: TenantId, scope: OwnerScope) -> list[str]: ...
    def board_column(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter,
                     limit: int) -> tuple[list[Lead], int]: ...
    def sql_queue(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter, limit: int) -> list[Lead]: ...
    def open_for_prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Lead]: ...
    def open_prospect_ids(self, tenant_id: TenantId, prospect_ids: Iterable[str]) -> set[str]: ...
    def lock(self, lead_id: str) -> Lead: ...
    def due_for_recycle(self, now: datetime) -> list[Lead]: ...
    def other_open_lead_exists(self, prospect_id: str, lead_id: str) -> bool: ...
    def delete_many(self, lead_ids: Sequence[str]) -> None: ...
    def add(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...
    def claim_version(self, entity: Any, expected: Optional[int], record: str) -> int: ...


class SqlAlchemyLeadRepository(_SessionRepository):
    def _visible(self, tenant_id: TenantId, scope: OwnerScope) -> Query[Lead]:
        return scoped(self._session.query(Lead).filter(Lead.tenant_id == tenant_id), Lead.owner_id, scope)

    def _filtered(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter) -> Query[Lead]:
        query = self._visible(tenant_id, scope)
        if flt.prospect_id:
            query = query.filter(Lead.prospect_id == flt.prospect_id)
        if flt.effective_stages:
            query = query.filter(Lead.stage.in_(flt.effective_stages))
        if flt.owner_id:
            query = query.filter(Lead.owner_id == flt.owner_id)
        if flt.source:
            query = query.filter(Lead.source == flt.source)
        if flt.search:
            term = flt.search
            query = query.join(Prospect, Prospect.prospect_id == Lead.prospect_id).filter(or_(
                ilike(Prospect.first_name, term), ilike(Prospect.last_name, term), ilike(Prospect.email, term),
                ilike(Prospect.company_name, term)))
        return query

    def get(self, tenant_id: TenantId, lead_id: str) -> Optional[Lead]:
        return self._session.query(Lead).filter(Lead.lead_id == lead_id, Lead.tenant_id == tenant_id).first()

    def get_visible(self, tenant_id: TenantId, scope: OwnerScope, lead_id: str) -> Optional[Lead]:
        return self._visible(tenant_id, scope).filter(Lead.lead_id == lead_id).first()

    def page(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter, query: ListQuery) -> Page[Lead]:
        q = self._filtered(tenant_id, scope, flt)
        total = q.count()
        col = getattr(Lead, query.sort.field)
        rows = q.order_by(col.desc() if query.sort.descending else col.asc(), Lead.lead_id) \
            .offset(query.paging.offset).limit(query.paging.limit).all()
        return Page(rows, total, query.paging)

    def sources(self, tenant_id: TenantId, scope: OwnerScope) -> list[str]:
        return [s for (s,) in self._visible(tenant_id, scope).with_entities(Lead.source).distinct() if s]

    def board_column(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter,
                     limit: int) -> tuple[list[Lead], int]:
        q = self._filtered(tenant_id, scope, flt)
        rows = q.order_by(Lead.stage_changed_at.asc()).limit(limit).all()
        return rows, q.count()

    def sql_queue(self, tenant_id: TenantId, scope: OwnerScope, flt: LeadFilter, limit: int) -> list[Lead]:
        return self._filtered(tenant_id, scope, flt).order_by(Lead.qualified_at.asc()).limit(limit).all()

    def open_for_prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Lead]:
        return self._session.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.prospect_id == prospect_id,
                                                Lead.stage.in_(OPEN_LEAD_STAGES)).first()

    def open_prospect_ids(self, tenant_id: TenantId, prospect_ids: Iterable[str]) -> set[str]:
        return {pid for (pid,) in self._session.query(Lead.prospect_id).filter(
            Lead.tenant_id == tenant_id, Lead.prospect_id.in_(set(prospect_ids)), Lead.stage.in_(OPEN_LEAD_STAGES))}

    def lock(self, lead_id: str) -> Lead:
        """Lock the lead row and re-read it (two simultaneous conversions can't both create a deal)."""
        return self._session.query(Lead).filter(Lead.lead_id == lead_id).with_for_update().populate_existing().one()

    def due_for_recycle(self, now: datetime) -> list[Lead]:
        return self._session.query(Lead).filter(Lead.stage == "DISQUALIFIED", Lead.recycle_at.isnot(None),
                                                Lead.recycle_at <= now).all()

    def other_open_lead_exists(self, prospect_id: str, lead_id: str) -> bool:
        return self._session.query(Lead.lead_id).filter(
            Lead.prospect_id == prospect_id, Lead.lead_id != lead_id,
            Lead.stage.in_(OPEN_LEAD_STAGES)).first() is not None

    def delete_many(self, lead_ids: Sequence[str]) -> None:
        """Delete leads; deals converted from them keep going without the link."""
        if not lead_ids:
            return
        s = self._session
        s.query(Opportunity).filter(Opportunity.lead_id.in_(lead_ids)).update(
            {Opportunity.lead_id: None}, synchronize_session=False)
        s.query(PropertyChange).filter(PropertyChange.object_type == "LEAD",
                                       PropertyChange.object_id.in_(lead_ids)).delete(synchronize_session=False)
        s.query(Lead).filter(Lead.lead_id.in_(lead_ids)).delete(synchronize_session=False)


# ── Opportunities ────────────────────────────────────────────

class OpportunityRepository(Protocol):
    def get(self, tenant_id: TenantId, opportunity_id: str) -> Optional[Opportunity]: ...
    def get_visible(self, tenant_id: TenantId, scope: OwnerScope, opportunity_id: str) -> Optional[Opportunity]: ...
    def page(self, tenant_id: TenantId, scope: OwnerScope, flt: OpportunityFilter,
             query: ListQuery) -> tuple[Page[Opportunity], Any]: ...
    def stage_totals(self, tenant_id: TenantId, scope: OwnerScope,
                     flt: OpportunityFilter) -> dict[str, tuple[int, Any]]: ...
    def board_items(self, tenant_id: TenantId, scope: OwnerScope, flt: OpportunityFilter,
                    limit: int) -> list[Opportunity]: ...
    def revenue_rows(self, tenant_id: TenantId, scope: OwnerScope,
                     flt: OpportunityFilter) -> list[tuple[str, str, int, Any]]: ...
    def by_ids(self, tenant_id: TenantId, ids: Iterable[str]) -> list[Opportunity]: ...
    def won_exists_for_account(self, tenant_id: TenantId, account_id: str) -> bool: ...
    def sync_status_with_stage(self, stage_id: str, status: str) -> None: ...
    def last_activity(self, opp_ids: Sequence[str]) -> dict[str, datetime]: ...
    def tenants_with_open_deals(self) -> set[str]: ...
    def stale_candidates(self, tenant_id: TenantId, cutoff: datetime, today: date) -> list[Opportunity]: ...
    def delete_many(self, opportunity_ids: Sequence[str]) -> None: ...
    def add(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...
    def claim_version(self, entity: Any, expected: Optional[int], record: str) -> int: ...


class SqlAlchemyOpportunityRepository(_SessionRepository):
    def _visible(self, tenant_id: TenantId, scope: OwnerScope) -> Query[Opportunity]:
        return scoped(self._session.query(Opportunity).filter(Opportunity.tenant_id == tenant_id),
                      Opportunity.owner_id, scope)

    def filtered(self, tenant_id: TenantId, scope: OwnerScope, flt: OpportunityFilter) -> Query[Opportunity]:
        query = self._visible(tenant_id, scope)
        for column, value in ((Opportunity.prospect_id, flt.prospect_id), (Opportunity.owner_id, flt.owner_id)):
            if value:
                query = query.filter(column == value)
        if flt.statuses:
            query = query.filter(Opportunity.status.in_(flt.statuses))
        for column, value in ((Opportunity.stage_id, flt.stage_id), (Opportunity.client_type, flt.client_type)):
            if value:
                query = query.filter(column == value)
        if flt.close_from:
            query = query.filter(Opportunity.close_date >= flt.close_from)
        if flt.close_to:
            query = query.filter(Opportunity.close_date <= flt.close_to)
        if flt.account_id:
            query = query.filter(Opportunity.account_id == flt.account_id)
        if flt.search:
            term = flt.search
            query = query.outerjoin(Account, Account.account_id == Opportunity.account_id) \
                .filter(or_(ilike(Opportunity.name, term), ilike(Account.name, term)))
        return self._narrow(query, flt)

    @staticmethod
    def _narrow(query: Query[Opportunity], flt: OpportunityFilter) -> Query[Opportunity]:
        if flt.opportunity_id:
            query = query.filter(Opportunity.opportunity_id == flt.opportunity_id)
        if flt.stage_ids is not None:
            query = query.filter(Opportunity.stage_id.in_(flt.stage_ids))
        if flt.stale_before is not None:
            query = query.filter(Opportunity.status == "OPEN", or_(Opportunity.updated_at < flt.stale_before,
                                                                    Opportunity.close_date < flt.today))
        return query

    def get(self, tenant_id: TenantId, opportunity_id: str) -> Optional[Opportunity]:
        return self._session.query(Opportunity).filter(Opportunity.opportunity_id == opportunity_id,
                                                       Opportunity.tenant_id == tenant_id).first()

    def get_visible(self, tenant_id: TenantId, scope: OwnerScope, opportunity_id: str) -> Optional[Opportunity]:
        return self._visible(tenant_id, scope).filter(Opportunity.opportunity_id == opportunity_id).first()

    def page(self, tenant_id: TenantId, scope: OwnerScope, flt: OpportunityFilter,
             query: ListQuery) -> tuple[Page[Opportunity], Any]:
        """One page plus the total amount over every match (count and sum in one query)."""
        q = self.filtered(tenant_id, scope, flt)
        total, amount = q.with_entities(func.count(Opportunity.opportunity_id),
                                        func.coalesce(func.sum(Opportunity.amount), 0)).one()
        col = getattr(Opportunity, query.sort.field)
        rows = q.order_by(col.is_(None), col.desc() if query.sort.descending else col.asc(),
                          Opportunity.opportunity_id).offset(query.paging.offset).limit(query.paging.limit).all()
        return Page(rows, total, query.paging), amount

    def stage_totals(self, tenant_id: TenantId, scope: OwnerScope,
                     flt: OpportunityFilter) -> dict[str, tuple[int, Any]]:
        """(count, amount) per stage in one grouped query, whatever the number of stages (T-5)."""
        grouped = self.filtered(tenant_id, scope, flt).with_entities(
            Opportunity.stage_id, func.count(Opportunity.opportunity_id),
            func.coalesce(func.sum(Opportunity.amount), 0)).group_by(Opportunity.stage_id).all()
        return {stage_id: (count, amount) for stage_id, count, amount in grouped}

    def board_items(self, tenant_id: TenantId, scope: OwnerScope, flt: OpportunityFilter,
                    limit: int) -> list[Opportunity]:
        """The first ``limit`` deals of every stage (soonest close first) in one window query."""
        order = (Opportunity.close_date.is_(None), Opportunity.close_date, Opportunity.opportunity_id)
        ranked = self.filtered(tenant_id, scope, flt).with_entities(
            Opportunity.opportunity_id.label("oid"),
            func.row_number().over(partition_by=Opportunity.stage_id, order_by=order).label("rn"),
        ).subquery()
        return self._session.query(Opportunity).filter(Opportunity.opportunity_id.in_(
            select(ranked.c.oid).where(ranked.c.rn <= limit))).order_by(*order).all()

    def revenue_rows(self, tenant_id: TenantId, scope: OwnerScope,
                     flt: OpportunityFilter) -> list[tuple[str, str, int, Any]]:
        rows = self.filtered(tenant_id, scope, flt).with_entities(
            Opportunity.stage_id, Opportunity.client_type, func.count(Opportunity.opportunity_id),
            func.coalesce(func.sum(Opportunity.amount), 0)).group_by(Opportunity.stage_id, Opportunity.client_type)
        return [(a, b, c, d) for a, b, c, d in rows.all()]

    def by_ids(self, tenant_id: TenantId, ids: Iterable[str]) -> list[Opportunity]:
        return self._session.query(Opportunity).filter(Opportunity.tenant_id == tenant_id,
                                                       Opportunity.opportunity_id.in_(set(ids))).all()

    def ids_query(self, tenant_id: TenantId, scope: OwnerScope, flt: OpportunityFilter) -> Query[Any]:
        return self.filtered(tenant_id, scope, flt).with_entities(Opportunity.opportunity_id)

    def won_exists_for_account(self, tenant_id: TenantId, account_id: str) -> bool:
        return self._session.query(Opportunity.opportunity_id).filter(
            Opportunity.tenant_id == tenant_id, Opportunity.account_id == account_id,
            Opportunity.status == "WON").first() is not None

    def sync_status_with_stage(self, stage_id: str, status: str) -> None:
        """Deals in a stage follow its won/lost flag."""
        self._session.query(Opportunity).filter(Opportunity.stage_id == stage_id, Opportunity.status != status) \
            .update({Opportunity.status: status}, synchronize_session=False)

    def last_activity(self, opp_ids: Sequence[str]) -> dict[str, datetime]:
        """Latest logged activity or task change per deal."""
        if not opp_ids:
            return {}
        out: dict[str, datetime] = {}
        for oid, at in self._session.query(ContactActivity.opportunity_id, func.max(ContactActivity.occurred_at)) \
                .filter(ContactActivity.opportunity_id.in_(opp_ids)).group_by(ContactActivity.opportunity_id):
            out[oid] = at
        for oid, at in self._session.query(CrmTask.opportunity_id, func.max(CrmTask.updated_at)).filter(
                CrmTask.opportunity_id.in_(opp_ids)).group_by(CrmTask.opportunity_id):
            if at and (oid not in out or at > out[oid]):
                out[oid] = at
        return out

    def tenants_with_open_deals(self) -> set[str]:
        return {t for (t,) in self._session.query(Opportunity.tenant_id).filter(Opportunity.status == "OPEN").distinct()}

    def stale_candidates(self, tenant_id: TenantId, cutoff: datetime, today: date) -> list[Opportunity]:
        return self._session.query(Opportunity).filter(
            Opportunity.tenant_id == tenant_id, Opportunity.status == "OPEN",
            or_(Opportunity.updated_at < cutoff, Opportunity.close_date < today)).all()

    def delete_many(self, opportunity_ids: Sequence[str]) -> None:
        """Delete deals and everything hanging off them: proposals and their lines, deal tasks and
        history. Activities stay on the contact's timeline; converted leads go back to SQL."""
        if not opportunity_ids:
            return
        s = self._session
        SqlAlchemyProposalRepository(s).delete_many([pid for (pid,) in s.query(Proposal.proposal_id).filter(
            Proposal.opportunity_id.in_(opportunity_ids))])
        s.query(CrmTask).filter(CrmTask.opportunity_id.in_(opportunity_ids)).delete(synchronize_session=False)
        s.query(ContactActivity).filter(ContactActivity.opportunity_id.in_(opportunity_ids),
                                        ContactActivity.prospect_id.is_(None)).delete(synchronize_session=False)
        s.query(ContactActivity).filter(ContactActivity.opportunity_id.in_(opportunity_ids)).update(
            {ContactActivity.opportunity_id: None}, synchronize_session=False)
        s.query(Lead).filter(Lead.opportunity_id.in_(opportunity_ids)).update(
            {Lead.opportunity_id: None, Lead.stage: "SQL"}, synchronize_session=False)
        s.query(PropertyChange).filter(PropertyChange.object_type == "DEAL",
                                       PropertyChange.object_id.in_(opportunity_ids)).delete(synchronize_session=False)
        s.query(Opportunity).filter(Opportunity.opportunity_id.in_(opportunity_ids)).delete(synchronize_session=False)


# ── Proposals ────────────────────────────────────────────────

class ProposalRepository(Protocol):
    def get(self, tenant_id: TenantId, proposal_id: str) -> Optional[Proposal]: ...
    def for_opportunity(self, opportunity_id: str) -> list[Proposal]: ...
    def line_counts(self, proposal_ids: Sequence[str]) -> dict[str, int]: ...
    def pipeline(self, tenant_id: TenantId, scope: OwnerScope, opps: OpportunityFilter,
                 statuses: Sequence[str], limit: int) -> list[Proposal]: ...
    def delete_many(self, proposal_ids: Sequence[str]) -> None: ...
    def add(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...
    def claim_version(self, entity: Any, expected: Optional[int], record: str) -> int: ...


class SqlAlchemyProposalRepository(_SessionRepository):
    def get(self, tenant_id: TenantId, proposal_id: str) -> Optional[Proposal]:
        return self._session.query(Proposal).filter(Proposal.proposal_id == proposal_id,
                                                    Proposal.tenant_id == tenant_id).first()

    def for_opportunity(self, opportunity_id: str) -> list[Proposal]:
        return self._session.query(Proposal).filter(Proposal.opportunity_id == opportunity_id) \
            .order_by(Proposal.created_at.desc()).all()

    def line_counts(self, proposal_ids: Sequence[str]) -> dict[str, int]:
        if not proposal_ids:
            return {}
        return {pid: int(n) for pid, n in self._session.query(ProposalLine.proposal_id, func.count(ProposalLine.line_id))
                .filter(ProposalLine.proposal_id.in_(proposal_ids)).group_by(ProposalLine.proposal_id)}

    def pipeline(self, tenant_id: TenantId, scope: OwnerScope, opps: OpportunityFilter,
                 statuses: Sequence[str], limit: int) -> list[Proposal]:
        opp_ids = SqlAlchemyOpportunityRepository(self._session).ids_query(tenant_id, scope, opps)
        query = self._session.query(Proposal).filter(Proposal.tenant_id == tenant_id,
                                                     Proposal.opportunity_id.in_(opp_ids))
        if statuses:
            query = query.filter(Proposal.status.in_(statuses))
        return query.order_by(Proposal.created_at.desc()).limit(limit).all()

    def delete_many(self, proposal_ids: Sequence[str]) -> None:
        """Delete proposals (quotes) with their line items."""
        if not proposal_ids:
            return
        self._session.query(ProposalLine).filter(ProposalLine.proposal_id.in_(proposal_ids)) \
            .delete(synchronize_session=False)
        self._session.query(Proposal).filter(Proposal.proposal_id.in_(proposal_ids)).delete(synchronize_session=False)


# ── Lookups the sales screens need ───────────────────────────

class SalesLookupRepository(Protocol):
    def user_names(self, ids: Iterable[Optional[str]]) -> dict[str, str]: ...
    def user(self, tenant_id: TenantId, user_id: str) -> Optional[User]: ...
    def user_exists(self, tenant_id: TenantId, user_id: str) -> bool: ...
    def active_user_ids(self, tenant_id: TenantId) -> set[str]: ...
    def hierarchy_users(self, tenant_id: TenantId) -> list[User]: ...
    def reports_with_level(self, manager_id: str) -> list[User]: ...
    def prospect(self, prospect_id: Optional[str]) -> Optional[Prospect]: ...
    def live_prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]: ...
    def tenant_prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]: ...
    def prospects(self, ids: Iterable[Optional[str]]) -> dict[str, Prospect]: ...
    def prospect_account_id(self, prospect_id: str) -> Optional[str]: ...
    def account(self, account_id: Optional[str]) -> Optional[Account]: ...
    def account_exists(self, tenant_id: TenantId, account_id: str) -> bool: ...
    def account_names(self, ids: Iterable[Optional[str]]) -> dict[str, str]: ...
    def campaign_names(self, ids: Iterable[Optional[str]]) -> dict[str, str]: ...
    def last_campaign_emailed(self, prospect_id: str, before: Optional[datetime]) -> Optional[str]: ...
    def history(self, object_type: str, object_id: str, limit: int) -> list[PropertyChange]: ...
    def message(self, message_id: str) -> Optional[EmailMessage]: ...
    def positive_reply_prospects(self, since: datetime) -> set[str]: ...
    def visible_contacts(self, tenant_id: TenantId, scope: OwnerScope, ids: Iterable[str],
                         limit: int) -> list[Prospect]: ...
    def latest_reply(self, prospect_id: str) -> Optional[EmailMessage]: ...
    def deal_activities(self, opportunity_id: str, prospect_id: Optional[str], since: datetime,
                        limit: int) -> list[ContactActivity]: ...
    def deal_tasks(self, opportunity_id: str, limit: int) -> list[CrmTask]: ...
    def contact_emails(self, prospect_id: str, since: datetime, limit: int) -> list[EmailMessage]: ...
    def add(self, entity: Any) -> Any: ...
    def flush(self) -> None: ...


class SqlAlchemySalesLookupRepository(_SessionRepository):
    def user_names(self, ids: Iterable[Optional[str]]) -> dict[str, str]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {u.user_id: f"{u.first_name} {u.last_name}".strip()
                for u in self._session.query(User.user_id, User.first_name, User.last_name)
                .filter(User.user_id.in_(wanted))}

    def user(self, tenant_id: TenantId, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.user_id == user_id, User.tenant_id == tenant_id).first()

    def user_exists(self, tenant_id: TenantId, user_id: str) -> bool:
        return self._session.query(User.user_id).filter(User.user_id == user_id,
                                                        User.tenant_id == tenant_id).first() is not None

    def active_user_ids(self, tenant_id: TenantId) -> set[str]:
        return {u for (u,) in self._session.query(User.user_id).filter(User.tenant_id == tenant_id,
                                                                       User.status == "ACTIVE")}

    def hierarchy_users(self, tenant_id: TenantId) -> list[User]:
        return self._session.query(User).filter(User.tenant_id == tenant_id, User.status != "INACTIVE") \
            .order_by(User.first_name, User.last_name).all()

    def reports_with_level(self, manager_id: str) -> list[User]:
        return self._session.query(User).filter(User.manager_id == manager_id, User.sales_level.isnot(None)).all()

    def prospect(self, prospect_id: Optional[str]) -> Optional[Prospect]:
        if not prospect_id:
            return None
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id).first()

    def live_prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id,
                                                    Prospect.deleted_at.is_(None)).first()

    def tenant_prospect(self, tenant_id: TenantId, prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def prospects(self, ids: Iterable[Optional[str]]) -> dict[str, Prospect]:
        return {p.prospect_id: p for p in self._session.query(Prospect).filter(
            Prospect.prospect_id.in_({i for i in ids if i}))}

    def prospect_account_id(self, prospect_id: str) -> Optional[str]:
        return self._session.query(Prospect.account_id).filter(Prospect.prospect_id == prospect_id).scalar()

    def account(self, account_id: Optional[str]) -> Optional[Account]:
        if not account_id:
            return None
        return self._session.query(Account).filter(Account.account_id == account_id).first()

    def account_exists(self, tenant_id: TenantId, account_id: str) -> bool:
        return self._session.query(Account.account_id).filter(
            Account.account_id == account_id, Account.tenant_id == tenant_id).first() is not None

    def account_names(self, ids: Iterable[Optional[str]]) -> dict[str, str]:
        return {a.account_id: a.name for a in self._session.query(Account.account_id, Account.name).filter(
            Account.account_id.in_({i for i in ids if i}))}

    def campaign_names(self, ids: Iterable[Optional[str]]) -> dict[str, str]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {c.campaign_id: c.campaign_name for c in self._session.query(
            Campaign.campaign_id, Campaign.campaign_name).filter(Campaign.campaign_id.in_(wanted))}

    def last_campaign_emailed(self, prospect_id: str, before: Optional[datetime]) -> Optional[str]:
        q = self._session.query(EmailMessage.campaign_id).filter(
            EmailMessage.prospect_id == prospect_id, EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.isnot(None), EmailMessage.campaign_id.isnot(None))
        if before:
            q = q.filter(EmailMessage.sent_at <= before)
        row = q.order_by(EmailMessage.sent_at.desc()).first()
        return row[0] if row else None

    def history(self, object_type: str, object_id: str, limit: int) -> list[PropertyChange]:
        return self._session.query(PropertyChange).filter(
            PropertyChange.object_type == object_type, PropertyChange.object_id == object_id
        ).order_by(PropertyChange.changed_at.desc()).limit(limit).all()

    def message(self, message_id: str) -> Optional[EmailMessage]:
        return self._session.query(EmailMessage).filter(EmailMessage.message_id == message_id).first()

    def positive_reply_prospects(self, since: datetime) -> set[str]:
        outbound = self._session.query(EmailEvent.message_id).filter(
            EmailEvent.event_type == EmailEvent.EVENT_POSITIVE_REPLY, EmailEvent.event_time >= since)
        return {pid for (pid,) in self._session.query(EmailMessage.prospect_id).filter(
            EmailMessage.message_id.in_(outbound))}

    def visible_contacts(self, tenant_id: TenantId, scope: OwnerScope, ids: Iterable[str],
                         limit: int) -> list[Prospect]:
        query = self._session.query(Prospect).filter(Prospect.prospect_id.in_(set(ids)), Prospect.deleted_at.is_(None),
                                                     Prospect.tenant_id == tenant_id)
        return scoped(query, Prospect.owner_id, scope).limit(limit).all()

    def latest_reply(self, prospect_id: str) -> Optional[EmailMessage]:
        return self._session.query(EmailMessage).filter(EmailMessage.prospect_id == prospect_id,
                                                        EmailMessage.direction == "INBOUND") \
            .order_by(EmailMessage.sent_at.desc()).first()

    def deal_activities(self, opportunity_id: str, prospect_id: Optional[str], since: datetime,
                        limit: int) -> list[ContactActivity]:
        on_contact = (ContactActivity.prospect_id == prospect_id) if prospect_id else false()
        return self._session.query(ContactActivity).filter(
            or_(ContactActivity.opportunity_id == opportunity_id, on_contact),
            ContactActivity.occurred_at >= since).order_by(ContactActivity.occurred_at.desc()).limit(limit).all()

    def deal_tasks(self, opportunity_id: str, limit: int) -> list[CrmTask]:
        return self._session.query(CrmTask).filter(CrmTask.opportunity_id == opportunity_id) \
            .order_by(CrmTask.created_at.desc()).limit(limit).all()

    def contact_emails(self, prospect_id: str, since: datetime, limit: int) -> list[EmailMessage]:
        return self._session.query(EmailMessage).filter(EmailMessage.prospect_id == prospect_id,
                                                        EmailMessage.sent_at >= since) \
            .order_by(EmailMessage.sent_at.desc()).limit(limit).all()


# ── Unit of work ─────────────────────────────────────────────

class SalesRepositories:
    """Every sales repository bound to one session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.stages = SqlAlchemyStageRepository(session)
        self.leads = SqlAlchemyLeadRepository(session)
        self.opportunities = SqlAlchemyOpportunityRepository(session)
        self.proposals = SqlAlchemyProposalRepository(session)
        self.lookups = SqlAlchemySalesLookupRepository(session)
        self.quotes = SqlAlchemyQuoteRepository(session)
        self.settings = SqlAlchemySalesSettingsRepository(session)
        self.admin = SqlAlchemySalesAdminRepository(session)
        self.forecast = SqlAlchemyForecastRepository(session)


class SqlAlchemySalesUnitOfWork(SqlAlchemyUnitOfWork):
    """``SalesUnitOfWork`` over a SQLAlchemy session: the repositories are bound when the block opens."""

    repos: SalesRepositories

    def __enter__(self) -> SqlAlchemySalesUnitOfWork:
        super().__enter__()
        self.repos = SalesRepositories(self.session)
        return self

    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemySalesUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow
