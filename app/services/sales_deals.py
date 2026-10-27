"""
Sales stages, opportunities (deals), deal activity and timeline, proposals and the revenue pipeline
(BRD v2.0 5.7, BR-SP-01..04, BR-SF-04/05/06/08/11/12).

``DealService`` is what app/routers/sales_router.py calls for these routes: one method per route,
one unit of work per call. Shared rules (stage moves, forecast categories, close reasons,
notifications) live in ``SalesRules`` (app/services/sales.py); SQL in app/repositories/sales.py.
"""
from datetime import date, datetime, timedelta
from uuid import UUID
from typing import Any, Callable, Dict, List, Optional

from app.core.clock import utcnow
from app.core.concurrency import resolve_expected
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.paging import Sort
from app.domain.sales import (CLIENT_TYPES, FORECAST_CATEGORIES, OPP_FIELDS, PROPOSAL_STATUSES, ListQuery,
                              OpportunityFilter, ProposalFilter, naive_utc, stage_write_error)
from app.models.contact_activity import ACTIVITY_TYPES, ContactActivity
from app.models.sales import Opportunity, Proposal, SalesStage
from app.repositories.sales import BOARD_COLUMN_LIMIT
from app.services import crm
from app.services.contact_service import clean_str
from app.services.quote_approval import QuoteApproval
from app.services.sales import SalesRules, SalesServiceBase, as_float, category_for, money, stage_dict, stage_status
from app.services.sales_leads import check_amount_rights, history
from app.services.sales_settings import amounts_allowed, masked_with

TaskFormatter = Callable[[Any, Dict[str, str]], dict]
"""Maps a CRM task to its API dict (tasks_router.task_dict bound to the request session)."""

TIMELINE_WINDOW_DAYS = 30
PROPOSAL_PIPELINE_LIMIT = 1000


# ── Pure helpers ─────────────────────────────────────────────

def stage_history(opp: Any, changes_newest_first: List[dict], now: datetime) -> List[dict]:
    """Each stage the deal has been in, with how long it stayed (BR-SF-04)."""
    changes = sorted((h for h in changes_newest_first if h["field"] == "stage_id"), key=lambda h: h["changed_at"])
    out = []
    current = changes[0]["old_value"] if changes else None
    start = opp.created_at or now
    for h in changes:
        out.append({"stage": current, "entered_at": start, "left_at": h["changed_at"],
                    "days": round((h["changed_at"] - start).total_seconds() / 86400, 1)})
        current, start = h["new_value"], h["changed_at"]
    out.append({"stage": current, "entered_at": start, "left_at": None,
                "days": round(((opp.closed_at or now) - start).total_seconds() / 86400, 1)})
    return [o for o in out if o["stage"]]


def proposal_dict(p: Any, opp: Optional[dict] = None) -> dict:
    d = {"proposal_id": p.proposal_id, "row_version": p.row_version, "opportunity_id": p.opportunity_id,
         "title": p.title, "amount": as_float(p.amount) if p.amount is not None else None, "status": p.status,
         "status_label": PROPOSAL_STATUSES.get(p.status), "valid_until": p.valid_until, "notes": p.notes,
         "sent_at": p.sent_at, "decided_at": p.decided_at, "created_at": p.created_at,
         "approval_status": p.approval_status or "NOT_REQUIRED", "approval_reason": p.approval_reason,
         "approval_decided_at": p.approval_decided_at}
    if opp:
        d.update({"opportunity_name": opp["name"], "company_name": opp["company_name"],
                  "owner_id": opp["owner_id"], "owner_name": opp["owner_name"], "client_type": opp["client_type"]})
    return d


def set_proposal_status(p: Any, status: str) -> None:
    if status not in PROPOSAL_STATUSES:
        raise BadRequestError(f"status must be one of {list(PROPOSAL_STATUSES)}")
    if status != "DRAFT" and not p.sent_at:
        p.sent_at = utcnow()
    p.decided_at = utcnow() if status in ("ACCEPTED", "REJECTED") else None
    p.status = status


