"""Sales UAT gap fixes (fix agent E), unit level (no database).

  * E1  the new owner is notified when a lead or a deal is reassigned (not when they reassign to themselves)
  * E6  round robin: the pick reads the settings under a row lock; the index restarts when the pool changes
        and a client-sent index is ignored
  * E7  region rules match country / state names, ISO codes and US state codes, case and space insensitive
  * E8  leaderboard targets are pro-rated over the fiscal quarters a period touches; attainment ordering
"""
from datetime import date
from types import SimpleNamespace

import pytest

from app.domain.regions import country_key, region_matches, state_key
from app.domain.reports import period_quarter_shares, quarter_bounds
from app.services import sales_settings
from app.services.reports_sales_service import _attainment_rank
from app.services.sales import SalesRules
from app.services.sales_deals import DealService
from app.services.sales_leads import LeadService
from tests.unit.a3_sales_fakes import (FakeCollaborators, FakeLeads, FakeLookups, FakeOpps, FakeSettingsRepo,
                                       FakeStages, FakeUow, deal, make_repos, sales_stage, user)
from tests.unit.test_a3_sales_services import STAGES, lead, prospect

USERS = [user("u1"), user("u2"), user("u3")]


# ── E1 ──

def test_lead_reassignment_notifies_the_new_owner():
    repos = make_repos(leads=FakeLeads([lead(owner_id="u1")]), lookups=FakeLookups(users=USERS, prospects=[prospect()]))
    collab = FakeCollaborators()
    svc = LeadService(FakeUow(repos), collaborators=lambda session: collab)
    svc.update(user("u1", full_name="Ann Lee"), "l1", {"owner_id": "u2"}, None)
    assert repos.leads.rows["l1"].owner_id == "u2"
    kind, ids, title, body, link = collab.notifications[-1]
    assert (kind, ids, link) == ("ASSIGNED", ["u2"], "/app/leads/l1")
    assert "Pat Doe" in title and "Ann Lee" in body
    # the owner change is still recorded, so workflow rules on Owner keep firing
    assert any(c[0] == "LEAD" and c[3].get("owner_id") == "u2" for c in collab.changes)


@pytest.mark.parametrize("actor, new_owner", [("u2", "u2"), ("u1", "u1")])
def test_no_reassignment_notice_to_yourself_or_without_a_change(actor, new_owner):
    repos = make_repos(leads=FakeLeads([lead(owner_id="u1")]), lookups=FakeLookups(users=USERS, prospects=[prospect()]))
    collab = FakeCollaborators()
    LeadService(FakeUow(repos), collaborators=lambda session: collab).update(user(actor), "l1",
                                                                             {"owner_id": new_owner}, None)
    assert collab.notifications == []


def test_deal_reassignment_notifies_the_new_owner():
    repos = make_repos(stages=FakeStages(STAGES), opportunities=FakeOpps([deal(owner_id="u2")]),
                       lookups=FakeLookups(users=USERS))
    repos.proposals = SimpleNamespace(for_opportunity=lambda oid: [], line_counts=lambda ids: {})
    collab = FakeCollaborators()
    svc = DealService(FakeUow(repos), collaborators=lambda session: collab)
    svc.update_opportunity(user("u1"), "o1", {"owner_id": "u3"}, None)
    kind, ids, title, _body, link = collab.notifications[-1]
    assert (kind, ids, title, link) == ("ASSIGNED", ["u3"], "Deal reassigned: Big deal", "/app/deals/o1")


# ── E6 ──

def _round_robin(stored):
    repo = FakeSettingsRepo(stored)
    rules = SalesRules(make_repos(settings=repo, lookups=FakeLookups(users=USERS)), FakeCollaborators())
    return rules, repo


def test_round_robin_pick_is_read_under_a_lock_and_advances():
    rules, repo = _round_robin({"lead_assignment": {"mode": "round_robin", "users": ["u1", "u2"], "next_index": 1}})
    assert rules.auto_owner("t1", prospect(owner_id=None)) == "u2"
    assert getattr(repo, "locked", False)
    assert rules.auto_owner("t1", prospect(owner_id=None)) == "u1"
    assert repo.doc["lead_assignment"]["next_index"] == 1


