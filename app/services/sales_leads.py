"""
Leads (BRD v2.0 5.6, BR-LD-01..06, BR-SF-01/02/03): the list, board and SQL queue, create (also from
a campaign reply), qualification and stage moves, conversion to an opportunity, delete.

``LeadService`` is what app/routers/sales_router.py calls: one method per route, one unit of work per
call. Rules shared with deals live in ``SalesRules`` (app/services/sales.py); SQL lives in
app/repositories/sales.py.
"""
from datetime import datetime, timedelta
from uuid import UUID
from typing import Any, Dict, List, Optional

from app.core.clock import utcnow
from app.core.concurrency import resolve_expected
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.sales import (CLIENT_TYPES, FORECAST_CATEGORIES, LEAD_FIELDS, LEAD_STAGES, OPEN_LEAD_STAGES,
                              PROPOSAL_STATUSES, SQL_CRITERIA, LeadFilter, ListQuery, naive_utc)
from app.models.sales import Lead
from app.services import crm
from app.services.contact_service import clean_str
from app.services.sales import SalesRules, SalesServiceBase, settings, stage_dict
from app.services.sales_settings import amounts_allowed, masked_with

SQL_QUEUE_LIMIT = 500
REPLY_SUGGESTION_DAYS = 30
REPLY_SUGGESTION_LIMIT = 50
HISTORY_LIMIT = 200


def history(rules: SalesRules, object_type: str, object_id: str) -> List[dict]:
    """The record's change history, newest first (BR-SF-04)."""
    rows = rules.repos.lookups.history(object_type, object_id, HISTORY_LIMIT)
    names = rules.user_names([r.changed_by for r in rows])
    return [{"field": r.field, "old_value": r.old_value, "new_value": r.new_value, "changed_at": r.changed_at,
             "changed_by_name": names.get(r.changed_by)} for r in rows]


def check_amount_rights(rules: SalesRules, user: Any, data: dict) -> None:
    if "amount" in data and not amounts_allowed(rules.repos.settings, user):
        raise PermissionDenied("Your role cannot see or change amounts")


def _sql_queue_summary(items: List[dict]) -> dict:
    ages = [i["sql_age_days"] or 0 for i in items]
    by_owner: Dict[str, dict] = {}
    for i in items:
        o = by_owner.setdefault(i["owner_id"], {"owner_id": i["owner_id"], "owner_name": i["owner_name"],
                                               "count": 0, "oldest_days": 0, "no_next_step": 0})
        o["count"] += 1
        o["oldest_days"] = max(o["oldest_days"], i["sql_age_days"] or 0)
        o["no_next_step"] += 0 if i["next_step"] else 1
    return {
        "items": items, "total": len(items),
        "average_age_days": round(sum(ages) / len(ages), 1) if ages else 0,
        "over_14_days": sum(1 for a in ages if a > 14),
        "without_next_step": sum(1 for i in items if not i["next_step"]),
        "overdue_next_step": sum(1 for i in items if i["next_step_overdue"]),
        "by_owner": sorted(by_owner.values(), key=lambda o: -o["count"]),
    }


def _apply_qualification(lead: Any, changes: dict) -> None:
    q = dict(lead.qualification or {})
    for key in SQL_CRITERIA:
        if key in changes:
            q[key] = bool(changes[key])
    if "notes" in changes:
        q["notes"] = clean_str(changes["notes"])
    lead.qualification = q


