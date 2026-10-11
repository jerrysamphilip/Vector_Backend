"""
Leads, opportunities and proposals: shared rules (BRD v2.0 5.6, 5.7).
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.account import Account
from app.models.prospect import Prospect
from app.models.sales import (DEFAULT_SALES_STAGES, LEAD_STAGES, OPEN_LEAD_STAGES, SQL_CRITERIA, Lead,
                              Opportunity, SalesStage)
from app.models.user import User
from app.services import crm
from app.services.contact_service import can_see_owner, clean_str, scope

LEAD_FIELDS = ("owner_id", "stage", "source", "next_step", "next_step_at", "disqualified_reason", "recycle_at")
OPP_FIELDS = ("name", "owner_id", "stage_id", "amount", "close_date", "client_type", "closed_reason",
              "next_step", "description", "account_id", "prospect_id", "forecast_category")

FORECAST_CATEGORIES = {"PIPELINE": "Pipeline", "BEST_CASE": "Best case", "COMMIT": "Commit",
                       "CLOSED": "Closed", "OMITTED": "Omitted"}


def category_for(stage) -> str:
    """Default forecast category from the stage (BR-SF-08)."""
    if stage.is_won:
        return "CLOSED"
    if stage.is_lost:
        return "OMITTED"
    if stage.probability >= 70:
        return "COMMIT"
    if stage.probability >= 40:
        return "BEST_CASE"
    return "PIPELINE"


# ── Stages ───────────────────────────────────────────────────

def stages(db: Session, tenant_id: str, include_inactive: bool = False) -> List[SalesStage]:
    """The workspace's sales stages in order, seeding the defaults the first time (BR-SP-01)."""
    query = db.query(SalesStage).filter(SalesStage.tenant_id == tenant_id)
    if not query.first():
        for i, (name, prob, won, lost) in enumerate(DEFAULT_SALES_STAGES):
            db.add(SalesStage(tenant_id=tenant_id, name=name, probability=prob, sort_order=i, is_won=won, is_lost=lost))
        db.commit()
    if not include_inactive:
        query = query.filter(SalesStage.active.is_(True))
    return query.order_by(SalesStage.sort_order, SalesStage.name).all()


def stage_status(stage: SalesStage) -> str:
    return "WON" if stage.is_won else "LOST" if stage.is_lost else "OPEN"


def stage_dict(s: SalesStage) -> dict:
    return {"stage_id": s.stage_id, "name": s.name, "probability": s.probability, "sort_order": s.sort_order,
            "is_won": s.is_won, "is_lost": s.is_lost, "active": s.active, "status": stage_status(s)}


# ── Helpers ──────────────────────────────────────────────────

def money(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Amount must be a number")
    if amount < 0:
        raise HTTPException(status_code=400, detail="Amount cannot be negative")
    return amount.quantize(Decimal("0.01"))


def as_float(value) -> float:
    return float(value) if value is not None else 0.0


def user_names(db: Session, ids) -> Dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.user_id: f"{u.first_name} {u.last_name}".strip()
            for u in db.query(User.user_id, User.first_name, User.last_name).filter(User.user_id.in_(ids))}


def check_assignable(db: Session, user: User, owner_id: str) -> None:
    """Records can be assigned to a workspace user the current user can see (BR-SH-02)."""
    if not owner_id or not db.query(User.user_id).filter(User.user_id == owner_id,
                                                         User.tenant_id == user.tenant_id).first():
        raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")
    if not can_see_owner(db, user, owner_id):
        raise HTTPException(status_code=403, detail="You can only assign records to yourself or your team")


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


def fiscal_year_bounds(fy: int):
    start_month = settings.FISCAL_YEAR_START_MONTH
    start = date(fy, start_month, 1)
    end = date(fy + 1, start_month, 1) if start_month > 1 else date(fy + 1, 1, 1)
    return start, end


# ── Leads ────────────────────────────────────────────────────

def visible_leads(db: Session, user: User):
    query = db.query(Lead).filter(Lead.tenant_id == user.tenant_id)
    return scope(query, db, user, Lead.owner_id)


