"""
Leads, opportunities and proposals: shared rules (BRD v2.0 5.6, 5.7).

``SalesRules`` holds the business rules of the sales aggregate over the repositories
(app/repositories/sales*.py) and the collaborating services (app/services/sales_collaborators.py):
stage transitions, forecast categories, win/loss reasons, owner assignment, conversion, the API
dictionaries. It never commits; the calling service owns the unit of work.

The module-level functions taking ``db`` keep the signatures other modules (reports, tasks,
contacts, seed scripts) call; each binds ``SalesRules`` to that session.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from uuid import UUID

from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.domain import sales as rules
from app.domain.sales import (DEFAULT_SALES_STAGES, FORECAST_CATEGORIES, LEAD_FIELDS, LEAD_STAGES, OPEN_LEAD_STAGES,
                              OPP_FIELDS, category_for, missing_criteria, stage_change_message,
                              stage_change_recipients, stage_status)
from app.models.sales import Opportunity, SalesStage
from app.repositories.sales import SalesRepositories, SqlAlchemySalesUnitOfWork, SqlSession
from app.services import crm
from app.services.contact_service import clean_str
from app.services.sales_collaborators import SalesCollaborators, ServiceCollaborators
from app.services.sales_settings import load_settings, store_settings

__all__ = ["FORECAST_CATEGORIES", "LEAD_FIELDS", "OPP_FIELDS", "SalesRules", "as_float", "category_for",
           "criteria_met", "money", "settings", "stage_change_message", "stage_change_recipients", "stage_status"]

Commit = Callable[[], None]


def stage_dict(s: Any) -> dict:
    return {"stage_id": s.stage_id, "name": s.name, "probability": s.probability, "sort_order": s.sort_order,
            "is_won": s.is_won, "is_lost": s.is_lost, "active": s.active, "status": stage_status(s)}


# ── Pure helpers ─────────────────────────────────────────────

def money(value) -> Optional[Decimal]:
    try:
        return rules.parse_amount(value)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from None


def as_float(value) -> float:
    return float(value) if value is not None else 0.0


def criteria_met(qualification: Optional[dict]) -> List[str]:
    return missing_criteria(qualification)


def fiscal_year_of(d: date) -> int:
    """FY named by the calendar year it starts in (FY 2026 = Apr 2026 - Mar 2027 with an April start)."""
    start = settings.FISCAL_YEAR_START_MONTH
    return d.year if d.month >= start else d.year - 1


def fiscal_quarter_of(d: date) -> int:
    start = settings.FISCAL_YEAR_START_MONTH
    return ((d.month - start) % 12) // 3 + 1


def fiscal_label(fy: int) -> str:
    if settings.FISCAL_YEAR_START_MONTH == 1:
        return f"FY {fy}"
    return f"FY {fy}-{str(fy + 1)[-2:]}"


def fiscal_year_bounds(fy: int) -> Tuple[date, date]:
    start_month = settings.FISCAL_YEAR_START_MONTH
    start = date(fy, start_month, 1)
    end = date(fy + 1, start_month, 1) if start_month > 1 else date(fy + 1, 1, 1)
    return start, end


def idle_days(opp: Any, last_touch: Optional[datetime]) -> int:
    return rules.idle_days(opp.updated_at or opp.created_at, last_touch, utcnow())


def _age(now: datetime, since: Optional[datetime]) -> int:
    # clock skew between writers can put a timestamp a moment in the future; ages never go below 0
    return max(0, (now - since).days) if since else 0


def _lead_dict(lead: Any, contact: Any, company: Optional[str], owner_name: Optional[str], now: datetime) -> dict:
    p = contact
    return {
        "lead_id": lead.lead_id, "row_version": lead.row_version, "prospect_id": lead.prospect_id,
        "account_id": lead.account_id,
        "contact_name": p.full_name if p else None, "contact_email": p.email if p else None,
        "contact_title": p.designation if p else None,
        "company_name": company or (p.company_name if p else None),
        "owner_id": lead.owner_id, "owner_name": owner_name, "source": lead.source,
        "stage": lead.stage, "stage_label": LEAD_STAGES.get(lead.stage, (lead.stage,))[0],
        "qualification": lead.qualification or {}, "missing_criteria": missing_criteria(lead.qualification),
        "next_step": lead.next_step, "next_step_at": lead.next_step_at,
        "next_step_overdue": bool(lead.next_step_at and lead.next_step_at < now and lead.stage in OPEN_LEAD_STAGES),
        "disqualified_reason": lead.disqualified_reason, "recycle_at": lead.recycle_at,
        "opportunity_id": lead.opportunity_id,
        "campaign_id": lead.campaign_id, "created_at": lead.created_at, "stage_changed_at": lead.stage_changed_at,
        "qualified_at": lead.qualified_at, "converted_at": lead.converted_at,
        "age_days": _age(now, lead.created_at),
        "days_in_stage": _age(now, lead.stage_changed_at),
        "sql_age_days": max(0, (now - lead.qualified_at).days) if lead.qualified_at else None,
    }


@dataclass
class _OppContext:
    names: Dict[str, str]
    stages: Dict[str, Any]
    accounts: Dict[str, str]
    contacts: Dict[str, Any]
    campaigns: Dict[str, str]
    last_touch: Dict[str, datetime]
    stale_days: int
    today: date


def _opp_dict(o: Any, ctx: _OppContext) -> dict:
    s = ctx.stages.get(o.stage_id)
    prob = s.probability if s else 0
    amount = as_float(o.amount)
    p = ctx.contacts.get(o.prospect_id)
    idle = idle_days(o, ctx.last_touch.get(o.opportunity_id))
    overdue = bool(o.status == "OPEN" and o.close_date and o.close_date < ctx.today)
    return {
        "opportunity_id": o.opportunity_id, "row_version": o.row_version, "name": o.name, "account_id": o.account_id,
        "company_name": ctx.accounts.get(o.account_id), "prospect_id": o.prospect_id,
        "contact_name": p.full_name if p else None, "contact_email": p.email if p else None,
        "lead_id": o.lead_id, "owner_id": o.owner_id, "owner_name": ctx.names.get(o.owner_id),
        "stage_id": o.stage_id, "stage_name": s.name if s else None, "probability": prob,
        "amount": amount if o.amount is not None else None, "weighted_amount": round(amount * prob / 100, 2),
        "close_date": o.close_date, "client_type": o.client_type, "status": o.status,
        "closed_reason": o.closed_reason, "next_step": o.next_step, "description": o.description,
        "closed_at": o.closed_at, "created_at": o.created_at, "updated_at": o.updated_at,
        "overdue": overdue,
        "forecast_category": o.forecast_category or (category_for(s) if s else "PIPELINE"),
        "forecast_category_manual": bool(o.forecast_category_manual),
        "campaign_id": o.campaign_id, "campaign_name": ctx.campaigns.get(o.campaign_id),
        "days_idle": idle,
        "stale": bool(o.status == "OPEN" and (idle >= ctx.stale_days or (o.close_date and o.close_date < ctx.today))),
    }


class SalesRules:
    """The sales aggregate's rules over its repositories and collaborators (no commits here)."""

    def __init__(self, repos: Any, collab: SalesCollaborators) -> None:
        self.repos = repos
        self.collab = collab

    @classmethod
    def for_session(cls, db: SqlSession) -> "SalesRules":
        return cls(SalesRepositories(db), ServiceCollaborators(db))

    # ── settings ────────────────────────────────────────────
    def settings(self, tenant_id: str) -> dict:
        return load_settings(self.repos.settings, tenant_id)

    # ── stages ──────────────────────────────────────────────
    def stages(self, tenant_id: str, include_inactive: bool = False, commit: Optional[Commit] = None) -> List[Any]:
        """The workspace's sales stages in order, seeding the defaults the first time (BR-SP-01)."""
        if not self.repos.stages.exists(tenant_id):
            for i, (name, prob, won, lost) in enumerate(DEFAULT_SALES_STAGES):
                self.repos.stages.add(SalesStage(tenant_id=tenant_id, name=name, probability=prob, sort_order=i,
                                                is_won=won, is_lost=lost))
            if commit is not None:
                commit()
        return self.repos.stages.list(tenant_id, include_inactive)

    # ── users ───────────────────────────────────────────────
    def user_names(self, ids: Iterable[Optional[str]]) -> Dict[str, str]:
        return self.repos.lookups.user_names(ids)

    def check_assignable(self, user: Any, owner_id: Optional[str]) -> None:
        """Records can be assigned to a workspace user the current user can see (BR-SH-02)."""
        if not owner_id or not self.repos.lookups.user_exists(user.tenant_id, owner_id):
            raise BadRequestError("Owner must be a user in your workspace")
        if not self.collab.can_see_owner(user, owner_id):
            raise PermissionDenied("You can only assign records to yourself or your team")

    # ── leads ───────────────────────────────────────────────
    def get_lead(self, user: Any, lead_id: str) -> Any:
        lead = self.repos.leads.get_visible(user.tenant_id, self.collab.owner_scope(user), lead_id)
        if not lead:
            raise NotFoundError("Lead not found")
        return lead

    def sync_contact_lifecycle(self, prospect: Any, lifecycle: Optional[str], user_id: Optional[str]) -> None:
        """Move the contact's lifecycle stage forward to match the lead (never backward)."""
        if not prospect or not lifecycle:
            return
        current = prospect.lifecycle_stage
        if not rules.lifecycle_moves_forward(list(crm.LIFECYCLE_STAGES), current, lifecycle):
            return
        prospect.lifecycle_stage = lifecycle
        self.collab.record_changes(prospect.tenant_id, "CONTACT", prospect.prospect_id,
                                   {"lifecycle_stage": current}, {"lifecycle_stage": lifecycle}, user_id, "SYSTEM")

    def set_lead_stage(self, lead: Any, stage: str, user: Any) -> None:
        error = rules.lead_stage_error(lead.stage, stage, lead.qualification, clean_str(lead.disqualified_reason))
        if error:
            raise BadRequestError(error)
        if stage == lead.stage:
            return
        if stage == "SQL":
            lead.qualified_at = lead.qualified_at or utcnow()
        lead.stage = stage
        lead.stage_changed_at = utcnow()
        prospect = self.repos.lookups.prospect(lead.prospect_id)
        self.sync_contact_lifecycle(prospect, LEAD_STAGES[stage][1], user.user_id)

    def lead_dicts(self, leads: List[Any]) -> List[dict]:
        if not leads:
            return []
        names = self.user_names([lead.owner_id for lead in leads])
        contacts = self.repos.lookups.prospects({lead.prospect_id for lead in leads})
        accounts = self.repos.lookups.account_names({lead.account_id for lead in leads if lead.account_id})
        now = utcnow()
        return [_lead_dict(lead, contacts.get(lead.prospect_id), accounts.get(lead.account_id),
                           names.get(lead.owner_id), now) for lead in leads]

    def auto_owner(self, tenant_id: str, prospect: Any) -> Optional[str]:
        """Owner for an unowned lead from the workspace's assignment rule (BR-SF-03): region, else round robin."""
        rule = self.settings(tenant_id)["lead_assignment"]
        mode = rule.get("mode") or "off"
        if mode == "off":
            return None
        active = self.repos.lookups.active_user_ids(tenant_id)
        if mode == "region" and prospect is not None:
            for r in rule.get("regions") or []:
                field = "poc_country" if r.get("field") == "country" else "poc_state"
                value = (getattr(prospect, field, None) or "").strip().lower()
                if value and value == (r.get("value") or "").strip().lower() and r.get("user_id") in active:
                    return r["user_id"]
        pool = [u for u in (rule.get("users") or []) if u in active]
        if not pool:
            return None
        index = int(rule.get("next_index") or 0) % len(pool)
        store_settings(self.repos.settings, tenant_id, {"lead_assignment": {"next_index": index + 1}})
        return pool[index]

    # ── opportunities ───────────────────────────────────────
    def get_opp(self, user: Any, opportunity_id: str) -> Any:
        opp = self.repos.opportunities.get_visible(user.tenant_id, self.collab.owner_scope(user), opportunity_id)
        if not opp:
            raise NotFoundError("Opportunity not found")
        return opp

    def default_client_type(self, tenant_id: str, account_id: Optional[str]) -> str:
        """EXISTING when the company has already won a deal with us (BR-SP-04)."""
        if account_id and self.repos.opportunities.won_exists_for_account(tenant_id, account_id):
            return "EXISTING"
        return "NEW"

    def apply_stage(self, opp: Any, stage_id: str) -> Any:
        stage = self.repos.stages.get(opp.tenant_id, stage_id)
        if not stage:
            raise BadRequestError("Unknown sales stage")
        status = stage_status(stage)
        # A brand-new deal has no status yet (the column default applies on insert): treat it as open
        if status != "OPEN" and opp.status in (None, "OPEN"):
            opp.closed_at = utcnow()
            if not opp.close_date:
                opp.close_date = date.today()
        elif status == "OPEN":
            opp.closed_at = None
        opp.stage_id, opp.status = stage.stage_id, status
        # Closed deals always take the closed category; open ones follow the stage unless set by hand
        if status != "OPEN" or not opp.forecast_category_manual:
            opp.forecast_category = category_for(stage)
            if status != "OPEN":
                opp.forecast_category_manual = False
        return stage

    def notify_stage_change(self, opp: Any, stage: Any, actor: Any, previous_stage: Optional[str] = None) -> int:
        """In-app notification (and email, per preference) when a deal is won, lost or changes stage."""
        recipients = stage_change_recipients(opp.owner_id, self.collab.manager_of(opp.owner_id), actor.user_id)
        if not recipients:
            return 0
        actor_name = getattr(actor, "full_name", None) or actor.email
        kind, title, body = stage_change_message(opp.name, stage.name, stage_status(stage), actor_name,
                                                 previous_stage, clean_str(opp.closed_reason))
        return self.collab.notify(opp.tenant_id, recipients, kind, title, body, f"/app/deals/{opp.opportunity_id}")

    def require_close_reason(self, opp: Any, stage: Any, reason: Optional[str]) -> None:
        """A won or lost deal records why (BR-SF-05), unless the workspace turned that off."""
        if stage_status(stage) != "OPEN" and not clean_str(reason) and \
                self.settings(opp.tenant_id).get("require_close_reason", True):
            raise BadRequestError(f"Say why the deal was {'won' if stage.is_won else 'lost'}")

    def source_campaign(self, prospect_id: Optional[str], before: Optional[datetime] = None) -> Optional[str]:
        """The campaign that most recently emailed the contact (before a date): its ROI gets the deal (BR-SF-11)."""
        if not prospect_id:
            return None
        return self.repos.lookups.last_campaign_emailed(prospect_id, before)

    def last_activity(self, opp_ids: List[str]) -> Dict[str, datetime]:
        return self.repos.opportunities.last_activity(opp_ids)

    def opp_dicts(self, opps: List[Any]) -> List[dict]:
        if not opps:
            return []
        lookups = self.repos.lookups
        names = self.user_names([o.owner_id for o in opps])
        stages = self.repos.stages.by_ids(opps[0].tenant_id, {o.stage_id for o in opps})
        accounts = lookups.account_names({o.account_id for o in opps if o.account_id})
        contacts = lookups.prospects({o.prospect_id for o in opps if o.prospect_id})
        campaigns = lookups.campaign_names({o.campaign_id for o in opps if o.campaign_id})
        ctx = _OppContext(names=names, stages=stages, accounts=accounts, contacts=contacts, campaigns=campaigns,
                          last_touch=self.last_activity([o.opportunity_id for o in opps]),
                          stale_days=int(self.settings(opps[0].tenant_id).get("stale_deal_days") or 14),
                          today=date.today())
        return [_opp_dict(o, ctx) for o in opps]

    def convert_lead(self, user: Any, lead: Any, data: dict, commit: Optional[Commit] = None) -> Any:
        """SQL -> opportunity, carrying contact, company and owner across (BR-LD-06)."""
        lead = self.repos.leads.lock(lead.lead_id)
        if lead.stage != "SQL":
            raise BadRequestError("Only sales-qualified leads (SQL) can be converted")
        prospect = self.repos.lookups.prospect(lead.prospect_id)
        account_id = self._convert_account(lead, prospect)
        company = self.repos.lookups.account(account_id) if account_id else None
        stage_list = self.stages(user.tenant_id, commit=commit)
        stage_id = data.get("stage_id") or next(s.stage_id for s in stage_list if stage_status(s) == "OPEN")
        owner_id = data.get("owner_id") or lead.owner_id
        if owner_id != lead.owner_id:
            self.check_assignable(user, owner_id)
        name = clean_str(data.get("name")) or (
            f"{company.name if company else (prospect.company_name or prospect.full_name)} - {date.today():%b %Y}")
        opp = Opportunity(
            tenant_id=user.tenant_id, name=name[:255], account_id=account_id, prospect_id=lead.prospect_id,
            lead_id=lead.lead_id, owner_id=owner_id, stage_id=stage_id, amount=money(data.get("amount")),
            close_date=data.get("close_date"),
            client_type=data.get("client_type") or self.default_client_type(user.tenant_id, account_id),
            next_step=clean_str(data.get("next_step")) or lead.next_step, created_by=user.user_id,
            campaign_id=lead.campaign_id or self.source_campaign(lead.prospect_id, lead.created_at),
        )
        self.apply_stage(opp, stage_id)
        self.repos.opportunities.add(opp)
        self.repos.opportunities.flush()
        lead.stage, lead.opportunity_id = "CONVERTED", opp.opportunity_id
        lead.stage_changed_at = lead.converted_at = utcnow()
        lead.account_id = account_id
        self.sync_contact_lifecycle(prospect, "OPPORTUNITY", user.user_id)
        self.collab.record_changes(user.tenant_id, "LEAD", lead.lead_id, {"stage": "SQL"}, {"stage": "CONVERTED"},
                                   user.user_id, "UI")
        self.collab.record_changes(user.tenant_id, "DEAL", opp.opportunity_id, {"created": None},
                                   {"created": "Converted from lead"}, user.user_id, "UI")
        return opp

    def _convert_account(self, lead: Any, prospect: Any) -> Optional[str]:
        account_id = lead.account_id or (prospect.account_id if prospect else None)
        if not account_id and prospect:
            account = self.collab.resolve_company(prospect.tenant_id, prospect.email, prospect.company_name)
            if account:
                account_id = account.account_id
                prospect.account_id = account_id
        return account_id

    # ── deletes (dependents first: the database has no ON DELETE CASCADE) ──
    def delete_proposals(self, proposal_ids: List[str]) -> None:
        self.repos.proposals.delete_many(proposal_ids)

    def delete_opportunities(self, opportunity_ids: List[str]) -> None:
        self.repos.opportunities.delete_many(opportunity_ids)

    def delete_leads(self, lead_ids: List[str]) -> None:
        self.repos.leads.delete_many(lead_ids)