def test_round_robin_index_restarts_when_the_pool_changes():
    current = {"users": ["u1", "u2", "u3"], "next_index": 2}
    assert sales_settings.assignment_changes(current, {"users": ["u3", "u1"]})["next_index"] == 0
    # Same pool: the index is left alone, and a client value never overwrites it
    same = sales_settings.assignment_changes(current, {"users": ["u1", "u2", "u3"], "next_index": 9, "mode": "off"})
    assert "next_index" not in same and same["mode"] == "off"
    assert "next_index" not in sales_settings.assignment_changes(current, {"mode": "region"})


# ── E7 ──

@pytest.mark.parametrize("rule, contact", [
    ("US", "United States"), ("usa", "U.S.A."), ("United States of America", " us "),
    ("IN", "india"), ("IND", "India"), ("UK", "United Kingdom"), ("GB", "uk"), ("gbr", "Great  Britain"),
    ("Germany", "DE"), ("Canada", "canada "),
])
def test_country_aliases_match(rule, contact):
    assert region_matches("country", rule, contact)


@pytest.mark.parametrize("rule, contact", [("CA", "California"), ("new york", "NY"), ("TX", " texas"),
                                           ("District of Columbia", "dc")])
def test_us_state_codes_and_names_match(rule, contact):
    assert region_matches("state", rule, contact)


def test_unknown_and_blank_regions():
    assert region_matches("country", "Atlantis", " atlantis ")  # not in the table: normalised text
    assert region_matches("state", "Maharashtra", "maharashtra")
    assert not region_matches("country", "US", "India")
    assert not region_matches("country", "US", "")
    assert not region_matches("state", "", None)
    assert not region_matches("country", "IN", "Indiana")
    assert country_key("United Kingdom") == "GB" and state_key("California") == "CA"


def test_region_rule_assigns_by_iso_code():
    rules, _ = _round_robin({"lead_assignment": {"mode": "region", "users": ["u1"],
                                                 "regions": [{"field": "country", "value": "US", "user_id": "u3"},
                                                             {"field": "state", "value": "KA", "user_id": "u2"}]}})
    assert rules.auto_owner("t1", SimpleNamespace(poc_country="united states", poc_state=None)) == "u3"
    assert rules.auto_owner("t1", SimpleNamespace(poc_country="India", poc_state="ka")) == "u2"
    assert rules.auto_owner("t1", SimpleNamespace(poc_country="France", poc_state=None)) == "u1"  # pool fallback


# ── E8 ──

def _fy_april(fy):
    return date(fy, 4, 1), date(fy + 1, 4, 1)


def _fy_of(d):
    return d.year if d.month >= 4 else d.year - 1


def test_period_quarter_shares():
    q1 = quarter_bounds(*_fy_april(2026), 1)
    assert period_quarter_shares(q1[0], date(2026, 6, 30), _fy_of, _fy_april) == [(2026, 1, 1.0)]
    shares = period_quarter_shares(date(2026, 6, 1), date(2026, 7, 31), _fy_of, _fy_april)
    assert [(fy, q) for fy, q, _ in shares] == [(2026, 1), (2026, 2)]
    assert shares[0][2] == pytest.approx(30 / 91) and shares[1][2] == pytest.approx(31 / 92)
    across = period_quarter_shares(date(2027, 3, 1), date(2027, 4, 30), _fy_of, _fy_april)
    assert [(fy, q) for fy, q, _ in across] == [(2026, 4), (2027, 1)]


def test_attainment_rank_puts_reps_without_a_target_last():
    rows = [{"attainment": None}, {"attainment": 40.0}, {"attainment": 120.0}]
    assert [r["attainment"] for r in sorted(rows, key=_attainment_rank)] == [120.0, 40.0, None]
