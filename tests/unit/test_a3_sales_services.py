"""A3: sales services over fake repositories and collaborators (no database).

Stage transitions, forecast categories, win/loss reasons, amount hiding, discount approval, stale deals,
lead conflicts and the unit of work (one commit per call, rollback on error).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.sales import ListQuery, OPPORTUNITY_SORT_FIELDS, OpportunityFilter, OwnerScope
from app.services import sales_settings
from app.services.quote_approval import QuoteApproval
from app.services.sales_deals import DealService, revenue_pipeline, stage_history
from app.services.sales_jobs import SalesJobs
from app.services.sales_leads import LeadService
from tests.unit.a3_sales_fakes import (FakeCollaborators, FakeLeads, FakeLookups, FakeOpps, FakeQuotes,
                                       FakeSettingsRepo, FakeStages, FakeUow, deal, make_repos, sales_stage, user)

STAGES = [sales_stage("open", 10, order=0), sales_stage("nego", 75, order=1),
          sales_stage("won", 100, won=True, order=2), sales_stage("lost", 0, lost=True, order=3)]


def deal_service(repos, collab):
    uow = FakeUow(repos)
    return DealService(uow, collaborators=lambda session: collab), uow


def lead(**kw):
    base = dict(lead_id="l1", tenant_id="t1", prospect_id="p1", account_id=None, owner_id="u1", source=None,
                stage="NEW", qualification=None, next_step=None, next_step_at=None, disqualified_reason=None,
                recycle_at=None, opportunity_id=None, campaign_id=None, stage_changed_at=None, qualified_at=None,
                converted_at=None, created_at=datetime(2026, 1, 1), row_version=1)
    base.update(kw)
    return SimpleNamespace(**base)


def prospect(**kw):
    base = dict(prospect_id="p1", tenant_id="t1", owner_id="u1", lifecycle_stage="LEAD", full_name="Pat Doe",
                email="pat@x.test", designation=None, company_name="Acme", account_id=None, lead_source=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── settings and amount hiding (BR-SF-12) ────────────────────

def test_settings_merge_and_apply():
    merged = sales_settings.merge_settings({"lead_assignment": {"mode": "region"}, "stale_deal_days": 7})
    assert merged["lead_assignment"]["mode"] == "region" and merged["lead_assignment"]["users"] == []
    assert merged["stale_deal_days"] == 7 and merged["require_close_reason"] is True
    changed = sales_settings.apply_changes(merged, {"unknown": 1, "lead_assignment": {"next_index": 3}})
    assert "unknown" not in changed and changed["lead_assignment"] == {**merged["lead_assignment"], "next_index": 3}


def test_amounts_hidden_by_role_and_level():
    s = {"amount_hidden_roles": ["AGENT"], "amount_hidden_levels": [4]}
    assert sales_settings.amounts_visible(user(role="ADMIN"), s)
    assert not sales_settings.amounts_visible(user(role="AGENT"), s)
    assert not sales_settings.amounts_visible(user(role="MANAGER", sales_level=4), s)
    assert sales_settings.amounts_visible(user(role="MANAGER", sales_level=2), s)
    repo = FakeSettingsRepo(s)
    agent = user(role="AGENT")
    assert sales_settings.masked_with(repo, agent, {"amount": 5, "name": "x", "categories": {"COMMIT": 3}}) == \
        {"amount": None, "name": "x", "categories": {"COMMIT": None}}


def test_hidden_amount_sort_falls_back_to_close_date_and_masks():
    repos = make_repos(settings=FakeSettingsRepo({"amount_hidden_roles": ["AGENT"]}), stages=FakeStages(STAGES),
                       opportunities=FakeOpps([deal(amount=Decimal("100"))]))
    svc, uow = deal_service(repos, FakeCollaborators())
    out = svc.list_opportunities(user(role="AGENT"), OpportunityFilter(), False,
                                 ListQuery.build("amount", "desc", 1, 50, OPPORTUNITY_SORT_FIELDS))
    _scope, _flt, query = repos.opportunities.page_calls[0]
    assert query.sort.field == "close_date"
    assert out["total_amount"] is None and out["items"][0]["amount"] is None
    assert uow.commits == 0


# ── stage transitions, forecast categories, win/loss ─────────

def test_won_needs_a_reason_and_rolls_back():
    repos = make_repos(stages=FakeStages(STAGES), opportunities=FakeOpps([deal()]))
    svc, uow = deal_service(repos, FakeCollaborators())
    with pytest.raises(BadRequestError, match="Say why the deal was won"):
        svc.update_opportunity(user(), "o1", {"stage_id": "won"}, None)
    assert uow.commits == 0 and uow.rollbacks == 1


def test_winning_a_deal_closes_it_notifies_and_makes_a_customer():
    p = prospect(owner_id="u2", lifecycle_stage="OPPORTUNITY")
    repos = make_repos(stages=FakeStages(STAGES), opportunities=FakeOpps([deal(prospect_id="p1")]),
                       lookups=FakeLookups(prospects=[p]))
    repos.proposals = SimpleNamespace(for_opportunity=lambda oid: [], line_counts=lambda ids: {})
    collab = FakeCollaborators(managers={"u2": "m1"})
    svc, uow = deal_service(repos, collab)
    out = svc.update_opportunity(user(), "o1", {"stage_id": "won", "closed_reason": "Best fit",
                                                "expected_version": 1}, None)
    opp = repos.opportunities.rows["o1"]
    assert (opp.status, opp.forecast_category, opp.forecast_category_manual) == ("WON", "CLOSED", False)
    assert opp.closed_at is not None and opp.close_date == date.today()
    assert p.lifecycle_stage == "CUSTOMER"
    kind, recipients, *_ = collab.notifications[0]
    assert kind == "DEAL_WON" and recipients == ["u2", "m1"]
    assert repos.opportunities.claimed == [("opportunity", 1)]
    deal_change = [c for c in collab.changes if c[0] == "DEAL"][0]
    assert deal_change[2]["stage_id"] == "Open" and deal_change[3]["stage_id"] == "Won"  # stage names in history
    assert out["status"] == "WON" and uow.commits == 1


def test_manual_forecast_category_survives_open_moves_until_cleared():
    repos = make_repos(stages=FakeStages(STAGES), opportunities=FakeOpps([deal()]))
    repos.proposals = SimpleNamespace(for_opportunity=lambda oid: [], line_counts=lambda ids: {})
    svc, _ = deal_service(repos, FakeCollaborators())
    svc.update_opportunity(user(), "o1", {"forecast_category": "COMMIT"}, None)
    svc2, _ = deal_service(repos, FakeCollaborators())
    svc2.update_opportunity(user(), "o1", {"stage_id": "nego"}, None)
    opp = repos.opportunities.rows["o1"]
    assert (opp.forecast_category, opp.forecast_category_manual) == ("COMMIT", True)
    svc3, _ = deal_service(repos, FakeCollaborators())
    svc3.update_opportunity(user(), "o1", {"forecast_category": None}, None)
    assert (opp.forecast_category, opp.forecast_category_manual) == ("COMMIT", False)  # nego is 75% -> COMMIT
    svc4, _ = deal_service(repos, FakeCollaborators())
    with pytest.raises(BadRequestError, match="Unknown forecast category"):
        svc4.update_opportunity(user(), "o1", {"forecast_category": "MAYBE"}, None)


def test_agents_cannot_delete_deals_and_invisible_deals_404():
    repos = make_repos(stages=FakeStages(STAGES), opportunities=FakeOpps([deal(owner_id="u9")]))
    svc, _ = deal_service(repos, FakeCollaborators(scope=OwnerScope(frozenset({"u1"}))))
    with pytest.raises(NotFoundError, match="Opportunity not found"):
        svc.delete_opportunity(user(role="AGENT"), "o1")
    svc2, _ = deal_service(repos, FakeCollaborators())
    with pytest.raises(PermissionDenied, match="Agents cannot delete"):
        svc2.delete_opportunity(user(role="AGENT"), "o1")


def test_revenue_pipeline_and_stage_history_are_pure():
    out = revenue_pipeline(STAGES, [("open", "NEW", 2, 100), ("won", "EXISTING", 1, 50), ("lost", None, 1, 30)])
    t = out["totals"]
    assert (t["open_count"], t["open_amount"], t["weighted_amount"]) == (2, 100.0, 10.0)
    assert (t["won_amount"], t["lost_amount"]) == (50.0, 30.0)
    assert t["by_client_type"]["NEW"] == {"amount": 100.0, "weighted": 10.0}
    now = datetime(2026, 1, 11)
    opp = SimpleNamespace(created_at=datetime(2026, 1, 1), closed_at=None)
    hist = stage_history(opp, [{"field": "stage_id", "old_value": "Open", "new_value": "Nego",
                                "changed_at": datetime(2026, 1, 3)}], now)
    assert [(h["stage"], h["days"]) for h in hist] == [("Open", 2.0), ("Nego", 8.0)]


# ── leads ────────────────────────────────────────────────────

def lead_service(repos, collab):
    uow = FakeUow(repos)
    return LeadService(uow, collaborators=lambda session: collab), uow


def test_sql_needs_all_criteria_then_sets_qualified_at():
    repos = make_repos(leads=FakeLeads([lead()]), lookups=FakeLookups(users=[user()], prospects=[prospect()]))
    svc, uow = lead_service(repos, FakeCollaborators())
    with pytest.raises(BadRequestError, match="To mark as SQL"):
        svc.update(user(), "l1", {"stage": "SQL"}, None)
    assert uow.commits == 0
    svc2, uow2 = lead_service(repos, FakeCollaborators())
    out = svc2.update(user(), "l1", {"stage": "SQL", "qualification": {k: True for k in
                                                                     ("budget", "authority", "need", "timeline")}}, None)
    assert out["stage"] == "SQL" and repos.leads.rows["l1"].qualified_at is not None
    assert repos.lookups.prospects_["p1"].lifecycle_stage == "SQL" and uow2.commits == 1


def test_second_open_lead_is_a_conflict_with_the_existing_id():
    repos = make_repos(leads=FakeLeads([lead()]), lookups=FakeLookups(users=[user()], prospects=[prospect()]))
    svc, _ = lead_service(repos, FakeCollaborators())
    with pytest.raises(ConflictError) as err:
        svc.create(user(), {"prospect_id": "p1", "owner_id": None, "source": None, "stage": "NEW",
                            "next_step": None, "next_step_at": None, "campaign_id": None})
    assert err.value.detail == {"message": "This contact already has an open lead", "lead_id": "l1"}


def test_agents_delete_only_their_own_leads_and_converted_leads_stay():
    repos = make_repos(leads=FakeLeads([lead(owner_id="u2"), lead(lead_id="l2", stage="CONVERTED")]))
    svc, _ = lead_service(repos, FakeCollaborators())
    with pytest.raises(PermissionDenied):
        svc.delete(user(role="AGENT"), "l1")
    svc2, _ = lead_service(repos, FakeCollaborators())
    with pytest.raises(BadRequestError, match="Converted leads"):
        svc2.delete(user(), "l2")


# ── discount approval (BR-SF-17) ─────────────────────────────

def quote(**kw):
    base = dict(proposal_id="q1", tenant_id="t1", opportunity_id="o1", title="Quote", status="DRAFT",
                created_by="rep", approval_status="NOT_REQUIRED", approval_discount_pct=None,
                approval_requested_by=None, approval_requested_at=None, approval_decided_by=None,
                approval_decided_at=None, approval_reason=None)
    base.update(kw)
    return SimpleNamespace(**base)


def approval(lines, threshold=10):
    repos = make_repos(settings=FakeSettingsRepo({"discount_approval_threshold": threshold}),
                       quotes=FakeQuotes(lines={"q1": lines}, owners={"o1": "rep"}, managers={"rep": "boss"}),
                       lookups=FakeLookups())
    collab = FakeCollaborators()
    return QuoteApproval(repos, collab), collab


def test_quote_above_threshold_cannot_be_sent_until_approved():
    qa, collab = approval([(1, 100, 20)])
    p = quote()
    with pytest.raises(ConflictError, match="Submit it for approval first"):
        qa.ensure_can_send(p, "SENT")
    qa.submit(SimpleNamespace(user_id="rep", role="AGENT", first_name="Rep", last_name=""), p, "please")
    assert p.approval_status == "PENDING" and collab.notifications[0][1] == ["boss"]
    with pytest.raises(PermissionDenied):   # nobody decides on their own quote
        qa.decide(SimpleNamespace(user_id="rep", role="AGENT", tenant_id="t1", first_name="R", last_name=""),
                  p, True, None)
    qa.decide(SimpleNamespace(user_id="boss", role="MANAGER", tenant_id="t1", first_name="B", last_name=""),
              p, True, None)
    assert p.approval_status == "APPROVED" and p.approval_discount_pct == Decimal("20.00")
    qa.ensure_can_send(p, "SENT")   # cleared now


def test_raising_an_approved_discount_goes_back_to_pending():
    qa, collab = approval([(1, 100, 30)])
    p = quote(approval_status="APPROVED", approval_discount_pct=Decimal("20"))
    qa.after_lines_changed(SimpleNamespace(user_id="rep", role="AGENT", first_name="R", last_name=""), p)
    assert p.approval_status == "PENDING" and p.approval_discount_pct == Decimal("30.00")
    assert collab.notifications[-1][0] == "QUOTE_APPROVAL"


def test_reject_needs_a_reason():
    qa, _ = approval([(1, 100, 30)])
    p = quote(approval_status="PENDING", approval_requested_by="rep")
    with pytest.raises(BadRequestError, match="reason"):
        qa.decide(SimpleNamespace(user_id="adm", role="ADMIN", tenant_id="t1", first_name="A", last_name=""),
                  p, False, None)


# ── background jobs ──────────────────────────────────────────

def test_stale_deal_alerts():
    old = datetime.now() - timedelta(days=30)
    opps = FakeOpps([deal("o1", updated_at=old, created_at=old), deal("o2", updated_at=datetime.now()),
                     deal("o3", updated_at=datetime.now(), close_date=date.today() - timedelta(days=1))])
    commits = []
    collab = FakeCollaborators()
    jobs = SalesJobs(make_repos(opportunities=opps), collab, lambda: commits.append(1))
    assert jobs.stale_deals() == 2
    bodies = {n[4]: n[3] for n in collab.notifications}
    assert "no activity for" in bodies["/app/deals/o1"] and "has passed" in bodies["/app/deals/o3"]
    assert commits == [1]


def test_recycle_reopens_disqualified_leads():
    due = lead(stage="DISQUALIFIED", recycle_at=datetime(2020, 1, 1), disqualified_reason="No budget")
    collab = FakeCollaborators()
    jobs = SalesJobs(make_repos(leads=FakeLeads([due]), lookups=FakeLookups(prospects=[prospect()])), collab,
                     lambda: None)
    assert jobs.recycle_leads() == 1
    assert (due.stage, due.recycle_at) == ("NEW", None)
    assert collab.notifications[0][0] == "LEAD_RECYCLED" and collab.changes[0][4] == "SYSTEM"