# ── Base of the router-facing services ───────────────────────

CollaboratorFactory = Callable[[Any], SalesCollaborators]


class SalesServiceBase:
    """One service call = one unit of work (ADR 0009). ``uow`` provides ``repos`` and ``session``
    (handed to the collaborators); tests pass a fake unit of work and fake collaborators."""

    def __init__(self, uow: Any, collaborators: Optional[CollaboratorFactory] = None) -> None:
        self.uow = uow
        self._collaborators: CollaboratorFactory = collaborators or ServiceCollaborators

    @classmethod
    def for_session(cls, db: SqlSession) -> Any:
        return cls(SqlAlchemySalesUnitOfWork.for_session(db))

    @contextmanager
    def work(self) -> Iterator[SalesRules]:
        with self.uow:
            yield SalesRules(self.uow.repos, self._collaborators(self.uow.session))

    def commit(self) -> None:
        self.uow.commit()


# ── Session API (the signatures other modules use) ───────────

def _commit_of(db: SqlSession) -> Commit:
    return db.commit


def stages(db: SqlSession, tenant_id: str, include_inactive: bool = False) -> List[Any]:
    """The workspace's sales stages in order, seeding (and committing) the defaults the first time."""
    return SalesRules.for_session(db).stages(tenant_id, include_inactive, commit=_commit_of(db))