def get_lead(db: Session, user: User, lead_id: str) -> Lead:
    lead = visible_leads(db, user).filter(Lead.lead_id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def criteria_met(qualification: Optional[dict]) -> List[str]:
    q = qualification or {}
    return [label for key, label in SQL_CRITERIA.items() if not q.get(key)]


def sync_contact_lifecycle(db: Session, prospect: Optional[Prospect], lifecycle: Optional[str], user_id: Optional[str]):
    """Move the contact's lifecycle stage forward to match the lead (never backward)."""
    if not prospect or not lifecycle:
        return
    order = list(crm.LIFECYCLE_STAGES)
    current = prospect.lifecycle_stage
    if current in order and lifecycle in order and order.index(lifecycle) <= order.index(current):
        return
    before = {"lifecycle_stage": current}
    prospect.lifecycle_stage = lifecycle
    crm.record_changes(db, prospect.tenant_id, "CONTACT", prospect.prospect_id, before,
                       {"lifecycle_stage": lifecycle}, user_id, "SYSTEM")


def set_lead_stage(db: Session, lead: Lead, stage: str, user: User) -> None:
    if stage not in LEAD_STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {list(LEAD_STAGES)}")
    if stage == lead.stage:
        return
    if lead.stage == "CONVERTED":
        raise HTTPException(status_code=400, detail="This lead is already converted; work the opportunity instead")
    if stage == "CONVERTED":
        raise HTTPException(status_code=400, detail="Use Convert to create the opportunity")
    if stage == "SQL":
        missing = criteria_met(lead.qualification)
        if missing:
            raise HTTPException(status_code=400, detail="To mark as SQL, confirm: " + ", ".join(missing))
        lead.qualified_at = lead.qualified_at or datetime.utcnow()
    if stage == "DISQUALIFIED" and not clean_str(lead.disqualified_reason):
        raise HTTPException(status_code=400, detail="Give a reason for disqualifying the lead")
    lead.stage = stage
    lead.stage_changed_at = datetime.utcnow()
    prospect = db.query(Prospect).filter(Prospect.prospect_id == lead.prospect_id).first()
    sync_contact_lifecycle(db, prospect, LEAD_STAGES[stage][1], user.user_id)


def lead_dicts(db: Session, leads: List[Lead]) -> List[dict]:
    if not leads:
        return []
    names = user_names(db, [l.owner_id for l in leads])
    contacts = {p.prospect_id: p for p in db.query(Prospect).filter(
        Prospect.prospect_id.in_({l.prospect_id for l in leads}))}
    accounts = {a.account_id: a.name for a in db.query(Account.account_id, Account.name).filter(
        Account.account_id.in_({l.account_id for l in leads if l.account_id}))}
    now = datetime.utcnow()
    out = []
    for l in leads:
        p = contacts.get(l.prospect_id)
        out.append({
            "lead_id": l.lead_id, "prospect_id": l.prospect_id, "account_id": l.account_id,
            "contact_name": p.full_name if p else None, "contact_email": p.email if p else None,
            "contact_title": p.designation if p else None,
            "company_name": accounts.get(l.account_id) or (p.company_name if p else None),
            "owner_id": l.owner_id, "owner_name": names.get(l.owner_id), "source": l.source,
            "stage": l.stage, "stage_label": LEAD_STAGES.get(l.stage, (l.stage,))[0],
            "qualification": l.qualification or {}, "missing_criteria": criteria_met(l.qualification),
            "next_step": l.next_step, "next_step_at": l.next_step_at,
            "next_step_overdue": bool(l.next_step_at and l.next_step_at < now and l.stage in OPEN_LEAD_STAGES),
            "disqualified_reason": l.disqualified_reason, "recycle_at": l.recycle_at, "opportunity_id": l.opportunity_id,
            "campaign_id": l.campaign_id, "created_at": l.created_at, "stage_changed_at": l.stage_changed_at,
            "qualified_at": l.qualified_at, "converted_at": l.converted_at,
            "age_days": max(0, (now - l.created_at).days) if l.created_at else 0,
            "days_in_stage": max(0, (now - l.stage_changed_at).days) if l.stage_changed_at else 0,
            # clock skew between writers can put a timestamp a moment in the future; ages never go below 0
            "sql_age_days": max(0, (now - l.qualified_at).days) if l.qualified_at else None,
        })
    return out


# ── Opportunities ────────────────────────────────────────────

def visible_opps(db: Session, user: User):
    query = db.query(Opportunity).filter(Opportunity.tenant_id == user.tenant_id)
    return scope(query, db, user, Opportunity.owner_id)


def get_opp(db: Session, user: User, opportunity_id: str) -> Opportunity:
    opp = visible_opps(db, user).filter(Opportunity.opportunity_id == opportunity_id).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opp


def default_client_type(db: Session, tenant_id: str, account_id: Optional[str]) -> str:
    """EXISTING when the company has already won a deal with us (BR-SP-04)."""
    if account_id and db.query(Opportunity.opportunity_id).filter(
            Opportunity.tenant_id == tenant_id, Opportunity.account_id == account_id,
            Opportunity.status == "WON").first():
        return "EXISTING"
    return "NEW"


def apply_stage(db: Session, opp: Opportunity, stage_id: str) -> SalesStage:
    stage = db.query(SalesStage).filter(SalesStage.stage_id == stage_id,
                                        SalesStage.tenant_id == opp.tenant_id).first()
    if not stage:
        raise HTTPException(status_code=400, detail="Unknown sales stage")
    status = stage_status(stage)
    # A brand-new deal has no status yet (the column default applies on insert): treat it as open
    if status != "OPEN" and opp.status in (None, "OPEN"):
        opp.closed_at = datetime.utcnow()
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


def require_close_reason(db: Session, opp: Opportunity, stage: SalesStage, reason: Optional[str]) -> None:
    """A won or lost deal records why (BR-SF-05), unless the workspace turned that off."""
    from app.services.sales_settings import get_settings
    if stage_status(stage) != "OPEN" and not clean_str(reason) and get_settings(db, opp.tenant_id).get("require_close_reason", True):
        raise HTTPException(status_code=400, detail=f"Say why the deal was {'won' if stage.is_won else 'lost'}")


def source_campaign(db: Session, prospect_id: Optional[str], before: Optional[datetime] = None) -> Optional[str]:
    """The campaign that most recently emailed the contact (before a date): its ROI gets the deal (BR-SF-11)."""
    if not prospect_id:
        return None
    from app.models.email_message import EmailMessage
    q = db.query(EmailMessage.campaign_id).filter(EmailMessage.prospect_id == prospect_id,
                                                 EmailMessage.direction == "OUTBOUND",
                                                 EmailMessage.sent_at.isnot(None),
                                                 EmailMessage.campaign_id.isnot(None))
    if before:
        q = q.filter(EmailMessage.sent_at <= before)
    row = q.order_by(EmailMessage.sent_at.desc()).first()
    return row[0] if row else None


def auto_owner(db: Session, tenant_id: str, prospect: Optional[Prospect]) -> Optional[str]:
    """Owner for an unowned lead from the workspace's assignment rule (BR-SF-03): region, else round robin."""
    from app.services.sales_settings import get_settings, save_settings
    rule = get_settings(db, tenant_id)["lead_assignment"]
    mode = rule.get("mode") or "off"
    if mode == "off":
        return None
    active = {u for (u,) in db.query(User.user_id).filter(User.tenant_id == tenant_id, User.status == "ACTIVE")}
    if mode == "region" and prospect is not None:
        for r in rule.get("regions") or []:
            value = (getattr(prospect, "poc_country" if r.get("field") == "country" else "poc_state", None) or "").strip().lower()
            if value and value == (r.get("value") or "").strip().lower() and r.get("user_id") in active:
                return r["user_id"]
    pool = [u for u in (rule.get("users") or []) if u in active]
    if not pool:
        return None
    index = int(rule.get("next_index") or 0) % len(pool)
    save_settings(db, tenant_id, {"lead_assignment": {"next_index": index + 1}})
    return pool[index]


def opp_dicts(db: Session, opps: List[Opportunity]) -> List[dict]:
    if not opps:
        return []
    names = user_names(db, [o.owner_id for o in opps])
    stage_map = {s.stage_id: s for s in db.query(SalesStage).filter(
        SalesStage.stage_id.in_({o.stage_id for o in opps}))}
    accounts = {a.account_id: a.name for a in db.query(Account.account_id, Account.name).filter(
        Account.account_id.in_({o.account_id for o in opps if o.account_id}))}
    contacts = {p.prospect_id: p for p in db.query(Prospect).filter(
        Prospect.prospect_id.in_({o.prospect_id for o in opps if o.prospect_id}))}
    today = date.today()
    from app.models.campaign import Campaign
    campaigns = {c.campaign_id: c.campaign_name for c in db.query(Campaign.campaign_id, Campaign.campaign_name).filter(
        Campaign.campaign_id.in_({o.campaign_id for o in opps if o.campaign_id}))} if any(o.campaign_id for o in opps) else {}
    last_touch = last_activity(db, [o.opportunity_id for o in opps])
    from app.services.sales_settings import get_settings
    stale_days = int(get_settings(db, opps[0].tenant_id).get("stale_deal_days") or 14)
    out = []
    for o in opps:
        s = stage_map.get(o.stage_id)
        prob = s.probability if s else 0
        amount = as_float(o.amount)
        p = contacts.get(o.prospect_id)
        out.append({
            "opportunity_id": o.opportunity_id, "name": o.name, "account_id": o.account_id,
            "company_name": accounts.get(o.account_id), "prospect_id": o.prospect_id,
            "contact_name": p.full_name if p else None, "contact_email": p.email if p else None,
            "lead_id": o.lead_id, "owner_id": o.owner_id, "owner_name": names.get(o.owner_id),
            "stage_id": o.stage_id, "stage_name": s.name if s else None, "probability": prob,
            "amount": amount if o.amount is not None else None, "weighted_amount": round(amount * prob / 100, 2),
            "close_date": o.close_date, "client_type": o.client_type, "status": o.status,
            "closed_reason": o.closed_reason, "next_step": o.next_step, "description": o.description,
            "closed_at": o.closed_at, "created_at": o.created_at, "updated_at": o.updated_at,
            "overdue": bool(o.status == "OPEN" and o.close_date and o.close_date < today),
            "forecast_category": o.forecast_category or (category_for(s) if s else "PIPELINE"),
            "forecast_category_manual": bool(o.forecast_category_manual),
            "campaign_id": o.campaign_id, "campaign_name": campaigns.get(o.campaign_id),
            "days_idle": idle_days(o, last_touch.get(o.opportunity_id)),
            "stale": bool(o.status == "OPEN" and (idle_days(o, last_touch.get(o.opportunity_id)) >= stale_days
                                                   or (o.close_date and o.close_date < today))),
        })
    return out


def last_activity(db: Session, opp_ids: List[str]) -> Dict[str, datetime]:
    """Latest logged activity or task change per deal."""
    if not opp_ids:
        return {}
    from app.models.contact_activity import ContactActivity
    from app.models.crm import CrmTask
    out: Dict[str, datetime] = {}
    for oid, at in db.query(ContactActivity.opportunity_id, func.max(ContactActivity.occurred_at)).filter(
            ContactActivity.opportunity_id.in_(opp_ids)).group_by(ContactActivity.opportunity_id):
        out[oid] = at
    for oid, at in db.query(CrmTask.opportunity_id, func.max(CrmTask.updated_at)).filter(
            CrmTask.opportunity_id.in_(opp_ids)).group_by(CrmTask.opportunity_id):
        if at and (oid not in out or at > out[oid]):
            out[oid] = at
    return out


def idle_days(opp: Opportunity, last_touch: Optional[datetime]) -> int:
    latest = max([d for d in (opp.updated_at or opp.created_at, last_touch) if d] or [datetime.utcnow()])
    return max((datetime.utcnow() - latest).days, 0)


def convert_lead(db: Session, user: User, lead: Lead, data: dict) -> Opportunity:
    """SQL → opportunity, carrying contact, company and owner across (BR-LD-06)."""
    # Lock the lead row and re-read it so two simultaneous conversions can't both create a deal
    lead = db.query(Lead).filter(Lead.lead_id == lead.lead_id).with_for_update().populate_existing().one()
    if lead.stage != "SQL":
        raise HTTPException(status_code=400, detail="Only sales-qualified leads (SQL) can be converted")
    prospect = db.query(Prospect).filter(Prospect.prospect_id == lead.prospect_id).first()
    account_id = lead.account_id or (prospect.account_id if prospect else None)
    if not account_id and prospect:
        account = crm.resolve_company(db, prospect.tenant_id, prospect.email, prospect.company_name)
        if account:
            account_id = account.account_id
            prospect.account_id = account_id
    company = db.query(Account).filter(Account.account_id == account_id).first() if account_id else None
    stage_list = stages(db, user.tenant_id)
    stage_id = data.get("stage_id") or next(s.stage_id for s in stage_list if stage_status(s) == "OPEN")
    owner_id = data.get("owner_id") or lead.owner_id
    if owner_id != lead.owner_id:
        check_assignable(db, user, owner_id)
    name = clean_str(data.get("name")) or (
        f"{company.name if company else (prospect.company_name or prospect.full_name)} - {date.today():%b %Y}")
    opp = Opportunity(
        tenant_id=user.tenant_id, name=name[:255], account_id=account_id, prospect_id=lead.prospect_id,
        lead_id=lead.lead_id, owner_id=owner_id, stage_id=stage_id, amount=money(data.get("amount")),
        close_date=data.get("close_date"),
        client_type=data.get("client_type") or default_client_type(db, user.tenant_id, account_id),
        next_step=clean_str(data.get("next_step")) or lead.next_step, created_by=user.user_id,
        campaign_id=lead.campaign_id or source_campaign(db, lead.prospect_id, lead.created_at),
    )
    apply_stage(db, opp, stage_id)
    db.add(opp)
    db.flush()
    lead.stage, lead.opportunity_id = "CONVERTED", opp.opportunity_id
    lead.stage_changed_at = lead.converted_at = datetime.utcnow()
    lead.account_id = account_id
    sync_contact_lifecycle(db, prospect, "OPPORTUNITY", user.user_id)
    crm.record_changes(db, user.tenant_id, "LEAD", lead.lead_id, {"stage": "SQL"}, {"stage": "CONVERTED"},
                       user.user_id, "UI")
    crm.record_changes(db, user.tenant_id, "DEAL", opp.opportunity_id, {"created": None},
                       {"created": "Converted from lead"}, user.user_id, "UI")
    return opp


# ── Delete (dependents first: the database has no ON DELETE CASCADE) ──

def delete_proposals(db: Session, proposal_ids: List[str]) -> None:
    """Delete proposals (quotes) with their line items. Caller commits."""
    if not proposal_ids:
        return
    from app.models.sales import Proposal
    from app.models.sales_extra import ProposalLine
    db.query(ProposalLine).filter(ProposalLine.proposal_id.in_(proposal_ids)).delete(synchronize_session=False)
    db.query(Proposal).filter(Proposal.proposal_id.in_(proposal_ids)).delete(synchronize_session=False)


def delete_opportunities(db: Session, opportunity_ids: List[str]) -> None:
    """Delete deals and everything hanging off them: proposals and their lines, deal tasks and
    history. Activities stay on the contact's timeline; converted leads go back to SQL. Caller commits."""
    if not opportunity_ids:
        return
    from app.models.contact_activity import ContactActivity
    from app.models.crm import CrmTask, PropertyChange
    from app.models.sales import Proposal
    delete_proposals(db, [pid for (pid,) in db.query(Proposal.proposal_id).filter(
        Proposal.opportunity_id.in_(opportunity_ids))])
    db.query(CrmTask).filter(CrmTask.opportunity_id.in_(opportunity_ids)).delete(synchronize_session=False)
    db.query(ContactActivity).filter(ContactActivity.opportunity_id.in_(opportunity_ids),
                                     ContactActivity.prospect_id.is_(None)).delete(synchronize_session=False)
    db.query(ContactActivity).filter(ContactActivity.opportunity_id.in_(opportunity_ids)).update(
        {ContactActivity.opportunity_id: None}, synchronize_session=False)
    db.query(Lead).filter(Lead.opportunity_id.in_(opportunity_ids)).update(
        {Lead.opportunity_id: None, Lead.stage: "SQL"}, synchronize_session=False)
    db.query(PropertyChange).filter(PropertyChange.object_type == "DEAL",
                                    PropertyChange.object_id.in_(opportunity_ids)).delete(synchronize_session=False)
    db.query(Opportunity).filter(Opportunity.opportunity_id.in_(opportunity_ids)).delete(synchronize_session=False)


def delete_leads(db: Session, lead_ids: List[str]) -> None:
    """Delete leads; deals converted from them keep going without the link. Caller commits."""
    if not lead_ids:
        return
    from app.models.crm import PropertyChange
    db.query(Opportunity).filter(Opportunity.lead_id.in_(lead_ids)).update(
        {Opportunity.lead_id: None}, synchronize_session=False)
    db.query(PropertyChange).filter(PropertyChange.object_type == "LEAD",
                                    PropertyChange.object_id.in_(lead_ids)).delete(synchronize_session=False)
    db.query(Lead).filter(Lead.lead_id.in_(lead_ids)).delete(synchronize_session=False)
