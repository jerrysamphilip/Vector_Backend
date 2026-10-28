"""
Background sales jobs (BRD v2.0 5.12):
  recycle_leads()  reopens disqualified leads whose recycle date has come (BR-SF-02)
  stale_deals()    alerts owners about open deals with no activity, or past their close date (BR-SF-05)
Notifications they raise are emailed by notifications.email_pending (BR-SF-07).

Each job runs over the sales repositories in one unit of work on the worker's session and commits
once at the end. ``SalesJobs`` takes the repositories and collaborators so the rules can be tested
with fakes.
"""
from datetime import date, timedelta
from typing import Any, Callable

from app.core.clock import utcnow
from app.domain.sales import idle_days as idle_days_since
from app.domain.sales import stale_reason
from app.repositories.sales import SalesRepositories, SqlSession
from app.services.sales_collaborators import SalesCollaborators, ServiceCollaborators
from app.services.sales_settings import load_settings


class SalesJobs:
    def __init__(self, repos: Any, collab: SalesCollaborators, commit: Callable[[], None]) -> None:
        self.repos = repos
        self.collab = collab
        self.commit = commit

    def recycle_leads(self) -> int:
        now = utcnow()
        due = self.repos.leads.due_for_recycle(now)
        for lead in due:
            if self.repos.leads.other_open_lead_exists(lead.prospect_id, lead.lead_id):
                lead.recycle_at = None  # someone already reopened this contact
                continue
            reason = lead.disqualified_reason
            lead.stage, lead.stage_changed_at, lead.recycle_at = "NEW", now, None
            self.collab.record_changes(lead.tenant_id, "LEAD", lead.lead_id, {"stage": "DISQUALIFIED"},
                                       {"stage": "NEW"}, None, "SYSTEM")
            p = self.repos.lookups.prospect(lead.prospect_id)
            self.collab.notify(lead.tenant_id, [lead.owner_id], "LEAD_RECYCLED",
                               f"Lead back in play: {p.full_name if p else 'contact'}",
                               f"Disqualified earlier ({reason or 'no reason given'}); it's time to try again.",
                               f"/app/leads/{lead.lead_id}")
        self.commit()
        return len(due)

    def stale_deals(self) -> int:
        """One alert per deal per week while it stays stale."""
        sent = 0
        for tenant_id in self.repos.opportunities.tenants_with_open_deals():
            sent += self._stale_for(tenant_id)
        self.commit()
        return sent

    def _stale_for(self, tenant_id: str) -> int:
        days = int(load_settings(self.repos.settings, tenant_id).get("stale_deal_days") or 14)
        today = date.today()
        candidates = self.repos.opportunities.stale_candidates(tenant_id, utcnow() - timedelta(days=days), today)
        touch = self.repos.opportunities.last_activity([o.opportunity_id for o in candidates])
        sent = 0
        for o in candidates:
            idle = idle_days_since(o.updated_at or o.created_at, touch.get(o.opportunity_id), utcnow())
            why = stale_reason(idle, days, o.close_date, today)
            if why is None:
                continue
            sent += self.collab.notify(tenant_id, [o.owner_id], "STALE_DEAL", f"Stale deal: {o.name}",
                                       f"This deal has {why}.", f"/app/deals/{o.opportunity_id}",
                                       dedupe_key=f"stale:{o.opportunity_id}", dedupe_days=7)
        return sent


def _jobs(db: SqlSession) -> SalesJobs:
    return SalesJobs(SalesRepositories(db), ServiceCollaborators(db), db.commit)


def recycle_leads(db: SqlSession) -> int:
    return _jobs(db).recycle_leads()


def stale_deals(db: SqlSession) -> int:
    return _jobs(db).stale_deals()
