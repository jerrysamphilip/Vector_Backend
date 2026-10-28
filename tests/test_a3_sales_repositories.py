"""A3: the sales repositories against MySQL: filters, owner/team visibility, paging, sorting, tenant isolation,
dependent deletes and the forecast / approval queries (app/repositories/sales*.py)."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.security import hash_password
from app.domain.paging import Paging, Sort, SortDirection
from app.domain.sales import ListQuery, LeadFilter, OpportunityFilter, OwnerScope
from app.models.account import Account
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, Proposal, SalesStage
from app.models.sales_extra import ProposalLine
from app.models.user import User
from app.repositories.sales import SalesRepositories, SqlAlchemySalesUnitOfWork
from tests.conftest import RUN, SessionLocal, register_tenant


def _user(db, tenant_id, label, role="AGENT"):
    u = User(tenant_id=tenant_id, first_name=label, last_name="Repo", email=f"{label}-{RUN}-{uuid.uuid4().hex[:6]}@a3.example.com",
             password_hash=hash_password("x-pass-123"), role=role, status="ACTIVE", auth_provider="local",
             email_verified=True)
    db.add(u)
    db.flush()
    return u.user_id


@pytest.fixture(scope="module")
def world(client):
    a, b = register_tenant(client, "a3ra"), register_tenant(client, "a3rb")
    ta, tb = a["tenant_id"], b["tenant_id"]
    with SessionLocal() as db:
        rep1, rep2 = _user(db, ta, "rep1"), _user(db, ta, "rep2")
        other = _user(db, tb, "other")
        acme = Account(tenant_id=ta, name=f"Acme {RUN}")
        db.add(acme)
        db.flush()
        stages = []
        for i, (name, prob, won) in enumerate((("Open", 20, False), ("Won", 100, True))):
            s = SalesStage(tenant_id=ta, name=name, probability=prob, sort_order=i, is_won=won, is_lost=False)
            db.add(s)
            stages.append(s)
        sb = SalesStage(tenant_id=tb, name="Open", probability=20, sort_order=0, is_won=False, is_lost=False)
        db.add(sb)
        db.flush()
        prospects = []
        for i, owner in enumerate((rep1, rep1, rep2)):
            p = Prospect(tenant_id=ta, email=f"c{i}-{RUN}@a3c.example.com", first_name=f"Zed{i}{RUN}",
                         last_name="Contact", owner_id=owner, company_name="Acme")
            db.add(p)
            prospects.append(p)
        pb = Prospect(tenant_id=tb, email=f"cb-{RUN}@a3c.example.com", first_name="B", last_name="C", owner_id=other)
        db.add(pb)
        db.flush()
        leads = [Lead(tenant_id=ta, prospect_id=prospects[0].prospect_id, owner_id=rep1, stage="NEW", source="Web"),
                 Lead(tenant_id=ta, prospect_id=prospects[1].prospect_id, owner_id=rep1, stage="SQL", source="Event"),
                 Lead(tenant_id=ta, prospect_id=prospects[2].prospect_id, owner_id=rep2, stage="DISQUALIFIED"),
                 Lead(tenant_id=tb, prospect_id=pb.prospect_id, owner_id=other, stage="NEW", source="Web")]
        db.add_all(leads)
        today = date.today()
        opps = [Opportunity(tenant_id=ta, name=f"Alpha {RUN}", owner_id=rep1, stage_id=stages[0].stage_id,
                            amount=Decimal("100"), close_date=today + timedelta(days=10), account_id=acme.account_id,
                            status="OPEN", client_type="NEW"),
                Opportunity(tenant_id=ta, name=f"Beta {RUN}", owner_id=rep2, stage_id=stages[0].stage_id,
                            amount=Decimal("300"), close_date=today + timedelta(days=5), status="OPEN",
                            client_type="EXISTING"),
                Opportunity(tenant_id=ta, name=f"Gamma {RUN}", owner_id=rep1, stage_id=stages[1].stage_id,
                            amount=Decimal("50"), close_date=None, status="WON", client_type="NEW"),
                Opportunity(tenant_id=tb, name=f"Other {RUN}", owner_id=other, stage_id=sb.stage_id,
                            amount=Decimal("999"), status="OPEN", client_type="NEW")]
        db.add_all(opps)
        db.flush()
        prop = Proposal(tenant_id=ta, opportunity_id=opps[0].opportunity_id, title="Q", status="DRAFT")
        db.add(prop)
        db.flush()
        db.add(ProposalLine(proposal_id=prop.proposal_id, description="Seats", quantity=2, unit_price=50,
                            discount_pct=10, sort_order=0))
        db.commit()
        return {"ta": ta, "tb": tb, "rep1": rep1, "rep2": rep2, "other": other,
                "stages": [s.stage_id for s in stages], "leads": [x.lead_id for x in leads],
                "opps": [o.opportunity_id for o in opps], "proposal": prop.proposal_id,
                "prospects": [p.prospect_id for p in prospects]}


def _page(**kw):
    return ListQuery(Sort(kw.pop("field", "created_at"), kw.pop("direction", SortDirection.ASC)),
                     Paging(kw.pop("page", 1), kw.pop("page_size", 50)))


def test_lead_filters_scope_and_tenant(world):
    ta = world["ta"]
    with SessionLocal() as db:
        leads = SalesRepositories(db).leads
        everyone = OwnerScope()
        all_a = leads.page(ta, everyone, LeadFilter(), _page())
        assert {lead.lead_id for lead in all_a.items} == set(world["leads"][:3]) and all_a.total == 3
        assert leads.page(ta, everyone, LeadFilter(open_only=True), _page()).total == 2
        assert leads.page(ta, everyone, LeadFilter(stages=("SQL", "DISQUALIFIED")), _page()).total == 2
        assert leads.page(ta, everyone, LeadFilter(source="Web"), _page()).total == 1
        assert leads.page(ta, everyone, LeadFilter(search=f"Zed2{RUN}"), _page()).total == 1
        mine = OwnerScope(frozenset({world["rep1"]}))
        assert {x.owner_id for x in leads.page(ta, mine, LeadFilter(), _page()).items} == {world["rep1"]}
        assert leads.page(ta, OwnerScope(frozenset()), LeadFilter(), _page()).total == 0
        assert leads.get_visible(ta, mine, world["leads"][2]) is None
        assert leads.get(ta, world["leads"][3]) is None              # other tenant's lead
        assert sorted(leads.sources(ta, everyone)) == ["Event", "Web"]
        page2 = leads.page(ta, everyone, LeadFilter(), _page(page=2, page_size=2))
        assert (len(page2.items), page2.total, page2.pages) == (1, 3, 2)
        assert leads.open_for_prospect(ta, world["prospects"][0]).lead_id == world["leads"][0]
        assert leads.open_for_prospect(world["tb"], world["prospects"][0]) is None


def test_opportunity_filters_sort_totals_and_board(world):
    ta = world["ta"]
    with SessionLocal() as db:
        opps = SalesRepositories(db).opportunities
        everyone = OwnerScope()
        page, amount = opps.page(ta, everyone, OpportunityFilter(), _page(field="close_date"))
        assert [o.name for o in page.items] == [f"Beta {RUN}", f"Alpha {RUN}", f"Gamma {RUN}"]  # NULL close last
        assert (page.total, float(amount)) == (3, 450.0)
        page, amount = opps.page(ta, everyone, OpportunityFilter(statuses=("OPEN",)),
                                 _page(field="amount", direction=SortDirection.DESC))
        assert [o.name for o in page.items] == [f"Beta {RUN}", f"Alpha {RUN}"] and float(amount) == 400.0
        assert opps.page(ta, everyone, OpportunityFilter(search="acme"), _page())[0].total == 1   # company name
        assert opps.page(ta, OwnerScope(frozenset({world["rep2"]})), OpportunityFilter(), _page())[0].total == 1
        assert opps.page(ta, everyone, OpportunityFilter(client_type="EXISTING"), _page())[0].total == 1
        totals = opps.stage_totals(ta, everyone, OpportunityFilter())
        assert totals[world["stages"][0]][0] == 2 and float(totals[world["stages"][0]][1]) == 400.0
        board = opps.board_items(ta, everyone, OpportunityFilter(stage_ids=(world["stages"][0],)), 1)
        assert [o.name for o in board] == [f"Beta {RUN}"]           # one card per stage, soonest close first
        rows = opps.revenue_rows(ta, everyone, OpportunityFilter())
        assert sum(n for _, _, n, _ in rows) == 3
        assert opps.get(ta, world["opps"][3]) is None
        assert world["ta"] in opps.tenants_with_open_deals()


def test_proposals_quotes_and_forecast_queries(world):
    ta = world["ta"]
    with SessionLocal() as db:
        repos = SalesRepositories(db)
        pipeline = repos.proposals.pipeline(ta, OwnerScope(), OpportunityFilter(), (), 1000)
        assert [p.proposal_id for p in pipeline] == [world["proposal"]]
        assert repos.proposals.pipeline(ta, OwnerScope(frozenset({world["rep2"]})), OpportunityFilter(), (), 10) == []
        assert repos.proposals.line_counts([world["proposal"]]) == {world["proposal"]: 1}
        assert [(float(q), float(u), float(d)) for q, u, d in repos.quotes.line_tuples(world["proposal"])] == \
            [(2.0, 50.0, 10.0)]
        assert repos.quotes.deal_owner_id(world["opps"][0]) == world["rep1"]
        start = date.today().replace(day=1) - timedelta(days=400)
        grouped = repos.forecast.grouped_deals(ta, start, date.today() + timedelta(days=400))
        assert {(r[0], r[1]) for r in grouped} == {(world["rep1"], "OPEN"), (world["rep2"], "OPEN")}
        assert float(repos.forecast.won_amount(ta, start, date.today(), None)) == 0.0   # the won deal has no close date
        assert repos.forecast.snapshot_count(ta, "MONTH", start) == 0


def test_unit_of_work_rolls_back_and_deletes_cascade(world):
    ta = world["ta"]
    with SessionLocal() as db:
        uow = SqlAlchemySalesUnitOfWork.for_session(db)
        with pytest.raises(RuntimeError):
            with uow:
                uow.repos.stages.add(SalesStage(tenant_id=ta, name="Ghost", probability=1, sort_order=9))
                uow.repos.stages.flush()
                raise RuntimeError("boom")
        assert all(s.name != "Ghost" for s in SalesRepositories(db).stages.list(ta, True))
    with SessionLocal() as db:
        uow = SqlAlchemySalesUnitOfWork.for_session(db)
        with uow:
            uow.repos.opportunities.delete_many([world["opps"][0]])
            uow.commit()
        repos = SalesRepositories(db)
        assert repos.opportunities.get(ta, world["opps"][0]) is None
        assert repos.proposals.get(ta, world["proposal"]) is None
        assert repos.quotes.line_tuples(world["proposal"]) == []