def user_names(db: SqlSession, ids) -> Dict[str, str]:
    return SalesRules.for_session(db).user_names(ids)


def check_assignable(db: SqlSession, user: Any, owner_id: str) -> None:
    SalesRules.for_session(db).check_assignable(user, owner_id)


def get_lead(db: SqlSession, user: Any, lead_id: str) -> Any:
    return SalesRules.for_session(db).get_lead(user, lead_id)


def get_opp(db: SqlSession, user: Any, opportunity_id: str) -> Any:
    return SalesRules.for_session(db).get_opp(user, opportunity_id)


def sync_contact_lifecycle(db: SqlSession, prospect: Any, lifecycle: Optional[str], user_id: Optional[str]) -> None:
    SalesRules.for_session(db).sync_contact_lifecycle(prospect, lifecycle, user_id)


def set_lead_stage(db: SqlSession, lead: Any, stage: str, user: Any) -> None:
    SalesRules.for_session(db).set_lead_stage(lead, stage, user)


def lead_dicts(db: SqlSession, leads: List[Any]) -> List[dict]:
    return SalesRules.for_session(db).lead_dicts(leads)


def opp_dicts(db: SqlSession, opps: List[Any]) -> List[dict]:
    return SalesRules.for_session(db).opp_dicts(opps)


