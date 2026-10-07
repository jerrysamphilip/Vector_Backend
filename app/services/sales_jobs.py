"""
Background sales jobs (BRD v2.0 5.12):
  recycle_leads()  reopens disqualified leads whose recycle date has come (BR-SF-02)
  stale_deals()    alerts owners about open deals with no activity, or past their close date (BR-SF-05)
Notifications they raise are emailed by notifications.email_pending (BR-SF-07).
"""
from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity
from app.services import crm, sales as svc
from app.services.notifications import notify
from app.services.sales_settings import get_settings


def recycle_leads(db: Session) -> int:
    now = datetime.utcnow()
    due = db.query(Lead).filter(Lead.stage == "DISQUALIFIED", Lead.recycle_at.isnot(None), Lead.recycle_at <= now).all()
    for lead in due:
        if db.query(Lead.lead_id).filter(Lead.prospect_id == lead.prospect_id, Lead.lead_id != lead.lead_id,
                                         Lead.stage.in_(("NEW", "CONTACTED", "ENGAGED", "SQL"))).first():
            lead.recycle_at = None  # someone already reopened this contact
            continue
        reason = lead.disqualified_reason
        lead.stage, lead.stage_changed_at, lead.recycle_at = "NEW", now, None
        crm.record_changes(db, lead.tenant_id, "LEAD", lead.lead_id, {"stage": "DISQUALIFIED"}, {"stage": "NEW"}, None, "SYSTEM")
        p = db.query(Prospect).filter(Prospect.prospect_id == lead.prospect_id).first()
        notify(db, lead.tenant_id, [lead.owner_id], "LEAD_RECYCLED",
               f"Lead back in play: {p.full_name if p else 'contact'}",
               f"Disqualified earlier ({reason or 'no reason given'}); it's time to try again.", f"/app/leads/{lead.lead_id}")
    db.commit()
    return len(due)


def stale_deals(db: Session) -> int:
    """One alert per deal per week while it stays stale."""
    sent = 0
    tenants = {t for (t,) in db.query(Opportunity.tenant_id).filter(Opportunity.status == "OPEN").distinct()}
    for tenant_id in tenants:
        days = int(get_settings(db, tenant_id).get("stale_deal_days") or 14)
        cutoff = datetime.utcnow() - timedelta(days=days)
        candidates = db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, Opportunity.status == "OPEN",
                                                  or_(Opportunity.updated_at < cutoff, Opportunity.close_date < date.today())).all()
        touch = svc.last_activity(db, [o.opportunity_id for o in candidates])
        for o in candidates:
            idle = svc.idle_days(o, touch.get(o.opportunity_id))
            overdue = bool(o.close_date and o.close_date < date.today())
            if idle < days and not overdue:
                continue
            why = (f"no activity for {idle} days" if idle >= days else "") + \
                  (" and " if idle >= days and overdue else "") + (f"close date {o.close_date} has passed" if overdue else "")
            sent += notify(db, tenant_id, [o.owner_id], "STALE_DEAL", f"Stale deal: {o.name}", f"This deal has {why}.",
                           f"/app/deals/{o.opportunity_id}", dedupe_key=f"stale:{o.opportunity_id}", dedupe_days=7)
    db.commit()
    return sent