def _empty_revenue_totals() -> dict:
    return {"open_count": 0, "open_amount": 0.0, "weighted_amount": 0.0,
            "won_count": 0, "won_amount": 0.0, "lost_count": 0, "lost_amount": 0.0,
            "by_client_type": {"NEW": {"amount": 0.0, "weighted": 0.0},
                               "EXISTING": {"amount": 0.0, "weighted": 0.0}}}


def _revenue_by_stage(rows: List[tuple]) -> Dict[str, dict]:
    by_stage: Dict[str, dict] = {}
    for stage_id, ctype, n, amount in rows:
        s = by_stage.setdefault(stage_id, {"count": 0, "amount": 0.0, "NEW": 0.0, "EXISTING": 0.0})
        s["count"] += n
        s["amount"] += as_float(amount)
        s[ctype if ctype in ("NEW", "EXISTING") else "NEW"] += as_float(amount)
    return by_stage


def _add_to_totals(totals: dict, status: str, s: dict, weighted: float, probability: int) -> None:
    if status == "OPEN":
        totals["open_count"] += s["count"]
        totals["open_amount"] += s["amount"]
        totals["weighted_amount"] += weighted
        for ct in ("NEW", "EXISTING"):
            totals["by_client_type"][ct]["amount"] += s[ct]
            totals["by_client_type"][ct]["weighted"] += s[ct] * probability / 100
    elif status == "WON":
        totals["won_count"] += s["count"]
        totals["won_amount"] += s["amount"]
    else:
        totals["lost_count"] += s["count"]
        totals["lost_amount"] += s["amount"]


def revenue_pipeline(stage_list: List[Any], rows: List[tuple]) -> dict:
    """Open pipeline by stage, unweighted and weighted, plus won and lost (BR-SP-03/04)."""
    by_stage = _revenue_by_stage(rows)
    stages_out, totals = [], _empty_revenue_totals()
    for stage in stage_list:
        s = by_stage.get(stage.stage_id)
        if not s and not stage.active:
            continue
        s = s or {"count": 0, "amount": 0.0, "NEW": 0.0, "EXISTING": 0.0}
        status = stage_status(stage)
        weighted = round(s["amount"] * stage.probability / 100, 2) if status == "OPEN" else 0.0
        stages_out.append({**stage_dict(stage), "count": s["count"], "amount": round(s["amount"], 2),
                           "weighted": weighted, "new_amount": round(s["NEW"], 2),
                           "existing_amount": round(s["EXISTING"], 2)})
        _add_to_totals(totals, status, s, weighted, stage.probability)
    for k in ("open_amount", "weighted_amount", "won_amount", "lost_amount"):
        totals[k] = round(totals[k], 2)
    for ct in totals["by_client_type"].values():
        ct["amount"], ct["weighted"] = round(ct["amount"], 2), round(ct["weighted"], 2)
    return {"stages": stages_out, "totals": totals}


def _proposal_columns(items: List[dict]) -> List[dict]:
    columns = []
    for key, label in PROPOSAL_STATUSES.items():
        col = [i for i in items if i["status"] == key]
        columns.append({"status": key, "label": label, "count": len(col),
                        "amount": round(sum(i["amount"] or 0 for i in col), 2), "items": col})
    return columns


def _activity_item(a: Any, opp_id: str, names: Dict[str, str], mentioned: Dict[str, list]) -> dict:
    return {"kind": a.activity_type, "at": a.occurred_at, "activity": {
        "mentions": mentioned.get(a.activity_id, []),
        "activity_id": a.activity_id, "activity_type": a.activity_type, "subject": a.subject, "body": a.body,
        "outcome": a.outcome, "duration_minutes": a.duration_minutes, "source": a.source,
        "created_by_name": names.get(a.created_by), "on_deal": a.opportunity_id == opp_id}}


def _email_item(m: Any) -> dict:
    return {"kind": "EMAIL_RECEIVED" if m.direction == "INBOUND" else "EMAIL_SENT", "at": m.sent_at,
            "email": {"message_id": m.message_id, "subject": m.subject, "snippet": (m.body_text or "")[:300],
                      "status": m.status, "final_status": m.final_status}}