def last_activity(db: SqlSession, opp_ids: List[str]) -> Dict[str, datetime]:
    return SalesRules.for_session(db).last_activity(opp_ids)


def default_client_type(db: SqlSession, tenant_id: str, account_id: Optional[str]) -> str:
    return SalesRules.for_session(db).default_client_type(tenant_id, account_id)


def apply_stage(db: SqlSession, opp: Any, stage_id: str) -> Any:
    return SalesRules.for_session(db).apply_stage(opp, stage_id)


def delete_proposals(db: SqlSession, proposal_ids: List[str]) -> None:
    """Delete proposals (quotes) with their line items. Caller commits."""
    SalesRules.for_session(db).delete_proposals(proposal_ids)


def delete_opportunities(db: SqlSession, opportunity_ids: List[str]) -> None:
    """Delete deals and everything hanging off them. Caller commits."""
    SalesRules.for_session(db).delete_opportunities(opportunity_ids)


def delete_leads(db: SqlSession, lead_ids: List[str]) -> None:
    """Delete leads; deals converted from them keep going without the link. Caller commits."""
    SalesRules.for_session(db).delete_leads(lead_ids)


def entity_id(value: str, missing: str) -> UUID:
    """The UUID a path names, or 404 with the record's usual message (C-7: ids are UUIDs)."""
    parsed = rules.parse_entity_id(value)
    if parsed is None:
        raise NotFoundError(missing)
    return parsed