class LeadService(SalesServiceBase):

    # ── reads ───────────────────────────────────────────────
    def meta(self, user: Any) -> dict:
        with self.work() as rules:
            settings_ = rules.settings(user.tenant_id)
            return {
                "stages": [{"value": k, "label": v[0], "lifecycle": v[1], "open": k in OPEN_LEAD_STAGES}
                           for k, v in LEAD_STAGES.items()],
                "criteria": [{"key": k, "label": v} for k, v in SQL_CRITERIA.items()],
                "client_types": [{"value": k, "label": v} for k, v in CLIENT_TYPES.items()],
                "proposal_statuses": [{"value": k, "label": v} for k, v in PROPOSAL_STATUSES.items()],
                "sales_stages": [stage_dict(s) for s in rules.stages(user.tenant_id, commit=self.commit)],
                "fiscal_year_start_month": settings.FISCAL_YEAR_START_MONTH,
                "sees_everything": rules.collab.sees_everything(user),
                "forecast_categories": [{"value": k, "label": v} for k, v in FORECAST_CATEGORIES.items()],
                "disqualify_reasons": settings_["disqualify_reasons"],
                "default_recycle_days": settings_["default_recycle_days"],
                "win_reasons": settings_["win_reasons"], "loss_reasons": settings_["loss_reasons"],
                "require_close_reason": settings_["require_close_reason"],
                "can_see_amounts": amounts_allowed(rules.repos.settings, user),
            }

    def list_leads(self, user: Any, flt: LeadFilter, query: ListQuery) -> dict:
        with self.work() as rules:
            scope = rules.collab.owner_scope(user)
            page = rules.repos.leads.page(user.tenant_id, scope, flt, query)
            sources = rules.repos.leads.sources(user.tenant_id, scope)
            return {"items": rules.lead_dicts(list(page.items)), "total": page.total, "page": query.paging.page,
                    "page_size": query.paging.page_size, "sources": sorted(sources)}

    def board(self, user: Any, flt: LeadFilter, include_closed: bool, per_stage: int) -> dict:
        """Leads pipeline board by stage (BR-LD-05)."""
        with self.work() as rules:
            scope = rules.collab.owner_scope(user)
            columns = []
            for key, (label, _lifecycle) in LEAD_STAGES.items():
                if not include_closed and key not in OPEN_LEAD_STAGES:
                    continue
                rows, count = rules.repos.leads.board_column(user.tenant_id, scope, flt.with_stage(key), per_stage)
                columns.append({"stage": key, "label": label, "count": count, "items": rules.lead_dicts(rows)})
            return {"columns": columns}

    def sql_queue(self, user: Any, flt: LeadFilter) -> dict:
        """Sales-qualified leads waiting to be converted, oldest first, with age and next step (BR-LD-03/04)."""
        with self.work() as rules:
            rows = rules.repos.leads.sql_queue(user.tenant_id, rules.collab.owner_scope(user), flt.with_stage("SQL"),
                                               SQL_QUEUE_LIMIT)
            return _sql_queue_summary(rules.lead_dicts(rows))

    def reply_suggestions(self, user: Any) -> dict:
        """Recent positive replies from contacts you can see who have no open lead yet (BR-SF-01)."""
        with self.work() as rules:
            lookups = rules.repos.lookups
            replied = lookups.positive_reply_prospects(utcnow() - timedelta(days=REPLY_SUGGESTION_DAYS))
            if not replied:
                return {"items": []}
            open_leads = rules.repos.leads.open_prospect_ids(user.tenant_id, replied)
            contacts = lookups.visible_contacts(user.tenant_id, rules.collab.owner_scope(user), replied - open_leads,
                                                REPLY_SUGGESTION_LIMIT)
            items = []
            for p in contacts:
                reply = lookups.latest_reply(p.prospect_id)
                if reply:
                    items.append({"message_id": reply.message_id, "prospect_id": p.prospect_id,
                                  "contact_name": p.full_name, "company_name": p.company_name,
                                  "subject": reply.subject, "snippet": (reply.body_text or "")[:200],
                                  "received_at": reply.sent_at, "campaign_id": reply.campaign_id})
            return {"items": sorted(items, key=lambda i: i["received_at"] or datetime.min, reverse=True)}

    def detail(self, user: Any, lead_id: UUID | str) -> dict:
        lead_id = str(lead_id)
        with self.work() as rules:
            return self._detail(rules, user, lead_id)

    @staticmethod
    def _detail(rules: SalesRules, user: Any, lead_id: str) -> dict:
        lead = rules.get_lead(user, lead_id)
        result = rules.lead_dicts([lead])[0]
        result["history"] = history(rules, "LEAD", lead.lead_id)
        return result

    # ── writes ──────────────────────────────────────────────
    def create(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            prospect = rules.repos.lookups.live_prospect(user.tenant_id, data["prospect_id"])
            if not prospect or not rules.collab.can_access_contact(user, prospect):
                raise NotFoundError("Contact not found")
            self._refuse_second_open_lead(rules, user, prospect.prospect_id)
            lead = self._make_lead(rules, user, prospect, data.get("owner_id"), clean_str(data.get("source")),
                                   data.get("stage"), clean_str(data.get("next_step")),
                                   naive_utc(data.get("next_step_at")), data.get("campaign_id"))
            self.commit()
            return rules.lead_dicts([lead])[0]

    def create_from_message(self, user: Any, message_id: str, owner_id: Optional[str]) -> dict:
        """One click: turn a positive campaign reply into an engaged lead (BR-SF-01)."""
        with self.work() as rules:
            msg = rules.repos.lookups.message(message_id)
            prospect = rules.repos.lookups.live_prospect(user.tenant_id, msg.prospect_id) if msg else None
            if not msg or not prospect or not rules.collab.can_access_contact(user, prospect):
                raise NotFoundError("Reply not found")
            self._refuse_second_open_lead(rules, user, prospect.prospect_id)
            snippet = (msg.body_text or "").strip().replace("\r", "")[:300]
            lead = self._make_lead(rules, user, prospect, owner_id, "Campaign reply", "ENGAGED",
                                   "Follow up on their reply", utcnow() + timedelta(days=1), msg.campaign_id)
            lead.qualification = {"need": True, "notes": f"Replied: {snippet}" if snippet else None}
            self.commit()
            return rules.lead_dicts([lead])[0]

    @staticmethod
    def _refuse_second_open_lead(rules: SalesRules, user: Any, prospect_id: str) -> None:
        existing = rules.repos.leads.open_for_prospect(user.tenant_id, prospect_id)
        if existing:
            raise ConflictError(detail={"message": "This contact already has an open lead",
                                        "lead_id": existing.lead_id})

    @staticmethod
    def _lead_owner(rules: SalesRules, user: Any, prospect: Any, owner_id: Optional[str]) -> tuple:
        """Owner: as given, else the contact's owner, else the assignment rule (BR-SF-03), else the creator."""
        auto = False
        if not owner_id:
            owner_id = prospect.owner_id
        if not owner_id:
            owner_id = rules.auto_owner(user.tenant_id, prospect)
            auto = bool(owner_id)
        owner_id = owner_id or user.user_id
        if not auto:
            rules.check_assignable(user, owner_id)
        return owner_id, auto

    def _make_lead(self, rules: SalesRules, user: Any, prospect: Any, owner_id: Optional[str], source: Optional[str],
                   stage: Optional[str], next_step: Optional[str] = None, next_step_at: Optional[datetime] = None,
                   campaign_id: Optional[str] = None) -> Any:
        owner_id, auto = self._lead_owner(rules, user, prospect, owner_id)
        lead = Lead(tenant_id=user.tenant_id, prospect_id=prospect.prospect_id, account_id=prospect.account_id,
                    owner_id=owner_id, source=source or prospect.lead_source, stage="NEW", next_step=next_step,
                    next_step_at=next_step_at, campaign_id=campaign_id, created_by=user.user_id)
        rules.repos.leads.add(lead)
        rules.repos.leads.flush()
        if auto and not prospect.owner_id:
            prospect.owner_id = owner_id  # the contact follows its lead's owner
        if stage and stage not in ("NEW", "SQL"):
            rules.set_lead_stage(lead, stage, user)
        rules.sync_contact_lifecycle(prospect, LEAD_STAGES[lead.stage][1], user.user_id)
        rules.collab.record_changes(user.tenant_id, "LEAD", lead.lead_id, {"created": None},
                                    {"created": "Lead created" + (" (auto-assigned)" if auto else "")},
                                    user.user_id, "UI")
        if owner_id != user.user_id:
            rules.collab.notify(user.tenant_id, [owner_id], "ASSIGNED",
                                f"New lead: {prospect.full_name or prospect.email}",
                                f"{'Assigned automatically' if auto else 'Assigned to you'}"
                                f"{' from a campaign reply' if campaign_id else ''}.", f"/app/leads/{lead.lead_id}")
        return lead

    def update(self, user: Any, lead_id: UUID | str, data: dict, if_match: Optional[int]) -> dict:
        lead_id = str(lead_id)
        with self.work() as rules:
            lead = rules.get_lead(user, lead_id)
            rules.repos.leads.claim_version(lead, resolve_expected(data.pop("expected_version", None), if_match),
                                            "lead")  # T-3
            before = crm.snapshot(lead, LEAD_FIELDS)
            self._apply_lead_changes(rules, user, lead, data)
            rules.collab.record_changes(user.tenant_id, "LEAD", lead.lead_id, before, crm.snapshot(lead, LEAD_FIELDS),
                                        user.user_id, "UI")
            self.commit()
            return self._detail(rules, user, lead_id)

    @staticmethod
    def _apply_lead_changes(rules: SalesRules, user: Any, lead: Any, data: dict) -> None:
        if "owner_id" in data:
            if not data["owner_id"]:
                raise BadRequestError("A lead always has an owner")
            rules.check_assignable(user, data["owner_id"])
            lead.owner_id = data["owner_id"]
        for field in ("source", "next_step", "disqualified_reason"):
            if field in data:
                setattr(lead, field, clean_str(data[field]))
        if "next_step_at" in data:
            lead.next_step_at = naive_utc(data["next_step_at"])
        if "qualification" in data:
            _apply_qualification(lead, data["qualification"])
        if data.get("stage"):
            rules.set_lead_stage(lead, data["stage"], user)
        if lead.stage == "DISQUALIFIED" and "recycle_in_days" in data:
            days = data["recycle_in_days"]
            lead.recycle_at = utcnow() + timedelta(days=days) if days else None
        elif lead.stage != "DISQUALIFIED":
            lead.recycle_at = None

    def convert(self, user: Any, lead_id: UUID | str, data: dict) -> dict:
        lead_id = str(lead_id)
        with self.work() as rules:
            lead = rules.get_lead(user, lead_id)
            check_amount_rights(rules, user, data)
            opp = rules.convert_lead(user, lead, data, commit=self.commit)
            self.commit()
            return masked_with(rules.repos.settings, user, rules.opp_dicts([opp])[0])

    def delete(self, user: Any, lead_id: UUID | str) -> dict:
        lead_id = str(lead_id)
        with self.work() as rules:
            lead = rules.get_lead(user, lead_id)
            if lead.stage == "CONVERTED":
                raise BadRequestError("Converted leads are kept with their opportunity")
            if user.role == "AGENT" and lead.owner_id != user.user_id:
                raise PermissionDenied("You can only delete your own leads")
            rules.delete_leads([lead.lead_id])
            self.commit()
            return {"status": "deleted", "lead_id": lead_id}