class DealService(SalesServiceBase):

    # ── stages (BR-SP-01) ───────────────────────────────────
    def list_stages(self, user: Any, include_inactive: bool) -> List[dict]:
        with self.work() as rules:
            return [stage_dict(s) for s in rules.stages(user.tenant_id, include_inactive, commit=self.commit)]

    def create_stage(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            error = stage_write_error(data)
            if error:
                raise BadRequestError(error)
            name = clean_str(data.get("name"))
            if not name:
                raise BadRequestError("Give the stage a name")
            existing = rules.stages(user.tenant_id, True, commit=self.commit)
            if any((s.name or "").lower() == name.lower() for s in existing):
                raise ConflictError(f"A stage called {name} already exists")
            stage = SalesStage(tenant_id=user.tenant_id, name=name, probability=data.get("probability") or 0,
                               sort_order=data.get("sort_order", max((s.sort_order for s in existing), default=0) + 1),
                               is_won=bool(data.get("is_won")), is_lost=bool(data.get("is_lost")))
            if stage.is_won:
                stage.probability = 100
            if stage.is_lost:
                stage.probability = 0
            rules.repos.stages.add(stage)
            self.commit()
            return stage_dict(stage)

    def update_stage(self, user: Any, stage_id: UUID | str, data: dict) -> dict:
        stage_id = str(stage_id)
        with self.work() as rules:
            stage = rules.repos.stages.get(user.tenant_id, stage_id)
            if not stage:
                raise NotFoundError("Stage not found")
            error = stage_write_error({**stage_dict(stage), **data})
            if error:
                raise BadRequestError(error)
            if "name" in data:
                self._rename_stage(rules, user, stage, data["name"])
            for f in ("probability", "sort_order", "is_won", "is_lost", "active"):
                if f in data and data[f] is not None:
                    setattr(stage, f, data[f])
            if data.get("active") is False and not any(
                    s.active for s in rules.stages(user.tenant_id, True, commit=self.commit)
                    if s.stage_id != stage.stage_id and stage_status(s) == "OPEN"):
                raise BadRequestError("Keep at least one open stage active")
            # Deals in this stage follow its won/lost flag
            rules.repos.opportunities.sync_status_with_stage(stage.stage_id, stage_status(stage))
            self.commit()
            return stage_dict(stage)

    def _rename_stage(self, rules: SalesRules, user: Any, stage: Any, raw_name: Any) -> None:
        new_name = clean_str(raw_name) or stage.name
        if any(s.stage_id != stage.stage_id and (s.name or "").lower() == new_name.lower()
               for s in rules.stages(user.tenant_id, True, commit=self.commit)):
            raise ConflictError(f"A stage called {new_name} already exists")
        stage.name = new_name

    # ── opportunities ───────────────────────────────────────
    def list_opportunities(self, user: Any, flt: OpportunityFilter, stale: bool, query: ListQuery) -> dict:
        with self.work() as rules:
            if stale:
                days = int(rules.settings(user.tenant_id).get("stale_deal_days") or 14)
                flt = flt.replace(stale_before=utcnow() - timedelta(days=days), today=date.today())
            if query.sort.field == "amount" and not amounts_allowed(rules.repos.settings, user):
                # sorting by a hidden amount would reveal it (BR-SF-12)
                query = ListQuery(Sort("close_date", query.sort.direction), query.paging)
            page, amount = rules.repos.opportunities.page(user.tenant_id, rules.collab.owner_scope(user), flt, query)
            return masked_with(rules.repos.settings, user, {
                "items": rules.opp_dicts(list(page.items)), "total": page.total, "total_amount": as_float(amount),
                "page": query.paging.page, "page_size": query.paging.page_size})

    def board(self, user: Any, flt: OpportunityFilter, include_closed: bool) -> dict:
        with self.work() as rules:
            shown = [s for s in rules.stages(user.tenant_id, commit=self.commit)
                     if include_closed or stage_status(s) == "OPEN"]
            flt = flt.replace(stage_ids=tuple(s.stage_id for s in shown))
            scope = rules.collab.owner_scope(user)
            opps = rules.repos.opportunities
            totals = opps.stage_totals(user.tenant_id, scope, flt)
            items: Dict[str, List[dict]] = {}
            for item in rules.opp_dicts(opps.board_items(user.tenant_id, scope, flt, BOARD_COLUMN_LIMIT)):
                items.setdefault(item["stage_id"], []).append(item)
            columns = []
            for stage in shown:
                count, total = totals.get(stage.stage_id, (0, 0.0))
                total = as_float(total)
                columns.append({**stage_dict(stage), "count": count, "amount": total,
                                "weighted": round(total * stage.probability / 100, 2),
                                "items": items.get(stage.stage_id, [])})
            return masked_with(rules.repos.settings, user, {"columns": columns})

    @staticmethod
    def _check_links(rules: SalesRules, user: Any, data: dict) -> None:
        if data.get("client_type") and data["client_type"] not in CLIENT_TYPES:
            raise BadRequestError("client_type must be NEW or EXISTING")
        if data.get("account_id") and not rules.repos.lookups.account_exists(user.tenant_id, data["account_id"]):
            raise BadRequestError("Company not found")
        if data.get("prospect_id"):
            p = rules.repos.lookups.tenant_prospect(user.tenant_id, data["prospect_id"])
            if not p or not rules.collab.can_access_contact(user, p):
                raise BadRequestError("Contact not found")

    def create_opportunity(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            name = clean_str(data.get("name"))
            if not name:
                raise BadRequestError("Give the opportunity a name")
            self._check_links(rules, user, data)
            check_amount_rights(rules, user, data)
            owner_id = data.get("owner_id") or user.user_id
            rules.check_assignable(user, owner_id)
            stage_list = rules.stages(user.tenant_id, commit=self.commit)
            account_id = data.get("account_id")
            if not account_id and data.get("prospect_id"):
                account_id = rules.repos.lookups.prospect_account_id(data["prospect_id"])
            opp = Opportunity(
                tenant_id=user.tenant_id, name=name[:255], account_id=account_id, prospect_id=data.get("prospect_id"),
                owner_id=owner_id, stage_id=data.get("stage_id") or stage_list[0].stage_id,
                amount=money(data.get("amount")), close_date=data.get("close_date"),
                client_type=data.get("client_type") or rules.default_client_type(user.tenant_id, account_id),
                next_step=clean_str(data.get("next_step")), description=data.get("description"),
                closed_reason=clean_str(data.get("closed_reason")), created_by=user.user_id,
                campaign_id=rules.source_campaign(data.get("prospect_id")))
            stage = rules.apply_stage(opp, opp.stage_id)
            rules.require_close_reason(opp, stage, opp.closed_reason)
            if data.get("forecast_category") in FORECAST_CATEGORIES and opp.status == "OPEN":
                opp.forecast_category, opp.forecast_category_manual = data["forecast_category"], True
            rules.repos.opportunities.add(opp)
            rules.repos.opportunities.flush()
            rules.collab.record_changes(user.tenant_id, "DEAL", opp.opportunity_id, {"created": None},
                                        {"created": "Opportunity created"}, user.user_id, "UI")
            self.commit()
            return masked_with(rules.repos.settings, user, rules.opp_dicts([opp])[0])

    def detail(self, user: Any, opportunity_id: UUID | str) -> dict:
        opportunity_id = str(opportunity_id)
        with self.work() as rules:
            return self._detail(rules, user, opportunity_id)

    @staticmethod
    def _detail(rules: SalesRules, user: Any, opportunity_id: str) -> dict:
        opp = rules.get_opp(user, opportunity_id)
        result = rules.opp_dicts([opp])[0]
        result["history"] = history(rules, "DEAL", opp.opportunity_id)
        result["proposals"] = [proposal_dict(p) for p in rules.repos.proposals.for_opportunity(opp.opportunity_id)]
        lead = rules.repos.leads.get(opp.tenant_id, opp.lead_id) if opp.lead_id else None
        result["lead"] = rules.lead_dicts([lead])[0] if lead else None
        result["stage_history"] = stage_history(opp, result["history"], utcnow())
        counts = rules.repos.proposals.line_counts([p["proposal_id"] for p in result["proposals"]])
        for p in result["proposals"]:
            p["line_count"] = counts.get(p["proposal_id"], 0)
        if not amounts_allowed(rules.repos.settings, user):
            result["history"] = [h for h in result["history"] if h["field"] != "amount"]
        return masked_with(rules.repos.settings, user, result)

    def update_opportunity(self, user: Any, opportunity_id: UUID | str, data: dict, if_match: Optional[int]) -> dict:
        """Edit a deal, including board drag-and-drop stage moves (stage_id) (T-3 versioned)."""
        opportunity_id = str(opportunity_id)
        with self.work() as rules:
            opp = rules.get_opp(user, opportunity_id)
            rules.repos.opportunities.claim_version(
                opp, resolve_expected(data.pop("expected_version", None), if_match), "opportunity")
            self._check_links(rules, user, data)
            check_amount_rights(rules, user, data)
            before = crm.snapshot(opp, OPP_FIELDS)
            self._apply_fields(rules, user, opp, data)
            self._apply_forecast_category(rules, opp, data)
            moved_to = self._move_stage(rules, user, opp, data)
            after = crm.snapshot(opp, OPP_FIELDS)
            if before.get("stage_id") != after.get("stage_id"):
                names = {s.stage_id: s.name for s in rules.stages(user.tenant_id, True, commit=self.commit)}
                before["stage_id"], after["stage_id"] = names.get(before["stage_id"]), names.get(after["stage_id"])
            if moved_to is not None:
                # Owner and their manager hear about won / lost / stage moves (E2E-J1-87b)
                rules.notify_stage_change(opp, moved_to, user, before.get("stage_id"))
            rules.collab.record_changes(user.tenant_id, "DEAL", opp.opportunity_id, before, after, user.user_id, "UI")
            self.commit()
            return self._detail(rules, user, opportunity_id)

    @staticmethod
    def _apply_fields(rules: SalesRules, user: Any, opp: Any, data: dict) -> None:
        if "owner_id" in data:
            if not data["owner_id"]:
                raise BadRequestError("An opportunity always has an owner")
            rules.check_assignable(user, data["owner_id"])
            opp.owner_id = data["owner_id"]
        if "name" in data:
            opp.name = clean_str(data["name"]) or opp.name
        if "amount" in data:
            opp.amount = money(data["amount"])
        for f in ("close_date", "client_type", "account_id", "prospect_id", "description"):
            if f in data:
                setattr(opp, f, data[f] if data[f] != "" else None)
        for f in ("closed_reason", "next_step"):
            if f in data:
                setattr(opp, f, clean_str(data[f]))

    @staticmethod
    def _apply_forecast_category(rules: SalesRules, opp: Any, data: dict) -> None:
        if "forecast_category" not in data:
            return
        if data["forecast_category"] and data["forecast_category"] not in FORECAST_CATEGORIES:
            raise BadRequestError("Unknown forecast category")
        if data["forecast_category"]:
            opp.forecast_category, opp.forecast_category_manual = data["forecast_category"], True
        else:  # back to automatic
            opp.forecast_category_manual = False
            current = rules.repos.stages.get(opp.tenant_id, opp.stage_id)
            opp.forecast_category = category_for(current) if current else "PIPELINE"

    @staticmethod
    def _move_stage(rules: SalesRules, user: Any, opp: Any, data: dict) -> Any:
        if not data.get("stage_id") or data["stage_id"] == opp.stage_id:
            return None
        stage = rules.apply_stage(opp, data["stage_id"])
        rules.require_close_reason(opp, stage, opp.closed_reason)
        if stage.is_won and opp.prospect_id:
            prospect = rules.repos.lookups.prospect(opp.prospect_id)
            rules.sync_contact_lifecycle(prospect, "CUSTOMER", user.user_id)
        return stage

    def delete_opportunity(self, user: Any, opportunity_id: UUID | str) -> dict:
        opportunity_id = str(opportunity_id)
        with self.work() as rules:
            opp = rules.get_opp(user, opportunity_id)
            if user.role == "AGENT":
                raise PermissionDenied("Agents cannot delete opportunities")
            rules.delete_opportunities([opp.opportunity_id])  # quotes, lines, tasks, history first (FKs)
            self.commit()
            return {"status": "deleted", "opportunity_id": opportunity_id}

    # ── deal activity and timeline (BR-SF-06) ───────────────
    def log_activity(self, user: Any, opportunity_id: UUID | str, data: dict) -> dict:
        opportunity_id = str(opportunity_id)
        with self.work() as rules:
            opp = rules.get_opp(user, opportunity_id)
            if data["activity_type"] not in ACTIVITY_TYPES:
                raise BadRequestError(f"activity_type must be one of {list(ACTIVITY_TYPES)}")
            if not (clean_str(data.get("subject")) or clean_str(data.get("body"))):
                raise BadRequestError("Add a subject or some notes")
            if data.get("mentioned_user_ids") and data["activity_type"] != "NOTE":
                raise BadRequestError("You can only mention people in notes")
            mentioned_users = rules.collab.validate_mentions(user, data.get("mentioned_user_ids"), opp.owner_id)
            a = ContactActivity(tenant_id=user.tenant_id, prospect_id=opp.prospect_id,
                                opportunity_id=opp.opportunity_id, activity_type=data["activity_type"],
                                subject=clean_str(data.get("subject")), body=data.get("body"),
                                outcome=clean_str(data.get("outcome")), duration_minutes=data.get("duration_minutes"),
                                occurred_at=naive_utc(data.get("occurred_at")) or utcnow(), created_by=user.user_id,
                                source="MANUAL")
            rules.repos.lookups.add(a)
            if mentioned_users:
                rules.repos.lookups.flush()
                rules.collab.save_mentions(user, a, mentioned_users, opp.name, f"/app/deals/{opp.opportunity_id}")
            opp.updated_at = utcnow()
            self.commit()
            if data.get("add_to_calendar") and data["activity_type"] == "MEETING":
                rules.collab.push_meeting(user, a, opp.name)
            return {"activity_id": a.activity_id}

    def timeline(self, user: Any, opportunity_id: UUID | str, task_dict: TaskFormatter) -> dict:
        """Activities, tasks, emails with the deal's contact, proposals and changes, newest first."""
        opportunity_id = str(opportunity_id)
        with self.work() as rules:
            opp = rules.get_opp(user, opportunity_id)
            lookups = rules.repos.lookups
            since = (opp.created_at or utcnow()) - timedelta(days=TIMELINE_WINDOW_DAYS)
            acts = lookups.deal_activities(opp.opportunity_id, opp.prospect_id, since, 200)
            names: Dict[str, str] = {}
            names.update(rules.user_names([a.created_by for a in acts]))
            mentioned = rules.collab.mentions_for([a.activity_id for a in acts if a.activity_type == "NOTE"])
            items = [_activity_item(a, opp.opportunity_id, names, mentioned) for a in acts]
            for t in lookups.deal_tasks(opp.opportunity_id, 100):
                items.append({"kind": "TASK", "at": t.completed_at or t.created_at, "task": task_dict(t, names)})
            if opp.prospect_id:
                items.extend(_email_item(m) for m in lookups.contact_emails(opp.prospect_id, since, 100))
            show_amounts = None
            for h in history(rules, "DEAL", opp.opportunity_id):
                if h["field"] == "amount":
                    show_amounts = amounts_allowed(rules.repos.settings, user) if show_amounts is None else show_amounts
                    if not show_amounts:
                        continue
                items.append({"kind": "PROPERTY_CHANGE", "at": h["changed_at"], "change": h})
            items.sort(key=lambda i: i["at"] or datetime.min, reverse=True)
            return {"items": items}

    # ── proposals (BR-SP-02) ────────────────────────────────
    @staticmethod
    def _get_proposal(rules: SalesRules, user: Any, proposal_id: str) -> Any:
        p = rules.repos.proposals.get(user.tenant_id, proposal_id)
        if not p:
            raise NotFoundError("Proposal not found")
        rules.get_opp(user, p.opportunity_id)  # visibility through its opportunity
        return p

    def list_proposals(self, user: Any, flt: ProposalFilter) -> dict:
        """The proposal pipeline: each status with its proposals and total (BR-SP-02, BR-SP-04)."""
        with self.work() as rules:
            rows = rules.repos.proposals.pipeline(user.tenant_id, rules.collab.owner_scope(user), flt.opportunities,
                                                  flt.statuses, flt.limit)
            opp_map = {o["opportunity_id"]: o for o in rules.opp_dicts(rules.repos.opportunities.by_ids(
                user.tenant_id, {p.opportunity_id for p in rows}))} if rows else {}
            items = [proposal_dict(p, opp_map.get(p.opportunity_id)) for p in rows]
            return masked_with(rules.repos.settings, user, {
                "columns": _proposal_columns(items), "items": items, "total": len(items),
                "total_amount": round(sum(i["amount"] or 0 for i in items), 2)})

    def create_proposal(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            check_amount_rights(rules, user, data)
            if not data.get("opportunity_id"):
                raise BadRequestError("A proposal belongs to an opportunity")
            opp = rules.get_opp(user, data["opportunity_id"])
            title = clean_str(data.get("title")) or f"Proposal - {opp.name}"
            p = Proposal(tenant_id=user.tenant_id, opportunity_id=opp.opportunity_id, title=title[:255],
                         amount=money(data.get("amount")) if "amount" in data else opp.amount,
                         valid_until=data.get("valid_until"), notes=data.get("notes"), created_by=user.user_id)
            set_proposal_status(p, data.get("status") or "DRAFT")
            rules.repos.proposals.add(p)
            rules.collab.record_changes(user.tenant_id, "DEAL", opp.opportunity_id, {"proposal": None},
                                        {"proposal": f"{title} ({p.status.lower()})"}, user.user_id, "UI")
            self.commit()
            return masked_with(rules.repos.settings, user, proposal_dict(p, rules.opp_dicts([opp])[0]))

    def update_proposal(self, user: Any, proposal_id: UUID | str, data: dict, if_match: Optional[int]) -> dict:
        proposal_id = str(proposal_id)
        with self.work() as rules:
            p = self._get_proposal(rules, user, proposal_id)
            rules.repos.proposals.claim_version(p, resolve_expected(data.pop("expected_version", None), if_match),
                                                "proposal")  # T-3
            check_amount_rights(rules, user, data)
            old_status = p.status
            if "title" in data:
                p.title = clean_str(data["title"]) or p.title
            if "amount" in data:
                p.amount = money(data["amount"])
            for f in ("valid_until", "notes"):
                if f in data:
                    setattr(p, f, data[f])
            if data.get("status"):
                # BR-SF-17: a discount above the limit needs approval
                QuoteApproval(rules.repos, rules.collab).ensure_can_send(p, data["status"])
                set_proposal_status(p, data["status"])
            if p.status != old_status:
                rules.collab.record_changes(user.tenant_id, "DEAL", p.opportunity_id,
                                            {"proposal": f"{p.title}: {old_status.lower()}"},
                                            {"proposal": f"{p.title}: {p.status.lower()}"}, user.user_id, "UI")
            self.commit()
            opp = rules.repos.opportunities.get(user.tenant_id, p.opportunity_id)
            return masked_with(rules.repos.settings, user, proposal_dict(p, rules.opp_dicts([opp])[0]))

    def delete_proposal(self, user: Any, proposal_id: UUID | str) -> dict:
        proposal_id = str(proposal_id)
        with self.work() as rules:
            p = self._get_proposal(rules, user, proposal_id)
            rules.delete_proposals([p.proposal_id])  # its quote lines first (FK)
            self.commit()
            return {"status": "deleted", "proposal_id": proposal_id}

    # ── revenue pipeline (BR-SP-03/04) ──────────────────────
    def revenue(self, user: Any, flt: OpportunityFilter) -> dict:
        """Open pipeline by stage, unweighted and weighted, plus won and lost in the same filter.
        Totals are sums of the same opportunities GET /opportunities returns for these filters."""
        with self.work() as rules:
            stage_list = rules.stages(user.tenant_id, include_inactive=True, commit=self.commit)
            rows = rules.repos.opportunities.revenue_rows(user.tenant_id, rules.collab.owner_scope(user), flt)
            return masked_with(rules.repos.settings, user, revenue_pipeline(stage_list, rows))
